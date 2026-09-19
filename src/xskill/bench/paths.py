from __future__ import annotations

import subprocess
from pathlib import Path


def repo_root() -> Path:
    here = Path(__file__).resolve()
    candidates = [here.parents[3], Path.cwd()]
    for candidate in candidates:
        if (candidate / "benchmarks" / "validate.py").is_file():
            return candidate
    return Path.cwd()


def benchmarks_root(root: Path | None = None) -> Path:
    return (root or repo_root()) / "benchmarks"


def default_manifest(benchmark: str, root: Path | None = None) -> Path:
    return benchmarks_root(root) / benchmark / "manifests" / f"{benchmark}_skillopt_id_split.json"


def git_head(root: Path | None = None) -> str:
    base = root or repo_root()
    try:
        out = subprocess.check_output(
            ["git", "-C", str(base), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        commit = out.strip()
        return commit or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
