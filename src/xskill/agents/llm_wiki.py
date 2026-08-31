"""Karpathy llm-wiki 精简版：Generate 把会话证据写到磁盘，压缩后还能读回来。

只给 GenerateAgent 挂工具。SkillEdit / TaskAgent 不导入、不注册。
没有新的 PyPI 依赖。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from agno.tools import tool

_SAFE_PAGE = re.compile(r"^[A-Za-z0-9_./-]+$")

AFTER_COMPACT_HINT = (
    "上下文刚被压缩。先调用 wiki_status，再 wiki_read pages/survey.md，"
    "只补表里还没有的 traj_id，不要把已经读过的轨迹整表重扫。"
)
AFTER_COMPACT_EMPTY_HINT = (
    "上下文刚被压缩，survey 还是空的。不要 list_files 扫轨迹目录。"
    "用 traj_search 按指令里的关键词搜，traj_cards 只负责挑人，"
    "然后按卡片上的 L 行号 read_traj 精读。没 read_traj 过的 id 不要写进 survey。"
)
_COMPACT_HINT_MARK = "上下文刚被压缩"


def _survey_has_rows(root: Path) -> bool:
    survey = root / "pages" / "survey.md"
    if not survey.is_file():
        return False
    try:
        lines = survey.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        text = line.strip()
        # 模型追加的行可能是表格行（| 开头）也可能是列表行（- 开头），都算数
        if "traj_" in text and text[:1] in "|-" and "traj_id" not in text:
            return True
    return False


def _after_compact_hint_text() -> str:
    root = _wiki_root()
    if isinstance(root, Path) and _survey_has_rows(root):
        return AFTER_COMPACT_HINT
    return AFTER_COMPACT_EMPTY_HINT


def apply_after_compact_hint(messages) -> None:
    """compact 成功后塞一条提示：survey 有行就回收，空表就去读会话。"""
    if not messages:
        return
    last = messages[-1]
    content = getattr(last, "content", None)
    if content is None and isinstance(last, dict):
        content = last.get("content")
    if _COMPACT_HINT_MARK in str(content or ""):
        return
    hint = _after_compact_hint_text()
    try:
        from agno.models.message import Message

        messages.append(Message(role="user", content=hint))
    except Exception:  # noqa: BLE001 — agno 不可用时退回 dict
        messages.append({"role": "user", "content": hint})

_SCHEMA = """# Generate 会话证据 wiki

三层：raw 是会话文件（只读）；wiki 是本目录（模型写）；schema 是约定。

页面：index.md、log.md、pages/survey.md、pages/patterns.md、pages/skill-outline.md。
不要为每条轨迹单独建一页。survey 一张表能装几十行。

增量协议：往 survey 加行、往 patterns 补一条，都用 wiki_edit（old_string 留空
就是追加到页尾），不要 wiki_write 整页重写——整页重写既费输出 token，又容易把
之前的行弄丢。只有重排整页结构时才用 wiki_write。

压缩之后：wiki_status → wiki_read pages/survey.md → 只补表里还没有的 traj_id。
"""

_INDEX = """# index

- [SCHEMA.md](SCHEMA.md)
- [pages/survey.md](pages/survey.md)
- [pages/patterns.md](pages/patterns.md)
- [pages/skill-outline.md](pages/skill-outline.md)
- [log.md](log.md)
"""

_SURVEY_SEED = """# survey

每精读完几条轨迹就来登记，一条轨迹一行，用 wiki_edit（old_string 留空）把新行
追加到本页末尾。只登记真正 read_traj 精读过的 traj_id；只看过卡片的不算。
「要点」写这条轨迹实际发生了什么（一两句）；「可写进 skill 的做法」写能复用的
结论、坑、绕路——没有就写「无新增」，别硬编。

| traj_id | 要点 | 可写进 skill 的做法 |
|---|---|---|
"""

_PATTERNS_SEED = """# patterns

跨轨迹反复出现的做法才配叫 pattern：至少两三条不同轨迹里出现过，写明证据来自
哪几个 traj_id。只在一条轨迹里出现的个例留在 survey 里，不要提前搬进来。
用 wiki_edit 追加，格式照下面的骨架：

## pattern: （一句话名字）
- 证据: traj_xxx, traj_yyy
- 做法: （怎么做）
- 坑: （不这么做会怎样，可留空）
"""

_OUTLINE_SEED = """# skill-outline

动笔写 SKILL.md 之前先在这里列大纲：分几节、每节说什么、证据用哪些 traj_id。
写作时照着大纲逐节展开，引用的 traj_id 从 survey 里挑，别引用没精读过的。

- 名字/一句话定位:
- 章节草案:
  - （节名）— 证据: traj_...
- 还缺什么证据、下一轮搜什么词:
"""


def seed_generate_wiki(root: Path) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pages").mkdir(exist_ok=True)
    files = {
        "SCHEMA.md": _SCHEMA,
        "index.md": _INDEX,
        "log.md": "# log\n\n",
        "pages/survey.md": _SURVEY_SEED,
        "pages/patterns.md": _PATTERNS_SEED,
        "pages/skill-outline.md": _OUTLINE_SEED,
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
    """列出 wiki 现有的页面，附 index.md 开头。上下文被压缩后第一个该调的工具。

    看到有哪些页之后，用 wiki_read 把 pages/survey.md 读回来，就知道之前已经
    精读过哪些轨迹，只补缺的，不用重扫。
    """
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
    """读回 wiki 里的一页，用来在上下文被压缩后找回自己写过的证据。

    压缩之后的标准动作是先 wiki_status 看有哪些页，再 wiki_read
    pages/survey.md 拿回已经读过哪些轨迹。

    Args:
        path: 相对 wiki 根的页面路径，例如 index.md 或 pages/survey.md。
    """
    root = _wiki_root()
    if isinstance(root, str):
        return root
    target = _resolve_page(root, path)
    if isinstance(target, str):
        return target
    if not target.is_file():
        return f"error: 没有这一页: {target.relative_to(root)}"
    return f"path={target.relative_to(root)}\n\n{target.read_text(encoding='utf-8')}"


@tool(name="wiki_edit")
def wiki_edit(path: str, old_string: str, new_string: str) -> str:
    """增量改一页 wiki：old_string 留空是追加到页尾，非空是唯一替换。

    往 pages/survey.md 加新行、往 patterns 补一条，都用留空追加，
    不要 wiki_write 整页重写。改已有的某一行才用替换：old_string 必须
    在页里唯一命中，命中不了或命中多处都会报错，这时多带几行上下文。

    Args:
        path: 相对 wiki 根的页面路径，例如 pages/survey.md。
        old_string: 要被替换的原文；留空表示把 new_string 追加到页尾。
        new_string: 新内容。追加时自成一行或多行。
    """
    root = _wiki_root()
    if isinstance(root, str):
        return root
    target = _resolve_page(root, path)
    if isinstance(target, str):
        return target
    if not target.is_file():
        return f"error: 没有这一页: {path}，新页面用 wiki_write 创建"
    new_text = new_string or ""
    if not new_text.strip() and (old_string or "").strip():
        # 替换成空 = 删除，允许；追加空串没意义
        pass
    text = target.read_text(encoding="utf-8")
    old = old_string or ""
    if not old.strip():
        if not new_text.strip():
            return "error: 追加时 new_string 不能为空"
        if not text.endswith("\n"):
            text += "\n"
        merged = text + new_text.rstrip("\n") + "\n"
        if len(merged) > 40_000:
            return "error: 追加后超过单页四万字上限，先精简或另起一页"
        target.write_text(merged, encoding="utf-8")
        return f"ok appended {target.relative_to(root).as_posix()} chars=+{len(new_text)}"
    count = text.count(old)
    if count == 0:
        return "error: old_string 没有命中，先 wiki_read 看原文再试"
    if count > 1:
        return f"error: old_string 命中 {count} 处，多带几行上下文让它唯一"
    merged = text.replace(old, new_text, 1)
    if len(merged) > 40_000:
        return "error: 替换后超过单页四万字上限"
    target.write_text(merged, encoding="utf-8")
    return f"ok edited {target.relative_to(root).as_posix()}"


@tool(name="wiki_write")
def wiki_write(path: str, content: str) -> str:
    """新建一页 wiki，或整页重写既有页。日常增量更新用 wiki_edit，不用这个。

    每精读几条轨迹就把证据落到 pages/survey.md（用 wiki_edit 追加行），
    不要攒到最后；只写真正精读过的 traj_id。跨会话的共性做法进
    pages/patterns.md，skill 大纲进 pages/skill-outline.md。
    单页上限四万字，不要为每条轨迹单独建一页。

    Args:
        path: 相对 wiki 根的页面路径，例如 pages/survey.md。
        content: 这一页的完整新内容。
    """
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
    """在 wiki 全部页面里跑正则，查某条轨迹或某个主题是不是已经写过。

    比整页读回来省上下文：想确认某个 traj_id 记过没有，搜它的 id 即可。

    Args:
        pattern: 正则表达式，大小写不敏感。
        max_results: 最多返回多少行命中，默认 40，上限 80。
    """
    root = _wiki_root()
    if isinstance(root, str):
        return root
    pat = (pattern or "").strip()
    if not pat:
        return "error: pattern 为空"
    try:
        cre = re.compile(pat, re.IGNORECASE)
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
    """往 log.md 追加一行带时间戳的进度，记录干到哪一步了。

    和 wiki_write 的区别：这里是追加一行流水，不覆盖任何东西。适合写
    「已精读 15 条，下一步搜哪几个词」这类给压缩后的自己看的交接信息。

    Args:
        entry: 一行进度说明。
    """
    root = _wiki_root()
    if isinstance(root, str):
        return root
    text = (entry or "").strip()
    if not text:
        return "error: entry 为空"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"## [{stamp}] {text}\n"
    log = root / "log.md"
    if not log.is_file():
        log.write_text("# log\n\n", encoding="utf-8")
    with log.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if size:
            handle.seek(-1, 2)
            prefix = "" if handle.read(1) == b"\n" else "\n"
        else:
            prefix = "\n"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(prefix + line)
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
