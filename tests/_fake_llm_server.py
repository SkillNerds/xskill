"""Reusable in-process fake LLM HTTP server for E2E tests.

Serves three protocols on the same port:

- ``POST /v1/messages`` -- Anthropic Messages API (consumed by the real
  ``claude`` CLI when ``ANTHROPIC_BASE_URL`` points at us).
- ``POST /chat/completions`` -- OpenAI-compatible chat completion (consumed
  by xskill's ``LLMClient`` via the openai SDK).
- ``POST /embeddings/multimodal`` -- ARK-style multimodal embedding endpoint
  (consumed by xskill's ``EmbedClient``).

Behavior is intent-driven: the caller installs ``Responder`` objects keyed
by route, and each request is dispatched to the first responder whose
``match(body)`` returns True. The default responder for each route is a
benign acknowledgement so the server never hangs an unknown caller.

Every request (path, headers, parsed body) is appended to ``server.requests``
for assertions.

Underscore-prefixed filename so pytest doesn't try to collect tests here.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


# ─────────────────────────────────────────────────────────────────
# Responder primitives
# ─────────────────────────────────────────────────────────────────

@dataclass
class Responder:
    """One matcher + response pair.

    ``match(body)`` decides whether this responder handles the request.
    ``build(body)`` returns the JSON response dict. Both receive the parsed
    request body. If a responder is single-use, set ``one_shot=True`` and
    the server will pop it after the first hit.

    Special markers in build() return dict:
    - ``__status``: int — override HTTP status (default 200)
    - ``__headers``: dict — extra response headers
    - ``__stream``: True + ``__sse_body``: str — return SSE stream instead of JSON
    """
    name: str
    match: Callable[[dict], bool]
    build: Callable[[dict], dict]
    one_shot: bool = False


@dataclass
class RateLimitResponder:
    """包装 inner Responder,自带 token bucket 模拟 provider 限流。

    超额时按 over_limit_mode 返回不同形态:
    - 'http_429'   → 标准 OpenAI 429 + Retry-After header
    - 'http_503'   → vLLM OOM 形态(500/503)
    - 'body_error' → OneAPI 风格 200 + body 含 error 字段
    """
    name: str
    inner: Responder
    rpm: int
    over_limit_mode: str = "http_429"  # "http_429" | "http_503" | "body_error"
    one_shot: bool = False

    def __post_init__(self):
        self._requests_in_window: list = []
        self._lock = threading.Lock()

    @property
    def match(self):
        return self.inner.match

    def build(self, body: dict) -> dict:
        """返回 inner.build(body) 或 over-limit payload。"""
        now = time.time()
        with self._lock:
            self._requests_in_window = [
                t for t in self._requests_in_window if now - t < 60
            ]
            if len(self._requests_in_window) >= self.rpm:
                return self._over_limit_payload()
            self._requests_in_window.append(now)
        return self.inner.build(body)

    def _over_limit_payload(self) -> dict:
        if self.over_limit_mode == "http_429":
            return {
                "__status": 429,
                "__headers": {"Retry-After": "2"},
                "error": {"message": "rate limit exceeded", "type": "rate_limit"},
            }
        if self.over_limit_mode == "http_503":
            return {"__status": 503, "error": {"message": "service unavailable"}}
        return {"error": {"message": "rate limit", "code": "RATE_LIMIT"}}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ─────────────────────────────────────────────────────────────────
# FakeLLMServer
# ─────────────────────────────────────────────────────────────────

class FakeLLMServer:
    def __init__(self, host: str = "127.0.0.1", port: int | None = None):
        self.host = host
        self.port: int = port if port is not None else _free_port()
        self.requests: list[dict[str, Any]] = []
        self.requests_lock = threading.Lock()
        self.responders: dict[str, list[Responder]] = {
            "/v1/messages": [],
            "/chat/completions": [],
            "/embeddings/multimodal": [],
            "/embeddings": [],
        }
        self._app = self._build_app()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    # ── lifecycle ─────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 5.0) -> None:
        config = uvicorn.Config(
            self._app, host=self.host, port=self.port, log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, daemon=True, name="fake-llm-server",
        )
        self._thread.start()
        # Wait until the server is accepting connections.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"FakeLLMServer failed to start within {timeout}s")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> "FakeLLMServer":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ── responder registration ───────────────────────────────────

    def add_responder(self, path: str, responder: Responder) -> None:
        if path not in self.responders:
            raise ValueError(f"unknown path: {path}")
        self.responders[path].append(responder)

    def reset(self) -> None:
        """Clear request log and all custom responders (keep defaults inline)."""
        with self.requests_lock:
            self.requests.clear()
        for path in self.responders:
            self.responders[path].clear()

    # ── introspection helpers ────────────────────────────────────

    def requests_for(self, path: str) -> list[dict[str, Any]]:
        with self.requests_lock:
            return [r for r in self.requests if r["path"] == path]

    # ── app construction ─────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/v1/health")
        async def health():
            return {"ok": True}

        @app.post("/v1/messages")
        async def messages(request: Request):
            return await self._handle(request, "/v1/messages", self._default_anthropic)

        @app.post("/chat/completions")
        async def chat(request: Request):
            return await self._handle(request, "/chat/completions", self._default_openai)

        @app.post("/embeddings/multimodal")
        async def embed(request: Request):
            return await self._handle(request, "/embeddings/multimodal", self._default_embedding_multimodal)

        @app.post("/embeddings")
        async def embed_openai(request: Request):
            # cursor 引入的 ARK 分流：text 模型走 /embeddings (openai 风格，
            # data 是 list)，vision 模型走 /embeddings/multimodal (data 是
            # dict)。fake server 两路都得给，且响应 shape 不同。
            return await self._handle(request, "/embeddings", self._default_embedding_openai)

        return app

    async def _handle(self, request: Request, path: str, default_builder):
        raw = await request.body()
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", errors="replace")}
        headers = {k: v for k, v in request.headers.items()}
        rec = {"path": path, "headers": headers, "body": body, "matched_by": None}
        with self.requests_lock:
            self.requests.append(rec)
        # Dispatch
        for resp in list(self.responders[path]):
            try:
                if resp.match(body):
                    payload = resp.build(body)
                    if resp.one_shot:
                        self.responders[path].remove(resp)
                    rec["matched_by"] = resp.name
                    return self._render_payload(payload)
            except Exception as e:  # pragma: no cover -- surface in tests
                rec["matched_by"] = f"ERROR in {resp.name}: {e}"
                return JSONResponse(
                    {"error": f"responder {resp.name} raised: {e}"}, status_code=500,
                )
        rec["matched_by"] = "(default)"
        return JSONResponse(default_builder(body))

    @staticmethod
    def _render_payload(payload):
        """把 Responder.build 返回 dict 转 FastAPI Response,
        识别 __status / __headers / __stream / __sse_body 特殊标记。"""
        if not isinstance(payload, dict):
            return JSONResponse(payload)
        # Streaming branch
        if payload.get("__stream"):
            sse_body = payload.get("__sse_body", "")
            return StreamingResponse(iter([sse_body]), media_type="text/event-stream")
        # JSON branch with optional status/headers
        status = payload.pop("__status", 200)
        headers = payload.pop("__headers", None)
        return JSONResponse(payload, status_code=status, headers=headers)

    # ── default response builders ────────────────────────────────

    @staticmethod
    def _default_anthropic(body: dict) -> dict:
        model = body.get("model", "claude-fake")
        return {
            "id": "msg_fake_default",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [
                {"type": "text", "text": "(fake) default anthropic response."},
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }

    @staticmethod
    def _default_openai(body: dict) -> dict:
        model = body.get("model", "fake-model")
        return {
            "id": "chatcmpl-fake-default",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "(fake) default openai response.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    @staticmethod
    def _make_vec(body: dict) -> list[float]:
        """Deterministic 8-dim vector for the input text，跨 path 共用。"""
        text = ""
        inp = body.get("input")
        if isinstance(inp, list) and inp and isinstance(inp[0], dict):
            text = inp[0].get("text", "") or ""
        elif isinstance(inp, str):
            text = inp
        seed = sum(ord(c) for c in text[:64])
        return [((seed + i * 7) % 97) / 97.0 for i in range(8)]

    @classmethod
    def _default_embedding_multimodal(cls, body: dict) -> dict:
        """ARK multimodal `/embeddings/multimodal`：data 是 dict。"""
        return {
            "model": body.get("model", "fake-embed"),
            "data": {"embedding": cls._make_vec(body)},
        }

    @classmethod
    def _default_embedding_openai(cls, body: dict) -> dict:
        """OpenAI-compatible `/embeddings`：data 是 list of {embedding}。"""
        return {
            "model": body.get("model", "fake-embed"),
            "data": [{"embedding": cls._make_vec(body), "index": 0}],
        }

    # 兼容 backwards：老测试可能直接调 _default_embedding(body)
    _default_embedding = _default_embedding_multimodal


# ─────────────────────────────────────────────────────────────────
# Convenience builders for our specific E2E flows
# ─────────────────────────────────────────────────────────────────

def make_anthropic_text_response(text: str, model: str = "claude-fake") -> dict:
    return {
        "id": "msg_fake_text",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }


def make_anthropic_tool_use_response(
    tool_name: str,
    tool_input: dict,
    model: str = "claude-fake",
    tool_use_id: str = "toolu_01ABCDEF0123456789ABCD",
) -> dict:
    """Single-block tool_use response.

    Empirically the Claude Code CLI silently rejects responses where a
    ``tool_use`` block is preceded by a ``text`` block and re-prompts as
    if nothing happened — so this helper emits *only* the tool_use block.
    The CLI then runs the tool and posts the ``tool_result`` back; the
    next responder in line is expected to answer that follow-up turn.
    """
    return {
        "id": "msg_fake_toolcall",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }


def make_openai_chat_response(text: str, model: str = "fake-model") -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


def make_openai_tool_call_response(
    tool_name: str,
    arguments: dict,
    model: str = "fake-model",
    tool_call_id: str = "call_fake_0001",
) -> dict:
    return make_openai_tool_calls_response(
        [(tool_name, arguments, tool_call_id)], model=model)


def make_openai_tool_calls_response(
    calls: list[tuple[str, dict, str | None]],
    model: str = "fake-model",
) -> dict:
    tool_calls = []
    for idx, (tool_name, arguments, tool_call_id) in enumerate(calls, 1):
        tool_calls.append({
            "id": tool_call_id or f"call_fake_{idx:04d}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        })
    return {
        "id": "chatcmpl-fake-toolcall",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


# ─────────────────────────────────────────────────────────────────
# CLI entry — docker_e2e rig 把本文件当独立可执行 python 脚本启动。
# 用法: python _fake_llm_server.py <port>
# 仅启默认 responders（identity 兜底），scenario 不需要更复杂逻辑。
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    port_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 19999
    srv = FakeLLMServer(host="0.0.0.0", port=port_arg)
    srv.start(timeout=10.0)
    print(f"[fake-llm] listening on http://0.0.0.0:{port_arg}", flush=True)
    # 主线程 join 让 process 不退出
    try:
        srv._thread.join()
    except KeyboardInterrupt:
        srv.stop()
