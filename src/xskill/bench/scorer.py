from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

from xskill.bench.constants import OFFICEQA_SCORER


def _load_officeqa_evaluate() -> Callable[[str, str], dict[str, Any]]:
    try:
        module = importlib.import_module("skillopt.envs.officeqa.evaluator")
        fn = getattr(module, "evaluate")
        if "reward" in getattr(module, "__file__", "").lower():
            raise RuntimeError("refused to load reward.py as OfficeQA scorer")
        return fn
    except ImportError:
        pass
    candidates = [
        Path("/home/admin/leaderboard/vendor/SkillOpt/skillopt/envs/officeqa/evaluator.py"),
        Path("/home/admin/leaderboard/leadeboard_apps/vendor/SkillOpt/skillopt/envs/officeqa/evaluator.py"),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        if path.name.lower() == "reward.py":
            continue
        spec = importlib.util.spec_from_file_location("_xskill_bench_officeqa_eval", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.evaluate
    raise RuntimeError(
        "SkillOpt OfficeQA evaluate() not found; install SkillOpt or keep the vendor tree"
    )


def officeqa_evaluate(prediction: str, gold: str) -> dict[str, Any]:
    fn = _load_officeqa_evaluate()
    result = fn(str(prediction), str(gold))
    if "reward.py" in OFFICEQA_SCORER["id"]:
        raise RuntimeError("OfficeQA scorer id must not point at reward.py")
    return result


def score_item(
    benchmark: str,
    *,
    pred: Any,
    gold: Any,
    status_hint: str | None = None,
) -> tuple[str, bool]:
    if status_hint == "timeout":
        return "timeout", False
    if status_hint == "invalid":
        return "invalid", False
    if status_hint == "infra_error":
        return "infra_error", False
    if benchmark == "officeqa" and gold is not None and pred is not None and status_hint != "fail":
        result = officeqa_evaluate(str(pred), str(gold))
        passed = float(result.get("em") or 0.0) >= 1.0
        return ("pass" if passed else "fail"), passed
    if status_hint in {"pass", "fail"}:
        return status_hint, status_hint == "pass"
    if benchmark in {"spreadsheet", "alfworld"}:
        passed = bool(pred) if pred in (True, False, 0, 1) else str(pred).upper() in {"PASS", "SUCCESS", "WON", "1", "TRUE"}
        if gold is not None and not isinstance(gold, bool):
            passed = str(pred) == str(gold)
        return ("pass" if passed else "fail"), passed
    passed = str(pred) == str(gold)
    return ("pass" if passed else "fail"), passed


def assert_no_reward_imported() -> None:
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", "") or ""
        if "reward.py" in file.replace("\\", "/").lower() and "officeqa" in file.lower():
            raise RuntimeError(f"OfficeQA reward.py was imported: {name} {file}")
