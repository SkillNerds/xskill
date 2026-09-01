"""
Benchmark Data Loader Template
================================
Copy this file and implement ``load_split_items`` to load your benchmark
data. The loader is a :class:`skillopt.datasets.base.SplitDataLoader`
subclass — the base class handles both ``split_mode="split_dir"`` (read
an existing train/val/test layout) and ``split_mode="ratio"`` (build the
splits from a single raw file deterministically).

For a fully worked example see
``skillopt/envs/officeqa/dataloader.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

from skillopt.datasets.base import SplitDataLoader


def _normalize_item(raw: dict) -> dict:
    """
    Normalise one raw entry into the dict shape SkillOpt expects.

    The only **hard** requirement is ``"id"`` (str). Add whatever extra
    fields your :class:`TemplateBenchmarkEnv.rollout` needs.
    """
    return {
        "id": str(raw.get("uid") or raw.get("id") or ""),
        "question": str(raw.get("question") or raw.get("prompt") or ""),
        "ground_truth": str(raw.get("ground_truth") or raw.get("answer") or ""),
        "task_type": str(raw.get("category") or raw.get("task_type") or "template"),
        # ── add benchmark-specific keys here ──
    }


class TemplateBenchmarkLoader(SplitDataLoader):
    """
    Data loader for <Your Benchmark Name>.

    Subclass note: you usually only need to implement
    :meth:`load_split_items`. The base class drives ``setup(cfg)``,
    materialises ratio-mode splits, exposes ``train_items``,
    ``val_items``, ``test_items``, and builds ``BatchSpec`` objects on
    demand.

    If you want to support ``split_mode="ratio"`` (auto-split a single
    file into train/val/test), also implement
    :meth:`load_raw_items(data_path)` returning the full list of items.
    """

    def load_split_items(self, split_path: str) -> list[dict]:
        """Load all items for one split directory.

        ``split_path`` is e.g. ``data/your_benchmark/train/``. Return a
        list of dicts, each shaped like :func:`_normalize_item`'s output.
        """
        path = Path(split_path)

        json_files = sorted(path.glob("*.json"))
        if json_files:
            with json_files[0].open(encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, list):
                raise ValueError(
                    f"Expected JSON array at top level of {json_files[0]}"
                )
            return [_normalize_item(row) for row in payload]

        jsonl_files = sorted(path.glob("*.jsonl"))
        if jsonl_files:
            items: list[dict] = []
            with jsonl_files[0].open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    items.append(_normalize_item(json.loads(line)))
            return items

        raise FileNotFoundError(
            f"No .json or .jsonl file found in {split_path}"
        )

    # Optional — only needed if you intend to use ``split_mode='ratio'``.
    # def load_raw_items(self, data_path: str) -> list[dict]:
    #     ...
