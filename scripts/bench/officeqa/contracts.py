"""SkillOpt 划分名单，以及读写这些约定文件的小工具。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SPLIT_MANIFEST = (
    REPOSITORY_ROOT
    / "benchmarks"
    / "officeqa"
    / "manifests"
    / "officeqa_skillopt_id_split.json"
)
FULL_MANIFEST = (
    REPOSITORY_ROOT / "benchmarks" / "officeqa" / "manifests" / "officeqa_full.json"
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    """对 JSON 对象做稳定序列化后再算 SHA-256（末尾带换行）。"""
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256_text(payload + "\n")


def load_json(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_skillopt_split(
    path: Path | str | None = None,
) -> dict[str, Any]:
    """读取 SkillOpt 的 train/val/test 划分名单。"""
    return load_json(path or SPLIT_MANIFEST)


def uids_for_split(
    split_name: str,
    *,
    manifest: dict[str, Any] | None = None,
) -> list[str]:
    """返回某一段（train/val/test）或 full（三段并集）的 UID 列表。"""
    data = manifest or load_skillopt_split()
    if split_name == "full":
        items = []
        for name in ("train", "val", "test"):
            items.extend(data["splits"][name])
    else:
        items = data["splits"][split_name]
    return [str(item["uid"]) for item in items]


def required_keys_present(obj: dict[str, Any], required: list[str]) -> list[str]:
    """返回缺失的必填字段名；都在则返回空列表。"""
    return [key for key in required if key not in obj]
