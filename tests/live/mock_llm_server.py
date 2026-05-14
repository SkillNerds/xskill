"""Mock LLM HTTP server for live-agent E2E tests.

Speaks just enough of two protocols to let real codex/opencode processes
complete one "user sends a prompt, LLM replies with a short fixed text"
turn without hitting the public internet:

* **Codex Responses API** (``POST /v1/responses``): SSE stream of
  ``response.created`` → ``response.output_item.done`` (assistant message)
  → ``response.completed``. Each event prefixed with ``event: <kind>\\n``
  per codex's own test harness (see
  ``~/learn/codex/codex-rs/core/tests/common/responses.rs::sse``).
* **OpenAI Chat Completions** (``POST /v1/chat/completions``): SSE chunks
  of ``data: {"choices":[{"delta":{"content":"..."}}]}`` terminated by
  ``data: [DONE]``, exactly what
  ``@ai-sdk/openai-compatible`` (opencode's provider SDK) expects.
* **Codex models manifest** (``GET /v1/models``): codex queries this on
  startup before any request; empty list is OK.

The server is intentionally minimal — it does NOT implement tool calling,
function output, or multi-turn state. It only proves that codex / opencode
can establish a network conversation, get a usable reply, write it into
their session storage (JSONL / SQLite), and exit cleanly so xskill's
ingester can pick the session up.

Standalone mode::

    python tests/live/mock_llm_server.py 8765 &
    curl -s http://localhost:8765/v1/health

Test mode (via pytest fixture in ``test_*_live.py``)::

    with MockLLMServer() as server:   # random port, in-process
        ...                            # codex/opencode point at server.base_url

Design notes:

* Uses ``fastapi`` + ``uvicorn`` (already a dev dep, used by
  ``tests/_fake_llm_server.py``)
* In-process when used as context manager; subprocess-spawnable for
  out-of-band runs (CI / local debugging)
* Echoes the user prompt in the reply to make ingester assertions
  deterministic — codex/opencode write the assistant message verbatim
  into session JSONL/SQLite, so the test can grep for it
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


# Fixed reply text used by both endpoints. Tests assert this lands in
# the session storage round-trip, so it must be unique enough to grep.
MOCK_REPLY_TEXT = "Hello from mock LLM"


def _free_port() -> int:
    """Pick a random unused TCP port on 127.0.0.1."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ─────────────────────────────────────────────────────────────────
# Codex Responses API SSE event builders
# ─────────────────────────────────────────────────────────────────
#
# Format (mirrors codex's own test harness sse() helper):
#
#     event: response.created
#     data: {"type":"response.created","response":{"id":"resp-1"}}
#
#     event: response.output_item.done
#     data: {...}
#
#     event: response.completed
#     data: {...}
#
# Each event terminated by a blank line. The ``event:`` line is required —
# codex's SSE parser dispatches on it.


def _sse_event(kind: str, payload: dict[str, Any]) -> str:
    """Serialize one SSE event in codex's wire format."""
    body = json.dumps(payload, separators=(",", ":"))
    return f"event: {kind}\ndata: {body}\n\n"


def _build_codex_sse_stream(response_id: str, reply_text: str) -> str:
    """Produce the full SSE body for a single completed Responses turn.

    Three events suffice:

    1. ``response.created`` — claims the response id so the client can correlate
    2. ``response.output_item.done`` — delivers the assistant message in one
       shot (we don't bother streaming token-by-token; codex's session writer
       only cares about the final message item)
    3. ``response.completed`` — terminal event with usage stats
    """
    msg_id = f"msg_{response_id}"
    return (
        _sse_event(
            "response.created",
            {"type": "response.created", "response": {"id": response_id}},
        )
        + _sse_event(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": msg_id,
                    "content": [{"type": "output_text", "text": reply_text}],
                },
            },
        )
        + _sse_event(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "usage": {
                        "input_tokens": 1,
                        "input_tokens_details": None,
                        "output_tokens": 1,
                        "output_tokens_details": None,
                        "total_tokens": 2,
                    },
                },
            },
        )
    )


# ─────────────────────────────────────────────────────────────────
# OpenAI chat-completions SSE builder
# ─────────────────────────────────────────────────────────────────
#
# Format:
#
#     data: {"id":"...","object":"chat.completion.chunk","choices":[
#         {"index":0,"delta":{"role":"assistant","content":""}}]}
#
#     data: {"choices":[{"index":0,"delta":{"content":"Hello"}}]}
#     ...
#     data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
#     data: [DONE]


def _build_openai_sse_stream(model: str, reply_text: str) -> str:
    """Produce SSE for one full chat completion turn.

    Two chunks: role+content opener, then a final chunk with
    ``finish_reason: stop``. Single content delta is enough for
    opencode/openai-compatible providers.
    """
    completion_id = f"chatcmpl-mock-{int(time.time() * 1000)}"
    created = int(time.time())
    chunk1 = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": reply_text},
                "finish_reason": None,
            }
        ],
    }
    chunk2 = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return (
        f"data: {json.dumps(chunk1, separators=(',', ':'))}\n\n"
        f"data: {json.dumps(chunk2, separators=(',', ':'))}\n\n"
        "data: [DONE]\n\n"
    )


def _build_openai_nonstream_response(model: str, reply_text: str) -> dict[str, Any]:
    """Non-streaming chat completion (when client sends ``stream: false``)."""
    return {
        "id": f"chatcmpl-mock-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# ─────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────


def build_app(reply_text: str = MOCK_REPLY_TEXT) -> FastAPI:
    """Build the FastAPI app. Factored so unit tests can swap reply text."""
    app = FastAPI()

    # Request log — useful for assertions when used as in-process fixture.
    request_log: list[dict[str, Any]] = []
    app.state.request_log = request_log

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "reply_text": reply_text}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        """Codex queries this on startup. Empty list satisfies the schema."""
        return {"data": [], "object": "list"}

    # ── Codex Responses API ───────────────────────────────────────
    @app.post("/v1/responses")
    async def responses(request: Request) -> StreamingResponse:
        body_raw = await request.body()
        try:
            body = json.loads(body_raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"_raw": body_raw.decode("utf-8", errors="replace")}
        request_log.append({"path": "/v1/responses", "body": body})

        response_id = f"resp-mock-{int(time.time() * 1000)}"
        sse_body = _build_codex_sse_stream(response_id, reply_text)

        async def _gen() -> AsyncIterator[bytes]:
            yield sse_body.encode("utf-8")

        return StreamingResponse(_gen(), media_type="text/event-stream")

    # ── OpenAI chat completions ──────────────────────────────────
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body_raw = await request.body()
        try:
            body = json.loads(body_raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"_raw": body_raw.decode("utf-8", errors="replace")}
        request_log.append({"path": "/v1/chat/completions", "body": body})

        model = body.get("model", "mock-model")
        stream = bool(body.get("stream", False))

        if stream:
            sse_body = _build_openai_sse_stream(model, reply_text)

            async def _gen() -> AsyncIterator[bytes]:
                yield sse_body.encode("utf-8")

            return StreamingResponse(_gen(), media_type="text/event-stream")

        return JSONResponse(_build_openai_nonstream_response(model, reply_text))

    return app


# ─────────────────────────────────────────────────────────────────
# In-process server wrapper (for use as pytest fixture)
# ─────────────────────────────────────────────────────────────────


class MockLLMServer:
    """In-process mock LLM server runnable as ``with MockLLMServer() as s: ...``.

    Picks a random free port on construction (unless ``port`` provided),
    starts uvicorn on a background daemon thread on ``__enter__``, and
    shuts it down cleanly on ``__exit__``.

    Use ``server.base_url`` (e.g. ``http://127.0.0.1:54321``) as the
    OpenAI-compat base URL for codex/opencode under test.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int | None = None,
        reply_text: str = MOCK_REPLY_TEXT,
    ):
        self.host = host
        self.port: int = port if port is not None else _free_port()
        self.reply_text = reply_text
        self._app: FastAPI = build_app(reply_text=reply_text)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def request_log(self) -> list[dict[str, Any]]:
        """All recorded requests since start (thread-local but FastAPI handlers
        run in the same event loop, so this is safe for read-after-stop assertions)."""
        return self._app.state.request_log

    def start(self, timeout: float = 5.0) -> None:
        config = uvicorn.Config(
            self._app, host=self.host, port=self.port,
            log_level="warning", loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, daemon=True, name="mock-llm-server",
        )
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"MockLLMServer failed to start within {timeout}s")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> "MockLLMServer":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()


# ─────────────────────────────────────────────────────────────────
# Standalone runner (for CI background process + local debugging)
# ─────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    """``python tests/live/mock_llm_server.py [PORT]`` — block forever serving."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", type=int, default=8765,
                        help="port to bind (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="host to bind (default: 127.0.0.1)")
    parser.add_argument("--reply-text", default=MOCK_REPLY_TEXT,
                        help=f"text to return as the assistant reply "
                             f"(default: {MOCK_REPLY_TEXT!r})")
    args = parser.parse_args(argv)

    app = build_app(reply_text=args.reply_text)
    config = uvicorn.Config(
        app, host=args.host, port=args.port, log_level="info", loop="asyncio",
    )
    server = uvicorn.Server(config)
    print(f"[mock-llm] listening on http://{args.host}:{args.port}", flush=True)
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
