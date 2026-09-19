"""共享 LiteLLM 代理的地址、钥匙与按 run 建虚拟 key。不 import litellm。

两种记账通路：

- 能加请求头的客户端（我们自己起的 Claude Code、我们自己发的 Chat）走
  `X-Run-Id`、`X-Item-Id`、`x-litellm-tags`，代理按 `extra_spend_tag_headers`
  落进 `request_tags`，可以逐题对账。
- 加不了头的客户端（SkillOpt 官方 Chat 客户端写死了 default_headers；xskill 训练
  镜像里的 Claude Code 是 2.1.178，不认 `ANTHROPIC_CUSTOM_HEADERS`）走按 run 建的
  虚拟 key：spend 行里带 `api_key` 哈希和 `user_api_key_alias`，可以按 run 对账。
  key 级 tag 是 LiteLLM 企业版功能，这里不用。
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PROXY_URL = "http://127.0.0.1:4000"
DEFAULT_ENV_FILE = Path("/home/admin/litellm-proxy/.env")
ENV_FILE_VAR = "XSKILL_BENCH_LITELLM_ENV"
# 容器走 bridge 网络时用它回连宿主代理，需要 --add-host。
DOCKER_HOST_ALIAS = "host.docker.internal"
KEY_ALIAS_PREFIX = "xskill-bench"


def env_file() -> Path:
    return Path(os.environ.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE)


def load_proxy_env() -> dict[str, str]:
    path = env_file()
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        out[name.strip()] = value.strip().strip("'").strip('"')
    return out


def proxy_base_url(explicit: str | None = None) -> str:
    return (explicit or os.environ.get("LITELLM_BASE_URL") or DEFAULT_PROXY_URL).rstrip("/")


def master_key(explicit: str | None = None) -> str:
    """读 spend 日志、建虚拟 key 用的管理钥匙。"""
    if explicit:
        return explicit
    from_env = os.environ.get("LITELLM_MASTER_KEY")
    if from_env:
        return from_env
    return load_proxy_env().get("LITELLM_MASTER_KEY", "")


def container_base_url(url: str | None = None) -> str:
    """把宿主本地地址换成容器里能解析的别名。"""
    base = proxy_base_url(url)
    for local in ("127.0.0.1", "localhost", "0.0.0.0"):
        if f"//{local}:" in base or base.endswith(f"//{local}"):
            return base.replace(local, DOCKER_HOST_ALIAS, 1)
    return base


def docker_host_args() -> list[str]:
    return ["--add-host", f"{DOCKER_HOST_ALIAS}:host-gateway"]


def _request(
    path: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    payload: dict[str, Any] | None = None,
    method: str | None = None,
    timeout: float = 20.0,
) -> Any:
    base = proxy_base_url(base_url)
    key = master_key(api_key)
    if not key:
        raise RuntimeError("LiteLLM 管理操作需要 LITELLM_MASTER_KEY")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        base + path,
        data=data,
        headers=headers,
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def alive(base_url: str | None = None, timeout: float = 3.0) -> bool:
    url = proxy_base_url(base_url) + "/health/liveliness"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


@dataclass(frozen=True)
class RunKey:
    """一趟实验专用的虚拟 key。token 是 spend 行里的 api_key 哈希。"""

    key: str
    token: str
    alias: str

    @property
    def tagged(self) -> bool:
        return bool(self.token)


def run_key_alias(run_id: str, *, nonce: str | None = None) -> str:
    # 同名 alias 代理会拒（400）。run_id 是可复用的（train-officeqa-xskill 每次都一样），
    # 所以带一段时间戳，否则第二趟就建不出 key、整趟没法归账。
    if nonce is None:
        # 秒级还不够：同一趟里可能连建两把（训练一把、评测一把），再缀随机段。
        nonce = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    return f"{KEY_ALIAS_PREFIX}:{run_id}:{nonce}"[:120]


def create_run_key(
    run_id: str,
    *,
    models: list[str] | None = None,
    duration: str = "24h",
    base_url: str | None = None,
    api_key: str | None = None,
) -> RunKey:
    payload: dict[str, Any] = {
        "duration": duration,
        "key_alias": run_key_alias(run_id),
    }
    if models:
        payload["models"] = list(models)
    doc = _request("/key/generate", base_url=base_url, api_key=api_key, payload=payload)
    key = str(doc.get("key") or "")
    if not key:
        raise RuntimeError(f"LiteLLM 没给出虚拟 key: {doc}")
    return RunKey(key=key, token=str(doc.get("token") or ""), alias=str(doc.get("key_alias") or ""))


def try_create_run_key(run_id: str, **kwargs: Any) -> RunKey | None:
    """建不出来就返回 None：记账降级，但不许挡住做题。"""
    try:
        return create_run_key(run_id, **kwargs)
    except (
        RuntimeError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
    ):
        return None


def model_names(base_url: str | None = None, api_key: str | None = None) -> list[str]:
    doc = _request("/model/info", base_url=base_url, api_key=api_key)
    rows = doc.get("data") if isinstance(doc, dict) else doc
    out: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = row.get("model_name") or (row.get("litellm_params") or {}).get("model")
        if name:
            out.append(str(name))
    return out


def ensure_model(
    model_name: str,
    *,
    upstream_model: str,
    api_base: str,
    api_key_env: str,
    base_url: str | None = None,
    api_key: str | None = None,
) -> bool:
    """代理里没这条路由就加一条。返回是否新加。store_model_in_db 打开时不用重启。"""
    if model_name in model_names(base_url=base_url, api_key=api_key):
        return False
    _request(
        "/model/new",
        base_url=base_url,
        api_key=api_key,
        payload={
            "model_name": model_name,
            "litellm_params": {
                "model": upstream_model,
                "api_base": api_base,
                "api_key": f"os.environ/{api_key_env}",
            },
        },
    )
    return True
