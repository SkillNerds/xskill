"""Public CLI is always live. Pytest sets XSKILL_BENCH_FAKE=1 for wiring."""

from __future__ import annotations

import os

FAKE_ENV = "XSKILL_BENCH_FAKE"


def resolve_executor(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if os.environ.get(FAKE_ENV) == "1":
        return "fake"
    return "live"
