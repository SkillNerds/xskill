"""算法团队已有的核心能力。

这里故意与 XSkill SDK 分开：kernel.py 只做接口适配，真实算法仍可由独立包维护。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Atom:
    trajectory_id: str
    intent: str
    body: str


class SimpleSplitter:
    def split(self, trajectory_id: str, text: str) -> list[Atom]:
        parts = re.split(r"(?m)^## User\b[^\n]*\n?", text)
        chunks = [chunk.strip() for chunk in parts[1:] if chunk.strip()]
        if not chunks and text.strip():
            chunks = [text.strip()]
        return [
            Atom(
                trajectory_id=trajectory_id,
                intent=(chunk.splitlines()[0] if chunk.splitlines() else "未命名任务")[:40],
                body=chunk,
            )
            for chunk in chunks
        ]


class KeywordClusterer:
    def cluster(self, atoms: list[Atom], *, min_cluster_size: int) -> dict[str, list[Atom]]:
        grouped: dict[str, list[Atom]] = {}
        for atom in atoms:
            words = re.findall(r"[A-Za-z_]{3,}|[\u4e00-\u9fff]{2,}", atom.intent.lower())
            key = Counter(words).most_common(1)[0][0] if words else "general"
            grouped.setdefault(key, []).append(atom)
        return {
            key: members
            for key, members in grouped.items()
            if len(members) >= min_cluster_size
        }


class MarkdownSkillWriter:
    def write(self, name: str, atoms: list[Atom]) -> str:
        examples = "\n".join(f"- {atom.intent}" for atom in atoms[:5])
        return (
            "---\n"
            f"name: {name}\n"
            f"description: 当用户处理 {name} 类任务时使用；由轨迹证据自动提炼。\n"
            "---\n\n"
            f"# {name}\n\n"
            "## 轨迹中反复出现的任务\n\n"
            f"{examples}\n"
        )
