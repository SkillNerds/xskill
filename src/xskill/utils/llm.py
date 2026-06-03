"""
utils/llm.py — LLM 和 Embedding 统一客户端
═════════════════════════════════════════════
支持任意 OpenAI 兼容 API (vLLM, Ollama, Together, DeepSeek, 硅基流动, etc.)
LLM 和 Embedding 分别配置 base_url / model / api_key

用法:
  from xskill.utils.llm import LLMClient, EmbedClient

  llm = LLMClient.from_config(config["llm"])
  text = llm.chat("分析这条轨迹...")

  embed = EmbedClient.from_config(config["embedding"])
  vec = embed.encode("some text")       # → np.ndarray (dim,)
  vecs = embed.encode_batch(["a","b"])   # → np.ndarray (n, dim)
  print(embed.dim)                       # 自动探测的维度
"""

from __future__ import annotations

import os, json, logging, time
from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np

EmbedApiStyle = Literal["multimodal", "openai"]

logger = logging.getLogger(__name__)


def _ssl_verify() -> bool:
    """T2S_SSL_VERIFY=false / 0 / no / off → 不校验 SSL 证书。
    场景：企业代理做 MITM，用自签名 CA 重签上游证书，httpx 默认 verify 会抛
    CERTIFICATE_VERIFY_FAILED。更安全的方式是 export SSL_CERT_FILE=/path/to/ca.pem
    让 httpx 信任代理 CA；想快速 demo 可以直接关掉。"""
    v = os.environ.get("T2S_SSL_VERIFY", "").lower()
    return v not in ("false", "0", "no", "off")


# ═══════════════════════════════════════════════════════════════════
# LLM Client (chat completion)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class LLMClient:
    base_url: str
    model: str
    api_key: str
    # max_tokens 是模型 thinking + content 的总预算。对 deepseek-v4-flash
    # 这类 thinking model，reasoning 会先吃掉一部分 budget，剩下的才落到
    # XML/JSON content；之前默认 1500 偏紧，复杂 traj 上经常出现"返回 0
    # chars"或"截断在某个标签中段"导致 meta validate 失败 fallback rule。
    # 10000 给 thinking + 实际输出留足空间。``llm.max_tokens`` 在 config
    # 里可覆盖；缩小可省 token 费但要承担更高 fallback 率。
    max_tokens: int = 10000
    temperature: float = 0.0
    # 限流配置；None = 不限流(快路径)。结构: {rpm, tpm, burst} 任一可缺。
    # 详见 src/xskill/utils/rate_limit.py 与 docs/adr/0001。
    rate_limit_cfg: "Optional[dict]" = field(default=None)
    _client: object = field(default=None, repr=False)

    @classmethod
    def from_config(cls, cfg: dict) -> "LLMClient":
        base_url = cfg.get("base_url", "").rstrip("/")
        model = cfg.get("model", "")
        api_key = (
            cfg.get("api_key", "")
            or os.environ.get("LLM_API_KEY", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", "")
        )
        if not base_url or not model:
            raise ValueError("llm.base_url 和 llm.model 必须配置")
        kwargs = dict(base_url=base_url, model=model, api_key=api_key)
        # 允许 config 覆盖 max_tokens / temperature（缺省则用 dataclass 默认）。
        # 之前这里不读 max_tokens，导致 yaml 配了也不生效。
        if "max_tokens" in cfg:
            kwargs["max_tokens"] = int(cfg["max_tokens"])
        if "temperature" in cfg:
            kwargs["temperature"] = float(cfg["temperature"])
        if "rate_limit" in cfg:
            kwargs["rate_limit_cfg"] = cfg["rate_limit"]
        return cls(**kwargs)

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            kwargs = {"base_url": self.base_url, "api_key": self.api_key or "no-key", "timeout": 60.0}
            if not _ssl_verify():
                import httpx
                kwargs["http_client"] = httpx.Client(verify=False, timeout=60.0)
                logger.warning("T2S_SSL_VERIFY=false → LLM HTTPS 证书验证已关闭")
            self._client = OpenAI(**kwargs)
        return self._client

    def chat(self, prompt: str, system: str = "") -> str:
        """单轮对话，返回文本"""
        if self.rate_limit_cfg:
            # 走限流 wrapper —— 共享按 base_url 注册的桶
            from xskill.utils.rate_limit import (
                RateLimitedLLM, get_or_create_bucket,
            )
            bucket = get_or_create_bucket(
                self.base_url,
                rpm=self.rate_limit_cfg.get("rpm"),
                tpm=self.rate_limit_cfg.get("tpm"),
                burst=self.rate_limit_cfg.get("burst"),
            )
            wrapper = RateLimitedLLM(bucket=bucket, inner_call=self._raw_chat)
            resp = wrapper.call(prompt=prompt, system=system, timeout=60.0)
            self._record(resp)
            return resp.choices[0].message.content
        resp = self._raw_chat(prompt=prompt, system=system)
        self._record(resp)
        return resp.choices[0].message.content

    def _record(self, resp) -> None:
        """旁路记账(Issue #43);best-effort,绝不抛。"""
        from xskill.usage import current_step, get_ledger
        get_ledger().record_llm(current_step(), self.model, resp)

    def _raw_chat(self, *, prompt: str, system: str = ""):
        """原始 LLM 调用,返回完整 response 对象(供 wrapper reconcile usage)。"""
        client = self._get_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise

    def chat_stream(self, prompt: str, system: str = ""):
        """单轮对话，流式返回文本 chunk。

        Yields str chunks as they arrive from the OpenAI streaming API.
        Usage::

            for chunk in llm.chat_stream("hello"):
                print(chunk, end="")
        """
        client = self._get_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            stream = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content
        except Exception as e:
            logger.error(f"LLM 流式调用失败: {e}")
            raise

    def __repr__(self):
        return f"LLMClient(base_url={self.base_url}, model={self.model})"


# ═══════════════════════════════════════════════════════════════════
# Embedding Client
# ═══════════════════════════════════════════════════════════════════

def _resolve_embed_api_style(cfg: dict, model: str) -> EmbedApiStyle:
    """ARK 有两套 embedding 路径：

    - ``/embeddings`` — OpenAI 兼容，用于 ``doubao-embedding-large-text-*`` 等纯文本模型
    - ``/embeddings/multimodal`` — 用于 ``doubao-embedding-vision-*`` 等多模态模型

    可在 config 里显式写 ``embedding.api: openai | multimodal``；否则按模型名推断。
    """
    explicit = (cfg.get("api") or cfg.get("api_style") or "").strip().lower()
    if explicit in ("multimodal", "openai"):
        return explicit  # type: ignore[return-value]
    if "vision" in model.lower():
        return "multimodal"
    return "openai"


@dataclass
class EmbedClient:
    base_url: str
    model: str
    api_key: str
    dim: int = 0  # 0 = 未探测
    api_style: EmbedApiStyle = "openai"
    _client: object = field(default=None, repr=False)

    @classmethod
    def from_config(cls, cfg: dict) -> "EmbedClient":
        base_url = cfg.get("base_url", "").rstrip("/")
        model = cfg.get("model", "")
        api_key = (
            cfg.get("api_key", "")
            or os.environ.get("EMBED_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", "")
        )
        dim = cfg.get("dim", 0)
        if not base_url or not model:
            raise ValueError("embedding.base_url 和 embedding.model 必须配置")
        api_style = _resolve_embed_api_style(cfg, model)
        inst = cls(
            base_url=base_url, model=model, api_key=api_key, dim=dim, api_style=api_style,
        )
        return inst

    def _get_session(self):
        if self._client is None:
            import httpx
            verify = _ssl_verify()
            self._client = httpx.Client(timeout=60, verify=verify)
            if not verify:
                logger.warning("T2S_SSL_VERIFY=false → Embedding HTTPS 证书验证已关闭")
        return self._client

    def _post_json(self, path: str, body: dict) -> dict:
        session = self._get_session()
        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        resp = session.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def _call_api_multimodal(self, text: str) -> list[float]:
        """ARK multimodal：``doubao-embedding-vision-*`` 等"""
        data = self._post_json(
            "/embeddings/multimodal",
            {"model": self.model, "input": [{"type": "text", "text": text}]},
        )
        return data["data"]["embedding"]

    def _call_api_openai(self, text: str) -> list[float]:
        """ARK / OpenAI 兼容：``POST /embeddings``，``doubao-embedding-large-text-*`` 等"""
        data = self._post_json(
            "/embeddings",
            {"model": self.model, "input": text},
        )
        # 旁路记账(Issue #43);embeddings response 自带 usage.total_tokens。
        from xskill.usage import get_ledger
        get_ledger().record_embed(self.model, data)
        items = data.get("data") or []
        if not items:
            raise ValueError(f"embedding response missing data: {data!r}")
        return items[0]["embedding"]

    def _call_api_single(self, text: str) -> list[float]:
        if self.api_style == "multimodal":
            return self._call_api_multimodal(text)
        return self._call_api_openai(text)

    def probe_dim(self) -> int:
        """发送测试文本，探测 embedding 维度"""
        if self.dim > 0:
            return self.dim

        logger.info(
            "探测 embedding 维度: %s @ %s (api=%s)",
            self.model, self.base_url, self.api_style,
        )
        vec = self._call_api_single("hello")
        self.dim = len(vec)
        logger.info(f"探测完成: dim={self.dim}")
        return self.dim

    def encode(self, text: str) -> np.ndarray:
        """单条文本 → 向量"""
        vec = np.array(self._call_api_single(text), dtype=np.float32)
        if self.dim == 0:
            self.dim = len(vec)
        return vec

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        """批量文本 → (n, dim) 矩阵，逐条调用 embedding 端点"""
        from tqdm import tqdm
        all_vecs = []

        for i, text in enumerate(tqdm(texts, desc="embedding", unit="条")):
            try:
                vec = np.array(self._call_api_single(text), dtype=np.float32)
                all_vecs.append(vec)
            except Exception as e:
                logger.error(f"Embedding 失败 (第 {i} 条): {e}")
                raise

            if (i + 1) % 10 == 0:
                time.sleep(0.1)  # 简单限速

        result = np.stack(all_vecs)
        if self.dim == 0:
            self.dim = result.shape[1]
        return result

    def __repr__(self):
        return (
            f"EmbedClient(base_url={self.base_url}, model={self.model}, "
            f"dim={self.dim}, api_style={self.api_style})"
        )


# ═══════════════════════════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════════════════════════

def create_embed_client(config: dict) -> "EmbedClient":
    """根据配置创建 embedding 客户端，未配置或不可用时直接报错"""
    embed_cfg = config.get("embedding", {})

    if not embed_cfg.get("base_url") or not embed_cfg.get("model"):
        raise ValueError("embedding.base_url 和 embedding.model 必须配置")

    client = EmbedClient.from_config(embed_cfg)
    client.probe_dim()
    logger.info(f"Embedding: {client}")
    return client


def create_llm_client(config: dict, role: str = "default") -> "LLMClient | None":
    """根据配置创建 LLM 客户端，未配置返回 None。

    role:
      "default" / "index" — 用 config["llm"]（轻量用途：轨迹 meta 抽取、检索等）
      "skill" / "eval"    — 优先用 config["llm_skill"] 覆盖，缺省字段从 config["llm"]
                            继承（给 agent 生成 skill + LLM 打分用，质量敏感）

    例：
      llm:
        base_url: "..."
        model: "doubao-seed-2-0-mini-260215"   # 所有默认走这个
        api_key: "..."
      llm_skill:
        model: "doubao-seed-2-0-pro-260215"    # agent + eval 换大模型
        # base_url / api_key 缺省 → 继承 llm.*
    """
    base_cfg = config.get("llm", {}) or {}
    if role in ("skill", "eval"):
        override_cfg = config.get("llm_skill", {}) or {}
        merged = {**base_cfg, **{k: v for k, v in override_cfg.items() if v}}
    else:
        merged = base_cfg

    if merged.get("base_url") and merged.get("model"):
        try:
            client = LLMClient.from_config(merged)
            logger.info(f"LLM[{role}]: {client}")
            return client
        except Exception as e:
            logger.warning(f"LLM[{role}] 初始化失败: {e}")
            return None
    return None
