"""Tail kernel-host stdio for the dashboard live console."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Iterator

BACKLOG_BYTES = 64 * 1024
DEFAULT_BACKLOG_LINES = 200
DEFAULT_POLL_SECONDS = 0.4


def sse_pack(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _strip_cr(line: str) -> str:
    return line[:-1] if line.endswith("\r") else line


def _split_complete_lines(text: str) -> tuple[list[str], str]:
    """Split decoded text into complete lines and the leftover partial line."""
    if not text:
        return [], ""
    if text.endswith("\n"):
        return [_strip_cr(part) for part in text.split("\n")[:-1]], ""
    last_nl = text.rfind("\n")
    if last_nl < 0:
        return [], text
    complete = text[: last_nl + 1]
    return [_strip_cr(part) for part in complete.split("\n")[:-1]], text[last_nl + 1 :]


def iter_kernel_console_sse(
    path: Path,
    *,
    backlog: int = DEFAULT_BACKLOG_LINES,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_events: int | None = None,
    stop: Callable[[], bool] | None = None,
) -> Iterator[str]:
    """Yield SSE chunks: meta, optional status, then ``log`` lines as they appear."""
    emitted = 0

    def emit(payload: dict) -> str:
        nonlocal emitted
        emitted += 1
        return sse_pack(payload)

    yield emit({"t": "meta", "path": str(path)})
    if max_events is not None and emitted >= max_events:
        return

    offset = 0
    buf = ""
    primed = False
    idle_ticks = 0

    while True:
        if stop is not None and stop():
            return
        if not path.is_file():
            if not primed:
                primed = True
                yield emit({
                    "t": "status",
                    "line": "日志文件尚未生成，内核进程启动并打印后会出现在这里。",
                })
                if max_events is not None and emitted >= max_events:
                    return
            idle_ticks += 1
            if idle_ticks % 25 == 0:
                yield ": ping\n\n"
            time.sleep(poll_seconds)
            continue

        size = path.stat().st_size
        if size < offset:
            offset = 0
            buf = ""
            yield emit({"t": "status", "line": "日志文件被截断或轮转，从开头继续。"})
            if max_events is not None and emitted >= max_events:
                return

        with path.open("rb") as handle:
            if not primed:
                primed = True
                start = max(0, size - BACKLOG_BYTES)
                handle.seek(start)
                raw = handle.read()
                offset = handle.tell()
                text = raw.decode("utf-8", errors="replace")
                if start > 0:
                    newline = text.find("\n")
                    if newline >= 0:
                        text = text[newline + 1 :]
                lines, buf = _split_complete_lines(text)
                for line in lines[-backlog:]:
                    yield emit({"t": "log", "line": line})
                    if max_events is not None and emitted >= max_events:
                        return
                continue

            handle.seek(offset)
            raw = handle.read()
            offset = handle.tell()

        if not raw:
            idle_ticks += 1
            if idle_ticks % 25 == 0:
                yield ": ping\n\n"
            time.sleep(poll_seconds)
            continue

        idle_ticks = 0
        buf += raw.decode("utf-8", errors="replace")
        lines, buf = _split_complete_lines(buf)
        for line in lines:
            yield emit({"t": "log", "line": line})
            if max_events is not None and emitted >= max_events:
                return
