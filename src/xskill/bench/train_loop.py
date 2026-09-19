"""冻结 skill 的文件读写小工具。

训练本身不在这里。SkillOpt 走官方 scripts/train.py（见 skillopt_train.py），
xskill 走官方训练镜像（见 xskill_image.py）。这个模块以前有一套「跑一轮再整篇改写」
的循环，那不是任何一方的官方训练，已经移除。
"""

from __future__ import annotations

from pathlib import Path


def skill_markdown_path(skill_out: Path, algo: str) -> Path:
    if algo == "skillopt":
        path = Path(skill_out) / "SKILL.md"
        if not path.is_file():
            raise FileNotFoundError(f"skillopt skill missing: {path}")
        return path
    found = sorted(Path(skill_out).rglob("SKILL.md"))
    if not found:
        raise FileNotFoundError(f"no SKILL.md under {skill_out}")
    return found[0]


def read_skill_text(skill_out: Path, algo: str) -> str:
    return skill_markdown_path(skill_out, algo).read_text(encoding="utf-8")


def write_skill_text(skill_out: Path, algo: str, text: str) -> None:
    path = skill_markdown_path(skill_out, algo)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
