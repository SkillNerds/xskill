"""Generate 专用的轨迹工具：找轨迹、看卡片、精读三条链上各一个入口。

只给 GenerateAgent 挂。SkillEdit / TaskAgent / TaskCluster 不导入、不注册。
没有新的 PyPI 依赖。

GenerateAgent 的完整工具面（18 个，这里实现前 4 个）
--------------------------------------------------

轨迹侧（本模块）：

1. ``traj_search(query, offset, limit, context)`` —— 找轨迹的唯一入口。不带
   query 是翻目录，按最近修改排序分页给 traj_id、行数、首问一句；带 query 是
   全文检索，每行带首个命中行号、片段与该条总命中处数，context>0 时首命中处
   带前后原文，按命中处数、首次命中行、修改时间和 ID 排序。
2. ``atom_search(query, top_k)`` —— 语义检索轨迹原子。query 用整句自然语言，
   返回每个原子的 traj_id、行号区间、intent、summary 摘句和 tags；关键词搜
   不准、想按"这类事怎么做"找轨迹时用它。旧原子行号可能不可靠，会标注。
3. ``traj_cards(traj_ids)`` —— 看轨迹概要的唯一入口，一次最多 8 条。每张卡
   给来源、总行数、user 轮数、工具统计，然后是带行号的全部用户问题、每问
   一句去掉思维链的回答、末尾一句收尾结论。工具返回结果不进卡。看卡不算
   精读。
4. ``read_traj(traj_id, offset, limit)`` —— 精读的唯一入口，按 id 跨全部轨迹
   目录解析，返回带行号原文，越界自动夹紧。精读计数只在这里记，
   ``commit_generate_main`` 的保底闸门数的就是这个数。

wiki 侧（``llm_wiki``）：

5. ``wiki_status`` 列出有哪些页，压缩后第一个该调的。
6. ``wiki_read`` 读回某一页，主要用来找回 survey 表。
7. ``wiki_write`` 新建或整页重写，日常增量不用它。
8. ``wiki_edit`` 增量改一页：old_string 留空追加到页尾，非空唯一替换。
9. ``wiki_search`` 在全部页跑正则，查某个 traj_id 写过没有。
10. ``wiki_log`` 往 log.md 追加一行进度。

skill 侧（``agent_tools``）：

11. ``skill_read`` 读某个已有 skill 的 SKILL.md 与文件树。
12. ``list_files`` 列目录，只服务 skill 目录与 spill；打到轨迹目录改道。
13. ``grep_files`` 全文搜，同样只服务 skill 目录与 spill；打到轨迹目录改道。
14. ``read_file`` 按行读 skill 文件与 spill 落盘件；碰到 traj_*.md 改道
    ``read_traj``。读过才允许 edit。
15. ``write_file`` 在 skill 目录新建或整文件覆盖，写 SKILL.md 先校验
    frontmatter，非法不落盘；generate 里裸相对路径落到本次新建的 skill 目录。
16. ``edit`` 对读过的 skill 文件做一处精确替换。
17. ``new_skill_folder`` 新建 skill 目录并初始化 git 到 baby 分支。
18. ``commit_generate_main`` 提交到 main，提交前查精读条数与 SKILL.md 非占位。

轨迹条数不写死在代码里：这里只有一条低的硬保底（防摆烂），目标条数由提示词
与用户指令给，实验观测代理是否服从。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agno.tools import tool

QUERY_SNIP = 160
ANSWER_SNIP = 120
TAIL_SNIP = 200
CARD_CHAR_CAP = 1500
CARDS_MAX = 8
LIST_PAGE = 30
SEARCH_CAP = 30
READ_LIMIT_MAX = 400
MIN_TRAJ_BYTES = 200
SCAN_MAX_FILES = 2000
WIKI_NUDGE_EVERY = 5

_KNOWN_HEAD = re.compile(
    r"^## (User|Assistant|Initial Query|Raw Content|"
    r"Tool Call: [^\n]+|Tool Output: [^\n]+|Event: [^\n]+)\s*$"
)
_REASONING_PARA = re.compile(r"^\s*_\(reasoning\)_", re.IGNORECASE)
_PLACEHOLDER_ANSWER = re.compile(
    r"^\[[a-z]+ response_item #\d+\]$", re.IGNORECASE,
)
_TRAJ_NAME = re.compile(r"^traj_[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
_SOURCE_BY_PREFIX = (
    ("traj_cc_", "claude-code"),
    ("traj_codex_", "codex"),
    ("traj_oc_", "opencode"),
    ("traj_cursor_", "cursor"),
)


def _scrub(text: str) -> str:
    text = re.sub(r"(sk-[A-Za-z0-9_-]{8,})", "sk-[REDACTED]", text)
    return re.sub(
        r"(?i)(api[_-]?key|token|password)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )


def _one_line(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", _scrub(text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _traj_roots() -> list[Path]:
    from xskill.agents.agent_tools import (
        _is_blocked_read_path,
        current_agent_tool_context,
    )

    ctx = current_agent_tool_context()
    candidates: list[Path] = []
    if ctx.default_traj_root is not None:
        candidates.append(Path(ctx.default_traj_root))
    candidates.extend(Path(p) for p in (ctx.extra_read_roots or ()))
    roots: list[Path] = []
    seen: set[str] = set()
    for raw in candidates:
        try:
            path = raw.resolve()
        except OSError:
            path = raw
        key = str(path)
        if key in seen or _is_blocked_read_path(path):
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _traj_files() -> list[Path]:
    """所有可读的 traj_*.md，最近改动在前。on hold 目录已在根一级排除。"""
    from xskill.agents.agent_tools import _is_blocked_read_path

    found: list[Path] = []
    seen: set[str] = set()
    for root in _traj_roots():
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.rglob("traj_*.md"))
        except OSError:
            continue
        for path in entries:
            if len(found) >= SCAN_MAX_FILES:
                break
            if not _TRAJ_NAME.match(path.name) or not path.is_file():
                continue
            try:
                if path.stat().st_size < MIN_TRAJ_BYTES:
                    continue
                key = str(path.resolve())
            except OSError:
                continue
            if key in seen or _is_blocked_read_path(path):
                continue
            seen.add(key)
            found.append(path)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def _by_id() -> dict[str, Path]:
    return {path.stem: path for path in _traj_files()}


# ── 卡片渲染 ──────────────────────────────────────────────────────────

@dataclass
class _Section:
    kind: str
    name: str
    start: int
    body_start: int
    end: int


def _parse_sections(text: str) -> list[_Section]:
    """只认桥接件写出的固定节名；正文里自带的 ## 标题不当节边界。"""
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    for index, line in enumerate(lines, 1):
        matched = _KNOWN_HEAD.match(line)
        if matched:
            marks.append((index, matched.group(1)))
    out: list[_Section] = []
    for pos, (lineno, head) in enumerate(marks):
        end = marks[pos + 1][0] if pos + 1 < len(marks) else len(lines) + 1
        if head == "User":
            kind, name = "user", ""
        elif head == "Assistant":
            kind, name = "assistant", ""
        elif head.startswith("Tool Call: "):
            kind, name = "tool_call", head[len("Tool Call: "):]
        elif head.startswith("Tool Output: "):
            kind, name = "tool_output", head[len("Tool Output: "):]
        else:
            kind, name = "other", head
        out.append(_Section(kind, name, lineno, lineno + 1, end))
    return out


def _body(lines: list[str], section: _Section) -> str:
    return "\n".join(lines[section.body_start - 1 : section.end - 1]).strip()


def _source_of(traj_id: str) -> str:
    for prefix, name in _SOURCE_BY_PREFIX:
        if traj_id.startswith(prefix):
            return name
    return "unknown"


def _tool_histogram(sections: list[_Section]) -> str:
    counts: dict[str, int] = {}
    for section in sections:
        if section.kind == "tool_call":
            counts[section.name] = counts.get(section.name, 0) + 1
    if not counts:
        return "(无工具调用)"
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    return " ".join(f"{name}×{n}" for name, n in ranked)


def _answer_snippet(body: str) -> str:
    """答句丢思维链：先去 _(reasoning)_ 段，再跳过纯占位符正文。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    kept = [p for p in paragraphs if not _REASONING_PARA.match(p)]
    chosen = kept or paragraphs
    if not chosen:
        return ""
    text = _one_line(chosen[0], ANSWER_SNIP)
    return "" if _PLACEHOLDER_ANSWER.match(text) else text


def render_card(path: Path) -> str:
    traj_id = path.stem
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    sections = _parse_sections(text)
    users = [s for s in sections if s.kind == "user"]
    assistants = [s for s in sections if s.kind == "assistant"]

    rows: list[str] = []
    seen_questions: set[str] = set()
    seen_answers: set[int] = set()
    folded = 0
    for user in users:
        question = _one_line(_body(lines, user), QUERY_SNIP)
        if not question:
            continue
        if question in seen_questions:
            folded += 1
            continue
        seen_questions.add(question)
        rows.append(f"L{user.start} 问: {question}")
        follow = next((a for a in assistants if a.start > user.start), None)
        if follow is not None and follow.start not in seen_answers:
            answer = _answer_snippet(_body(lines, follow))
            if answer:
                seen_answers.add(follow.start)
                rows.append(f"L{follow.start} 答: {answer}")
    if folded:
        rows.append(f"（另有 {folded} 条重复问句已折叠）")

    # 收尾取最后一段有实义的回答；已经作为答句列过就不重复。
    tail = ""
    for section in reversed(assistants):
        if section.start in seen_answers:
            break
        snippet = _answer_snippet(_body(lines, section))
        if snippet:
            tail = f"L{section.start} 收尾: {_one_line(snippet, TAIL_SNIP)}"
            break

    header = (
        f"--- {traj_id} ---\n"
        f"来源: {_source_of(traj_id)}  总行数: {len(lines)}  "
        f"user 轮: {len(users)}  工具: {_tool_histogram(sections)}"
    )
    footer = f'精读: read_traj("{traj_id}", offset=<上面的 L 行号>, limit=120)'

    def assemble(body_rows: list[str]) -> str:
        parts = [header, *body_rows]
        if tail:
            parts.append(tail)
        parts.append(footer)
        return "\n".join(parts)

    # 超预算时从中间删问答行；头、尾、精读指引必须保住。
    kept_rows = list(rows)
    dropped = 0
    while len(assemble(kept_rows)) > CARD_CHAR_CAP and len(kept_rows) > 4:
        kept_rows.pop(len(kept_rows) // 2)
        dropped += 1
    if dropped:
        kept_rows.insert(
            len(kept_rows) // 2,
            f"…（超出卡片预算，中间 {dropped} 行问答未列，精读时按行号翻）",
        )
    card = assemble(kept_rows)
    if len(card) > CARD_CHAR_CAP + 200:
        card = card[: CARD_CHAR_CAP + 180] + "\n…[卡片截断]"
    return card


def _first_question(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(读失败)"
    lines = text.splitlines()
    for section in _parse_sections(text):
        if section.kind == "user":
            return _one_line(_body(lines, section), 100)
    return "(没有 user 段)"


def _line_count(path: Path) -> int:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _rg(args: list[str], timeout: int) -> str:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return completed.stdout if completed.returncode <= 1 else ""


def _format_search_hit(
    path: Path, hit_line: int, snippet: str, count: int, context: int,
) -> str:
    """检索命中：默认一行摘要；context>0 时带前后文，命中行标星。"""
    mark = f"（共命中 {count} 处）" if count > 1 else ""
    if context <= 0:
        return f"{path.stem}\tL{hit_line}: {_one_line(snippet, 100)}{mark}"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    start = max(1, hit_line - context)
    end = min(len(lines), hit_line + context) if lines else hit_line
    rows = [f"{path.stem}{mark}"]
    for number in range(start, end + 1):
        tag = "*" if number == hit_line else " "
        text = lines[number - 1] if 0 <= number - 1 < len(lines) else ""
        rows.append(f"  L{number}{tag} {_one_line(text, 90)}")
    return "\n".join(rows)


def _search_by_query(query: str, take: int, context: int = 0) -> str:
    from xskill.traj_browse import TrajHit, query_hit_sort_key

    roots = _traj_roots()
    if not shutil.which("rg"):
        return _search_by_query_python(query, take, roots, context)
    # 一遍 rg -c 同时拿命中文件与每文件命中处数
    counts: dict[Path, int] = {}
    for root in roots:
        if not root.is_dir():
            continue
        out = _rg(
            ["rg", "-c", "--color", "never", "--smart-case",
             "--glob", "traj_*.md", "-e", query, str(root)],
            30,
        )
        for raw in out.splitlines():
            spec, _, num = raw.strip().rpartition(":")
            path = Path(spec)
            try:
                usable = path.is_file() and path.stat().st_size >= MIN_TRAJ_BYTES
            except OSError:
                usable = False
            if usable and path not in counts:
                try:
                    counts[path] = int(num)
                except ValueError:
                    counts[path] = 1
    if not counts:
        return (
            f"query={query!r} 没有命中。换一组更常见的词，"
            "或不给 query 直接翻目录。"
        )
    # Only the top count bands can reach this page. Read first-hit lines for
    # the entire cutoff band so ties are ranked correctly without reopening
    # every lower-relevance file just to discard it afterward.
    cutoff = sorted(counts.values(), reverse=True)[min(take, len(counts)) - 1]
    hits: list[TrajHit] = []
    for path, count in counts.items():
        if count < cutoff:
            continue
        args = ["rg", "-n", "--no-heading", "--color", "never", "--smart-case",
                "-m", "1", "-e", query, str(path)]
        out = _rg(args, 10)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        first = lines[0] if lines else "1:"
        lineno, _, content = first.partition(":")
        hit_line = int(lineno) if lineno.isdigit() else 1
        hits.append(TrajHit(path.stem, path, "", hit_line, content, count))
    shown = sorted(hits, key=query_hit_sort_key)[:take]
    leftover = len(counts) - len(shown)
    rows = [
        _format_search_hit(hit.path, hit.line, hit.snippet, hit.hit_count, context)
        for hit in shown
    ]
    head = (
        f"query={query!r} 命中 {len(counts)} 条不同轨迹，展示 {len(shown)} 条"
        f"（按相关度排序）。看内容用 traj_cards，一次最多 {CARDS_MAX} 个 id。"
        "命中处数多的轨迹通常整条都和主题相关，优先精读；"
        "想看某一处的前后文，read_traj 按行号跳过去。"
    )
    if leftover > 0:
        head += f" 还有 {leftover} 条未列出，换一组词可以搜到别的。"
    return head + "\n" + "\n".join(rows)


def _search_by_query_python(
    query: str, take: int, roots: list[Path], context: int = 0,
) -> str:
    from xskill.traj_browse import TrajHit, query_hit_sort_key

    needle = query.lower()
    hits: list[TrajHit] = []
    for path in _traj_files():
        first_no, first_line, count = 0, "", 0
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    if needle in line.lower():
                        count += 1
                        if not first_no:
                            first_no, first_line = number, line
        except OSError:
            continue
        if count:
            hits.append(TrajHit(path.stem, path, "", first_no, first_line, count))
    if not hits:
        return f"query={query!r} 没有命中。换一组词，或不给 query 直接翻目录。"
    ordered = sorted(hits, key=query_hit_sort_key)[:take]
    rows = [
        _format_search_hit(hit.path, hit.line, hit.snippet, hit.hit_count, context)
        for hit in ordered
    ]
    leftover = len(hits) - len(rows)
    head = (
        f"query={query!r} 命中 {len(hits)} 条不同轨迹，展示 {len(rows)} 条"
        "（按相关度排序；无 rg，退回逐行匹配）。看内容用 traj_cards。"
    )
    if leftover > 0:
        head += f" 还有 {leftover} 条未列出。"
    return head + "\n" + "\n".join(rows)


# ── 工具 ─────────────────────────────────────────────────────────────

@tool(name="traj_search")
def traj_search(
    query: str = "", offset: int = 0, limit: int = LIST_PAGE, context: int = 0,
) -> str:
    """找参考轨迹的唯一入口：给 query 是全文检索，不给是按时间翻目录。

    带 query 时按关键词搜全部轨迹正文，每条命中给 traj_id、首个命中行号和该条
    的总命中处数；命中处数多的轨迹通常整条都和主题相关。context 大于 0 时首个
    命中处会带出前后几行原文，用来快速判断命中是不是想要的。先按命中处数降序、
    首次命中行升序，再按修改时间降序和 ID 排序。用户指令里的关键词、报错片段、
    命令名、模块名都适合当 query。不带 query 时按最近修改排序分页，每条给 traj_id、总行
    数、首问一句，用于摸清库里有什么。
    两种模式返回的都只是索引，要看内容用 traj_cards，要精读用 read_traj。
    轨迹目录不要用 list_files 或 grep_files。

    Args:
        query: 检索词，留空表示翻目录。
        offset: 翻目录时的起始位置，从 0 开始；检索模式忽略。
        limit: 本次返回条数，翻目录最多 30，检索最多 30。
        context: 检索模式下首个命中处前后各带几行原文，默认 0，最多 5。
    """
    try:
        start = max(0, int(offset))
        take = max(1, min(int(limit), LIST_PAGE))
        radius = max(0, min(int(context), 5))
    except (TypeError, ValueError):
        return "error: offset、limit、context 必须是整数"
    roots = _traj_roots()
    if not roots:
        return "error: 当前上下文没有可读的轨迹目录"
    needle = (query or "").strip()
    if needle:
        return _search_by_query(needle, min(take, SEARCH_CAP), radius)
    files = _traj_files()
    if not files:
        listed = "、".join(str(root) for root in roots)
        return f"这些目录里没有可读的 traj_*.md：{listed}"
    page = files[start : start + take]
    rows = [
        f"{path.stem}\t{_line_count(path)}行\t首问: {_first_question(path)}"
        for path in page
    ]
    head = (
        f"轨迹总数 {len(files)}，本页 {len(page)} 条（offset={start}，"
        f"按最近修改排序）。看内容用 traj_cards，一次最多 {CARDS_MAX} 个 id；"
        "定向找轨迹请给 query。"
    )
    footer = ""
    if start + take < len(files):
        footer = f"\n下一页: traj_search(offset={start + take}, limit={take})"
    return head + "\n" + "\n".join(rows) + footer


ATOM_TOPK_MAX = 16
_ATOM_STORES: dict[str, object] = {}
_QUERY_VECTOR_CACHE: dict[str, object] = {}


class _CachedEmbed:
    """同一个 query 只 encode 一次；跨 root 复用向量，其余属性透传。"""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def encode(self, text: str):
        key = f"{getattr(self._inner, 'model', '')}\x00{text}"
        if key not in _QUERY_VECTOR_CACHE:
            if len(_QUERY_VECTOR_CACHE) > 64:
                _QUERY_VECTOR_CACHE.clear()
            _QUERY_VECTOR_CACHE[key] = self._inner.encode(text)
        return _QUERY_VECTOR_CACHE[key]


def _atom_store_for(root: Path):
    from xskill.pipeline.atom import AtomTaskStore

    key = str(root)
    if key not in _ATOM_STORES:
        _ATOM_STORES[key] = AtomTaskStore(root)
    return _ATOM_STORES[key]


def _atom_offsets_ok(atom, line_total: int) -> bool:
    """老原子存的是字符偏移不是行号，超出轨迹行数即不可信。"""
    return (
        atom.offset_start >= 1
        and atom.offset_end > atom.offset_start
        and atom.offset_end <= line_total + 1
    )


@tool(name="atom_search")
def atom_search(query: str, top_k: int = 8) -> str:
    """语义检索轨迹原子：用整句自然语言找"干过这类事"的轨迹片段。

    每条轨迹被切成若干原子，原子带一段摘要；这里按语义相似度返回最接近的
    原子，附 traj_id、行号区间、意图、摘要和标签。关键词 traj_search 搜不准、
    或想描述一类做法而不是一个词的时候用它，query 越具体越好，可以是一整句
    话。返回的行号区间直接喂 read_traj 精读；标了行号不可靠的，用 traj_search
    带关键词定位或 read_traj 从头翻。返回的只是索引，不算精读。

    Args:
        query: 自然语言描述，例如"部署时网络源出问题怎么绕过"。
        top_k: 返回条数，默认 8，最多 16。
    """
    from xskill.agents.agent_tools import current_agent_tool_context

    text = (query or "").strip()
    if not text:
        return "error: query 为空。用一句话描述要找的做法。"
    ctx = current_agent_tool_context()
    client = ctx.embed_client
    if client is None:
        return (
            "error: 语义检索未配置（没有 embedding 客户端）。"
            "改用 traj_search 关键词搜。"
        )
    try:
        take = max(1, min(int(top_k), ATOM_TOPK_MAX))
    except (TypeError, ValueError):
        return "error: top_k 必须是整数"
    cached = _CachedEmbed(client)
    merged: list[tuple[float, str, object]] = []
    for root in _traj_roots():
        if not root.is_dir() or not any(root.glob("*/tasks/atom_*.json")):
            continue
        store = _atom_store_for(root)
        try:
            hits = store.vector_search(text, cached, top_k=take)
        except Exception as exc:  # noqa: BLE001 — 工具必须返回文本
            return f"error: 语义检索失败: {exc}"
        for hit in hits:
            merged.append((float(hit.get("similarity", 0.0)), hit["atom_id"], store))
    if not merged:
        return (
            "语义索引里没有可检索的原子。改用 traj_search 关键词搜。"
        )
    merged.sort(key=lambda item: item[0], reverse=True)
    lookup = _by_id()
    rows: list[str] = []
    seen_atoms: set[str] = set()
    for sim, atom_id, store in merged:
        if len(rows) >= take:
            break
        if atom_id in seen_atoms:
            continue
        seen_atoms.add(atom_id)
        try:
            atom = store.load(atom_id)
        except Exception:  # noqa: BLE001, S112 — 单条坏原子不拖垮整次检索
            continue
        path = lookup.get(atom.traj_id)
        if path is None:
            continue  # 轨迹不在可读根里（比如 on hold），不外泄
        total = _line_count(path)
        if _atom_offsets_ok(atom, total):
            where = f"L{atom.offset_start}-{atom.offset_end}"
        else:
            where = "行号不可靠，用 traj_search 定位"
        tags = " ".join(atom.tags[:5])
        rows.append(
            f"{sim:.2f} {atom.traj_id} {where}\n"
            f"  意图: {_one_line(atom.intent, 80)}\n"
            f"  摘要: {_one_line(atom.summary, 160)}"
            + (f"\n  标签: {tags}" if tags else "")
        )
    if not rows:
        return "命中的原子都不在可读轨迹里。改用 traj_search 关键词搜。"
    head = (
        f"query={_one_line(text, 60)!r} 语义命中 {len(rows)} 个原子"
        "（按相似度排序）。行号区间直接 read_traj 精读；这只是索引，不算精读。"
    )
    return head + "\n" + "\n".join(rows)


@tool(name="traj_cards")
def traj_cards(traj_ids: str) -> str:
    """看轨迹概要卡的唯一入口，一次最多 8 条，用来从候选里挑该精读哪几条。

    每张卡是这条会话的紧凑索引：来源、总行数、user 轮数、用过哪些工具，然后
    是全部用户问题（带行号）、每问后面一句去掉思维链的回答、最后一句收尾
    结论。工具的返回结果不进卡片，重复问句会折叠，太长的卡从中间省略并标出
    省了多少行。卡上每一行都有 L 行号，可以直接喂给 read_traj。
    卡片只是索引，不算精读，提交前的轨迹条数不数它。

    Args:
        traj_ids: 一个或多个 traj_id，用逗号或空格分开，最多 8 个。
    """
    raw = (traj_ids or "").replace(",", " ").split()
    ids = [item.strip() for item in raw if item.strip()]
    if not ids:
        return "error: traj_ids 为空。先用 traj_search 拿 id。"
    if len(ids) > CARDS_MAX:
        return (
            f"error: 一次最多 {CARDS_MAX} 条，这次给了 {len(ids)}。拆成多次调用。"
        )
    lookup = _by_id()
    chunks: list[str] = []
    for traj_id in ids:
        path = lookup.get(traj_id)
        if path is None:
            chunks.append(
                f"--- {traj_id} ---\nerror: 找不到这条轨迹。先用 traj_search 确认 id。"
            )
            continue
        try:
            chunks.append(render_card(path))
        except OSError as error:
            chunks.append(f"--- {traj_id} ---\nerror: 读失败 {error}")
    return (
        f"cards={len(ids)}（卡片只是索引，不算精读）\n\n" + "\n\n".join(chunks)
    )


@tool(name="read_traj")
def read_traj(traj_id: str, offset: int = 1, limit: int = 120) -> str:
    """精读轨迹原文的唯一入口，按 traj_cards 给的 L 行号读，返回带行号正文。

    按 id 在所有可读轨迹目录里解析文件，不需要路径。行号超出文件会自动夹到
    文件末尾，不报错。返回尾部会告诉你已经精读过多少条不同轨迹、这一条还剩
    多少行、续读该用什么 offset。
    提交 skill 前的轨迹条数只数这个工具读过的不同 traj_id，卡片不算。
    轨迹不要用 read_file 读。

    Args:
        traj_id: 轨迹 id，例如 traj_cc_admin_1ac707e2，不带路径和后缀。
        offset: 1-based 起始行号，取 traj_cards 卡片上的 L 行号。
        limit: 本次读多少行，默认 120，最多 400。
    """
    from xskill.agents.agent_tools import (
        _note_generate_traj_read,
        generate_read_traj_ids,
    )

    wanted = (traj_id or "").strip()
    if not wanted:
        return "error: traj_id 为空。先用 traj_search 拿 id。"
    if "/" in wanted or "\\" in wanted:
        return "error: traj_id 只要 id 本身，不要带路径。"
    if wanted.endswith((".md", ".json")):
        wanted = wanted.rsplit(".", 1)[0]
    path = _by_id().get(wanted)
    if path is None:
        return f"error: 找不到轨迹 {wanted}。先用 traj_search 确认 id。"
    try:
        start = max(1, int(offset))
        take = max(1, min(int(limit), READ_LIMIT_MAX))
    except (TypeError, ValueError):
        return "error: offset 和 limit 必须是整数"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        return f"error: 读失败 {path}: {error}"
    total = len(lines)
    if start > total:
        start = max(1, total - take + 1)
    end = min(total, start + take - 1)
    before = len(generate_read_traj_ids())
    _note_generate_traj_read(path)
    read_count = len(generate_read_traj_ids())
    first_time = read_count > before
    body = "\n".join(f"{number}| {lines[number - 1]}"
                     for number in range(start, end + 1))
    footer = f"\n\n已精读不同轨迹 {read_count} 条。"
    if end < total:
        footer += f"本条还剩 {total - end} 行，续读 offset={end + 1}。"
    # 只在新读到一条轨迹时催，续读同一条不重复催。
    if first_time and read_count % WIKI_NUDGE_EVERY == 0:
        footer += (
            "现在用 wiki_edit（old_string 留空）把这一批轨迹的要点追加进 "
            "pages/survey.md，并用 wiki_log 记一行，再继续读下一批。"
        )
    header = f"{wanted}  行 {start}..{end} / 共 {total}\n"
    return header + body + footer


__all__ = ["atom_search", "read_traj", "render_card", "traj_cards", "traj_search"]
