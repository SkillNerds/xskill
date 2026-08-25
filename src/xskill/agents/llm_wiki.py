"""Karpathy llm-wiki 精简版：Generate 把会话证据写到磁盘，压缩后还能读回来。"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from agno.tools import tool

_SAFE_PAGE = re.compile(r"^[A-Za-z0-9_./-]+$")

_SCHEMA = """# Generate 会话证据 wiki

三层：raw 是会话文件（只读）；wiki 是本目录（模型写）；schema 是约定。

页面：index.md、log.md、pages/survey.md、pages/patterns.md、pages/skill-outline.md。
不要为每条轨迹单独建一页。survey 一张表能装几十行。

压缩之后：wiki_status → wiki_read pages/survey.md → 只补表里还没有的 traj_id。
"""

_INDEX = """# index

- [SCHEMA.md](SCHEMA.md)
- [pages/survey.md](pages/survey.md)
- [pages/patterns.md](pages/patterns.md)
- [pages/skill-outline.md](pages/skill-outline.md)
- [log.md](log.md)
"""


def seed_generate_wiki(root: Path) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pages").mkdir(exist_ok=True)
    files = {
        "SCHEMA.md": _SCHEMA,
        "index.md": _INDEX,
        "log.md": "# log\n\n",
        "pages/survey.md": "# survey\n\n| traj_id | 要点 | 可写进 skill 的做法 |\n|---|---|---|\n",
        "pages/patterns.md": "# patterns\n\n还没有跨会话归纳。\n",
        "pages/skill-outline.md": "# skill-outline\n\n写 SKILL.md 前列出将引用的 traj_id。\n",
    }
    for rel, text in files.items():
        path = root / rel
        if not path.is_file():
            path.write_text(text, encoding="utf-8")
    return root


def _wiki_root() -> Path | str:
    from xskill.agents.agent_tools import current_agent_tool_context

    ctx = current_agent_tool_context()
    raw = getattr(ctx, "wiki_root", None)
    if raw is None:
        return "error: 当前 Generate 上下文没有 wiki_root"
    root = Path(raw).expanduser()
    try:
        root = root.resolve()
    except OSError:
        pass
    if not root.is_dir():
        return f"error: wiki 根不存在: {root}"
    return root


def _resolve_page(root: Path, rel: str) -> Path | str:
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return "error: path 为空"
    if rel.endswith("/"):
        return "error: path 必须是文件"
    if not _SAFE_PAGE.match(rel) or ".." in rel.split("/"):
        return "error: path 只允许相对 wiki 根，例如 index.md 或 pages/survey.md"
    if not rel.endswith(".md"):
        rel += ".md"
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "error: path 越出 wiki 根"
    return path


@tool(name="wiki_status")
def wiki_status() -> str:
    """看 wiki 有哪些页。压缩之后应先调这个，再 wiki_read index.md。"""
    root = _wiki_root()
    if isinstance(root, str):
        return root
    pages = sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*.md") if p.is_file()
    )
    index = root / "index.md"
    head = ""
    if index.is_file():
        head = "\n".join(index.read_text(encoding="utf-8").splitlines()[:40])
    return (
        f"wiki_root={root}\npages={len(pages)}\n"
        + "\n".join(f"- {name}" for name in pages)
        + ("\n\n# index.md (head)\n" + head if head else "")
    )


@tool(name="wiki_read")
def wiki_read(path: str) -> str:
    """读 wiki 里的一页。先 index.md，再 pages/survey.md。"""
    root = _wiki_root()
    if isinstance(root, str):
        return root
    target = _resolve_page(root, path)
    if isinstance(target, str):
        return target
    if not target.is_file():
        return f"error: 没有这一页: {target.relative_to(root)}"
    return f"path={target.relative_to(root)}\n\n{target.read_text(encoding='utf-8')}"


@tool(name="wiki_write")
def wiki_write(path: str, content: str) -> str:
    """新建或覆盖 wiki 页。每看完一批会话，更新 pages/survey.md。"""
    root = _wiki_root()
    if isinstance(root, str):
        return root
    target = _resolve_page(root, path)
    if isinstance(target, str):
        return target
    text = (content or "").strip()
    if not text:
        return "error: content 为空"
    if len(text) > 40_000:
        return "error: 单页不要超过 40000 字"
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    target.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
    rel = target.relative_to(root).as_posix()
    _touch_index(root, rel, created=not existed)
    return f"ok {'updated' if existed else 'created'} {rel} chars={len(text)}"


@tool(name="wiki_search")
def wiki_search(pattern: str, max_results: int = 40) -> str:
    """在 wiki 里用正则搜，找回已经写过的 traj_id。"""
    root = _wiki_root()
    if isinstance(root, str):
        return root
    pat = (pattern or "").strip()
    if not pat:
        return "error: pattern 为空"
    try:
        cre = re.compile(pat, re.I)
    except re.error as exc:
        return f"error: 非法正则: {exc}"
    take = max(1, min(int(max_results or 40), 80))
    hits: list[str] = []
    for path in sorted(root.rglob("*.md")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        for i, line in enumerate(lines, 1):
            if cre.search(line):
                hits.append(f"{rel}:{i}:{line[:200]}")
                if len(hits) >= take:
                    return "\n".join(hits)
    return "\n".join(hits) if hits else f"(no hits for {pat!r})"


@tool(name="wiki_log")
def wiki_log(entry: str) -> str:
    """往 log.md 追加一条时间线。"""
    root = _wiki_root()
    if isinstance(root, str):
        return root
    text = (entry or "").strip()
    if not text:
        return "error: entry 为空"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"## [{stamp}] {text}\n"
    log = root / "log.md"
    prev = log.read_text(encoding="utf-8") if log.is_file() else "# log\n\n"
    if not prev.endswith("\n"):
        prev += "\n"
    log.write_text(prev + line, encoding="utf-8")
    return f"ok appended log.md chars={len(line)}"


def _touch_index(root: Path, rel: str, *, created: bool) -> None:
    if rel in {"index.md", "log.md", "SCHEMA.md"} or not created:
        return
    index = root / "index.md"
    if not index.is_file():
        return
    text = index.read_text(encoding="utf-8")
    if f"]({rel})" in text:
        return
    if not text.endswith("\n"):
        text += "\n"
    index.write_text(text + f"- [{rel}]({rel})\n", encoding="utf-8")
