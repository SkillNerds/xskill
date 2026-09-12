"""Keep bench running after the calling terminal or tee pipe dies."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import TextIO

# Unix 写到已关闭的管道是 EPIPE(32)。Windows 同一情况常报 EINVAL(22)。
_BROKEN_PIPE = {32}
if os.name == "nt":
    _BROKEN_PIPE.add(22)


def _is_broken_pipe(exc: OSError) -> bool:
    return getattr(exc, "errno", None) in _BROKEN_PIPE


def _write_text(handle: TextIO, text: str) -> None:
    try:
        handle.write(text)
        handle.flush()
        return
    except UnicodeEncodeError:
        pass
    encoding = getattr(handle, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    handle.write(safe)
    handle.flush()


def ignore_sigpipe() -> None:
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        return


def _redirect_stdout_devnull() -> None:
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, fd)
        finally:
            os.close(devnull)
    except OSError:
        return


def quiet_broken_stdout() -> None:
    """Stop interpreter shutdown from turning a dead pipe into exit 120."""
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        _redirect_stdout_devnull()
    except OSError as exc:
        if _is_broken_pipe(exc):
            _redirect_stdout_devnull()
        else:
            raise


def safe_write(text: str, stream: TextIO | None = None) -> None:
    line = text if text.endswith("\n") else f"{text}\n"
    handle = sys.stdout if stream is None else stream
    try:
        _write_text(handle, line)
    except BrokenPipeError:
        return
    except OSError as exc:
        if _is_broken_pipe(exc):
            return
        raise


class SafeTee:
    """Write to stdout and a run log. A dead terminal must not abort the run."""

    def __init__(self, *handles: TextIO | None) -> None:
        self.handles = [handle for handle in handles if handle is not None]

    def write(self, text: str) -> int:
        for handle in self.handles:
            try:
                _write_text(handle, text)
            except BrokenPipeError:
                continue
            except OSError as exc:
                if _is_broken_pipe(exc):
                    continue
                raise
        return len(text)

    def flush(self) -> None:
        for handle in self.handles:
            try:
                handle.flush()
            except BrokenPipeError:
                continue
            except OSError as exc:
                if _is_broken_pipe(exc):
                    continue
                raise


def open_run_console(output_dir: Path) -> tuple[SafeTee, TextIO]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_fp = (output_dir / "console.log").open("a", encoding="utf-8")
    return SafeTee(sys.stdout, log_fp), log_fp
