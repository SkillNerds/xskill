from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xskill.bench.scorer import score_item
from xskill.bench.usage import missing_usage


@dataclass
class ItemOutcome:
    uid: str
    pred_answer: Any
    gold_answer: Any
    status: str
    is_correct: bool
    latency_ms: float
    summary_text: str = ""


def _load_json_map(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"expected object in {path}")
    return doc


class FakeExecutor:
    """Deterministic stand-in so CLI and artifacts can be tested without a model."""

    def __init__(
        self,
        *,
        benchmark: str,
        fake_script: Path | None = None,
        gold: dict[str, Any] | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.script = _load_json_map(fake_script)
        self.gold = gold or {}

    def run_item(self, uid: str, index: int) -> ItemOutcome:
        started = time.perf_counter()
        spec = self.script.get(uid) or {}
        if not isinstance(spec, dict):
            spec = {"pred": spec}
        gold = spec.get("gold", self.gold.get(uid))
        hint = spec.get("status")
        if hint is None and uid not in self.script:
            hint = ("pass", "fail", "timeout")[index % 3]
        pred = spec.get("pred", spec.get("pred_answer"))
        if pred is None:
            if hint == "pass" and gold is not None:
                pred = gold
            elif hint == "pass":
                pred = "PASS"
            elif hint == "timeout":
                pred = ""
            else:
                pred = "WRONG"
        if hint == "timeout":
            status, correct = "timeout", False
        else:
            status, correct = score_item(self.benchmark, pred=pred, gold=gold, status_hint=hint)
        return ItemOutcome(
            uid=uid,
            pred_answer=pred,
            gold_answer=gold,
            status=status,
            is_correct=correct,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            summary_text=str(spec.get("summary_text") or uid),
        )


class LocalExecutor:
    """Score-only local path. Public CLI drives Claude; this scores a predictions file."""

    def __init__(
        self,
        *,
        benchmark: str,
        predictions: Path | None = None,
        gold: dict[str, Any] | None = None,
    ) -> None:
        self.benchmark = benchmark
        self.predictions = _load_json_map(predictions) if predictions else {}
        self.gold = gold or {}
        if not self.predictions:
            raise RuntimeError(
                "local executor needs --predictions-json to score without a live model; "
                "wiring tests set XSKILL_BENCH_FAKE=1"
            )

    def run_item(self, uid: str, index: int) -> ItemOutcome:
        started = time.perf_counter()
        spec = self.predictions.get(uid) or {}
        if not isinstance(spec, dict):
            spec = {"pred": spec}
        pred = spec.get("pred", spec.get("pred_answer"))
        gold = spec.get("gold", self.gold.get(uid))
        hint = spec.get("status")
        status, correct = score_item(self.benchmark, pred=pred, gold=gold, status_hint=hint)
        return ItemOutcome(
            uid=uid,
            pred_answer=pred,
            gold_answer=gold,
            status=status,
            is_correct=correct,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            summary_text=str(spec.get("summary_text") or uid),
        )


def make_executor(
    name: str,
    *,
    benchmark: str,
    fake_script: Path | None = None,
    predictions: Path | None = None,
    gold: dict[str, Any] | None = None,
    **live_kw: Any,
):
    if name == "fake":
        return FakeExecutor(benchmark=benchmark, fake_script=fake_script, gold=gold)
    if name == "local":
        return LocalExecutor(benchmark=benchmark, predictions=predictions, gold=gold)
    if name == "live":
        from xskill.bench.live import LiveExecutor

        return LiveExecutor(benchmark=benchmark, gold=gold, **live_kw)
    raise ValueError(f"unknown executor {name!r}")


def usage_for_outcome() -> dict[str, Any]:
    return missing_usage()
