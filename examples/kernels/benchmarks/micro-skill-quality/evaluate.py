"""Dependency-free example evaluator for the runnable demo kernel."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> None:
    skills_dir = Path(os.environ["XSKILL_EVAL_SKILLS_DIR"])
    result_path = Path(os.environ["XSKILL_EVAL_RESULT_PATH"])
    skill_files = sorted(skills_dir.glob("*/SKILL.md"))
    checks = []
    for skill_file in skill_files:
        body = skill_file.read_text(encoding="utf-8")
        checks.append(
            body.startswith("---\n")
            and "\ndescription:" in body
            and "## Source excerpt" in body
        )
    passed = sum(checks)
    total = len(checks)
    score = round(passed / total * 100.0, 4) if total else 0.0
    result_path.write_text(json.dumps({
        "schema_version": 1,
        "metrics": [{
            "id": "micro-skill-structure",
            "dataset": "micro-skill-quality",
            "split": "validation",
            "score": score,
            "passed": passed,
            "total": total,
            "source": "example-evaluator",
        }],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
