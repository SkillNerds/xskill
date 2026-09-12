"""Official-split item lookup. Does not rewrite skillopt manifests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# First ID of each official SkillOpt-aligned split. Use --limit 1 on the
# official manifest; do not drop a mini file into benchmarks/*/manifests/.
SMOKE_IDS = {
    "officeqa": {"train": "UID0002", "val": "UID0001", "test": "UID0003"},
    "spreadsheet": {"train": "32438", "val": "45635", "test": "52532"},
    "alfworld": {"train": "train:0000", "val": "val:0000", "test": "test:0000"},
}

_DEFAULT_OFFICEQA_SPLIT = Path(
    "/home/admin/leaderboard/leadeboard_apps/officeqa_bench/data/officeqa_split"
)
_DEFAULT_OFFICEQA_DOCS = Path(
    "/home/admin/leaderboard/leadeboard_apps/officeqa_bench/data/officeqa_docs_official"
)
_DEFAULT_SPREADSHEET_DATA = Path(
    "/home/admin/leaderboard/leadeboard_apps/spreadsheets_bench/data/official/spreadsheetbench_verified_400"
)
_DEFAULT_ALFWORLD_IMAGE = "localhost:5000/l_creator/alfworld-eval:native-cc-20260904"
_DEFAULT_CLAUDE = Path("/home/admin/.nvm/versions/node/v24.14.1/bin/claude")
_DEFAULT_SKILLOPT_VENDOR = Path("/home/admin/leaderboard/vendor/SkillOpt")
_DEFAULT_SS_ROLLOUT = Path(
    "/home/admin/leaderboard/leadeboard_apps/spreadsheets_bench/algo_xskill/multi_turn_rollout.py"
)
_DEFAULT_SPLIT_ROOTS = {
    "officeqa": _DEFAULT_OFFICEQA_SPLIT,
    "spreadsheet": Path(
        "/home/admin/leaderboard/leadeboard_apps/spreadsheets_bench/data/spreadsheetbench_verified_400_split"
    ),
    "alfworld": Path(
        "/home/admin/leaderboard/leadeboard_apps/alfworld_bench/data/alfworld_path_split"
    ),
}
_SPLIT_ROOT_ENV = {
    "officeqa": "XSKILL_BENCH_OFFICEQA_SPLIT",
    "spreadsheet": "XSKILL_BENCH_SPREADSHEET_SPLIT",
    "alfworld": "XSKILL_BENCH_ALFWORLD_SPLIT",
}


def split_root(benchmark: str) -> Path:
    """官方 split 目录（items.json 那套），训练时原样喂给算法。"""
    try:
        env_key = _SPLIT_ROOT_ENV[benchmark]
    except KeyError as exc:
        raise ValueError(f"no official split root for {benchmark}") from exc
    return Path(os.environ.get(env_key, _DEFAULT_SPLIT_ROOTS[benchmark]))


def officeqa_split_root() -> Path:
    return split_root("officeqa")


def officeqa_docs_root() -> Path:
    return Path(os.environ.get("XSKILL_BENCH_OFFICEQA_DOCS", _DEFAULT_OFFICEQA_DOCS))


def spreadsheet_data_root() -> Path:
    return Path(os.environ.get("XSKILL_BENCH_SPREADSHEET_DATA", _DEFAULT_SPREADSHEET_DATA))


def alfworld_eval_image() -> str:
    return os.environ.get("XSKILL_BENCH_ALFWORLD_IMAGE", _DEFAULT_ALFWORLD_IMAGE)


def alfworld_rollout_py() -> Path:
    """评测用的 ALFWorld rollout。挂进镜像，覆盖镜像里写死 Skill 工具的那份。"""
    default = Path(__file__).resolve().parent / "alfworld_eval_rollout.py"
    return Path(os.environ.get("XSKILL_BENCH_ALFWORLD_ROLLOUT", default))


def claude_bin() -> str:
    return os.environ.get("XSKILL_BENCH_CLAUDE", str(_DEFAULT_CLAUDE))


def skillopt_vendor_root() -> Path:
    return Path(os.environ.get("XSKILL_BENCH_SKILLOPT_VENDOR", _DEFAULT_SKILLOPT_VENDOR))


def spreadsheet_rollout_py() -> Path:
    return Path(os.environ.get("XSKILL_BENCH_SS_ROLLOUT", _DEFAULT_SS_ROLLOUT))


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_officeqa_item(uid: str) -> dict[str, Any]:
    want = str(uid)
    for split in ("train", "val", "test"):
        path = officeqa_split_root() / split / "items.json"
        if not path.is_file():
            continue
        for item in load_json(path):
            if str(item.get("id") or item.get("uid")) == want:
                return item
    raise KeyError(f"OfficeQA item {uid!r} not in official officeqa_split")


def load_spreadsheet_item(uid: str) -> dict[str, Any]:
    path = spreadsheet_data_root() / "dataset.json"
    if not path.is_file():
        raise FileNotFoundError(f"SpreadsheetBench dataset.json missing: {path}")
    data = load_json(path)
    if isinstance(data, dict):
        data = data.get("data") or list(data.values())
    want = str(uid)
    for item in data:
        if str(item.get("id")) == want:
            return item
    raise KeyError(f"SpreadsheetBench item {uid!r} not in dataset.json")


def officeqa_gold(item: dict[str, Any]) -> str:
    return str(item.get("ground_truth") or (item.get("answers") or [""])[0] or "")
