from __future__ import annotations

import json
from pathlib import Path

from xskill.bench.paths import default_manifest


def load_manifest(benchmark: str, path: Path | None = None) -> dict:
    manifest_path = path or default_manifest(benchmark)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"split manifest not found: {manifest_path}")
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    if doc.get("benchmark") != benchmark:
        raise ValueError(f"manifest benchmark {doc.get('benchmark')!r} != {benchmark!r}")
    return doc


def uids_for_split(manifest: dict, split_name: str) -> list[str]:
    if split_name == "train_plus_val":
        return list(manifest["train"]) + list(manifest["val"])
    if split_name not in manifest:
        raise KeyError(f"unknown split {split_name!r}")
    return list(manifest[split_name])


def resolve_eval_uids(
    benchmark: str,
    split_name: str,
    *,
    manifest_path: Path | None = None,
    limit: int = 0,
) -> tuple[Path, list[str]]:
    path = manifest_path or default_manifest(benchmark)
    manifest = load_manifest(benchmark, path)
    uids = uids_for_split(manifest, split_name)
    if limit and limit < len(uids):
        uids = uids[:limit]
    return path, uids


def resolve_train_uids(
    benchmark: str,
    algo: str,
    *,
    manifest_path: Path | None = None,
    limit: int = 0,
) -> tuple[Path, list[str], str, bool]:
    path = manifest_path or default_manifest(benchmark)
    manifest = load_manifest(benchmark, path)
    if algo == "xskill":
        train = list(manifest["train"])
        val = list(manifest["val"])
        if limit:
            train = train[:limit]
            val = val[:limit]
        uids = train + val
        split_name = "train_plus_val"
        merge_val = True
    else:
        uids = uids_for_split(manifest, "train")
        split_name = "train"
        merge_val = False
        if limit and limit < len(uids):
            uids = uids[:limit]
    return path, uids, split_name, merge_val
