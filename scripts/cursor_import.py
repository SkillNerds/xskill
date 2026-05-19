#!/usr/bin/env python3
"""Import Cursor agent-transcripts (*.jsonl) into xskill watch dir as traj_*.md."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from xskill.adapters import submit_trajectory


def _jsonl_to_markdown(jsonl_path: Path) -> str:
    lines: list[str] = [
        "# Cursor Agent Trajectory",
        "",
        f"**source_file**: {jsonl_path}",
        "",
    ]
    for raw in jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        ev = json.loads(raw)
        role = ev.get("role", "unknown")
        msg = ev.get("message") or {}
        parts = msg.get("content") or []
        chunks: list[str] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and p.get("text"):
                chunks.append(str(p["text"]))
            elif p.get("type") == "tool_use":
                name = p.get("name", "tool")
                chunks.append(f"[tool_use: {name}]")
        body = "\n".join(chunks).strip()
        if not body:
            continue
        lines.append(f"## {str(role).capitalize()}")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--src",
        type=Path,
        default=Path.home()
        / ".cursor/projects/c-yzj-entrepreneurship-XSKILL-xskill/agent-transcripts",
        help="Cursor agent-transcripts root (searched recursively for *.jsonl)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path.home() / ".xskill/cursor_import",
        help="xskill watch directory (traj_*.md output)",
    )
    args = p.parse_args()
    src = args.src.expanduser().resolve()
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    jsonls = sorted(src.rglob("*.jsonl"))
    if not jsonls:
        print(f"no *.jsonl under {src}", file=sys.stderr)
        return 1

    for jsonl in jsonls:
        md = _jsonl_to_markdown(jsonl)
        sid = jsonl.stem
        result = submit_trajectory(
            content=md,
            format="markdown",
            metadata={
                "source": "cursor",
                "ecosystem": "cursor",
                "session_id": sid,
                "source_jsonl": str(jsonl),
            },
            traj_id=f"traj_cursor_{sid[:8]}",
            traj_dir=out,
        )
        print(f"imported {jsonl.name} -> {result['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
