"""本机轨迹全文检索与卡片。对齐 generate 工具面的 traj_search / traj_cards。

查询对着 ``traj_*.md`` 正文，不是会话首问索引。卡片只做索引，不算精读。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xskill.utils.proc import windowless_subprocess_kwargs

QUERY_SNIP = 160
ANSWER_SNIP = 120
TAIL_SNIP = 200
CARD_CHAR_CAP = 1500
CARDS_MAX = 8
LIST_PAGE = 30
LIST_CONTEXT = 3
MIN_TRAJ_BYTES = 50
SCAN_MAX_FILES = 2000

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


@dataclass
class TrajHit:
    traj_id: str
    path: Path
    user: str
    line: int
    snippet: str
    hit_count: int


@dataclass
class _Section:
    kind: str
    name: str
    start: int
    body_start: int
    end: int


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


def _tool_rank(item: tuple[str, int]) -> int:
    return -item[1]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _pair_mtime(item: tuple[str, Path]) -> float:
    return _mtime(item[1])


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _source_of(traj_id: str) -> str:
    for prefix, name in _SOURCE_BY_PREFIX:
        if traj_id.startswith(prefix):
            return name
    return "unknown"


def query_hit_sort_key(hit: TrajHit) -> tuple[int, int, float, str]:
    """Shared CLI/team/Generate relevance order, applied before pagination."""
    return (-hit.hit_count, hit.line, -_mtime(hit.path), hit.traj_id)


def _parse_sections(text: str) -> list[_Section]:
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


def _tool_histogram(sections: list[_Section]) -> str:
    counts: dict[str, int] = {}
    for section in sections:
        if section.kind == "tool_call":
            counts[section.name] = counts.get(section.name, 0) + 1
    if not counts:
        return "(无工具调用)"
    ranked = sorted(counts.items(), key=_tool_rank)[:8]
    parts = [f"{name}×{n}" for name, n in ranked]
    return " ".join(parts)


def _answer_snippet(body: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    kept = [p for p in paragraphs if not _REASONING_PARA.match(p)]
    chosen = kept or paragraphs
    if not chosen:
        return ""
    text = _one_line(chosen[0], ANSWER_SNIP)
    return "" if _PLACEHOLDER_ANSWER.match(text) else text


def iter_traj_md(
    dataset_dirs: list[tuple[str, Path]] | None = None,
) -> list[tuple[str, Path]]:
    from xskill.traj_search import watch_session_dirs

    dirs = dataset_dirs if dataset_dirs is not None else watch_session_dirs()
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for user, directory in dirs:
        if not directory.is_dir():
            continue
        try:
            children = list(directory.glob("traj_*.md"))
        except OSError:
            continue
        for path in children:
            if len(found) >= SCAN_MAX_FILES:
                return found
            if not _TRAJ_NAME.match(path.name) or not path.is_file():
                continue
            if _file_size(path) < MIN_TRAJ_BYTES:
                continue
            try:
                key = str(path.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            found.append((user, path))
    found.sort(key=_pair_mtime, reverse=True)
    return found


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
            **windowless_subprocess_kwargs(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return completed.stdout if completed.returncode <= 1 else ""


def _search_python(query: str, files: list[tuple[str, Path]]) -> list[TrajHit]:
    needle = query.lower()
    hits: list[TrajHit] = []
    for user, path in files:
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
            hits.append(TrajHit(
                traj_id=path.stem,
                path=path,
                user=user,
                line=first_no,
                snippet=_one_line(first_line, 100),
                hit_count=count,
            ))
    return hits


def _search_rg(query: str, files: list[tuple[str, Path]]) -> list[TrajHit] | None:
    if not shutil.which("rg"):
        return None
    roots: list[Path] = []
    seen_root: set[str] = set()
    for _user, path in files:
        root = path.parent
        key = str(root)
        if key in seen_root:
            continue
        seen_root.add(key)
        roots.append(root)
    count_map: dict[Path, int] = {}
    for root in roots:
        out = _rg(
            ["rg", "-c", "--color", "never", "--smart-case",
             "--glob", "traj_*.md", "-e", query, str(root)],
            30,
        )
        for raw in out.splitlines():
            spec, _, num = raw.strip().rpartition(":")
            path = Path(spec)
            if _file_size(path) < MIN_TRAJ_BYTES:
                continue
            try:
                count_map[path.resolve()] = int(num)
            except (OSError, ValueError):
                continue
    if not count_map:
        return []
    user_by = {str(path.resolve()): user for user, path in files}
    hits: list[TrajHit] = []
    for path, total in count_map.items():
        out = _rg(
            ["rg", "-n", "--no-heading", "--color", "never", "--smart-case",
             "-m", "1", "-e", query, str(path)],
            10,
        )
        first = ""
        for raw in out.splitlines():
            if raw.strip():
                first = raw
                break
        lineno, _, content = first.partition(":")
        try:
            line_no = int(lineno)
        except ValueError:
            line_no = 1
        hits.append(TrajHit(
            traj_id=path.stem,
            path=path,
            user=user_by.get(str(path), ""),
            line=line_no,
            snippet=_one_line(content, 100),
            hit_count=total,
        ))
    return hits


def find_query_hits(
    query: str,
    *,
    dataset_dirs: list[tuple[str, Path]] | None = None,
) -> list[TrajHit]:
    files = iter_traj_md(dataset_dirs)
    hits = _search_rg(query, files)
    if hits is None:
        hits = _search_python(query, files)
    return sorted(hits, key=query_hit_sort_key)


def page_slice(items: list[Any], page: int, page_size: int) -> list[Any]:
    start = max(0, (max(1, page) - 1) * page_size)
    return items[start : start + page_size]


def _cli_query_token(query: str) -> str:
    if not query:
        return ""
    if any(ch.isspace() for ch in query):
        return "'" + query.replace("'", "") + "'"
    return query


def hit_to_public(hit: TrajHit) -> dict[str, Any]:
    return {
        "kind": "traj",
        "traj_id": hit.traj_id,
        "user": hit.user,
        "line": hit.line,
        "snippet": hit.snippet,
        "hit_count": hit.hit_count,
    }


def _context_window(
    path: Path, hit_line: int, radius: int = LIST_CONTEXT,
) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    center = max(1, int(hit_line or 1))
    start = max(1, center - radius)
    end = min(len(lines), center + radius)
    rows: list[dict[str, Any]] = []
    for number in range(start, end + 1):
        rows.append({
            "line": number,
            "hit": number == center,
            "text": _one_line(lines[number - 1], 90),
        })
    return rows


def listing_hit(hit: TrajHit) -> dict[str, Any]:
    row = hit_to_public(hit)
    row["context"] = _context_window(hit.path, hit.line, LIST_CONTEXT)
    return row


def listing_rows(hits: list[TrajHit]) -> list[dict[str, Any]]:
    return [listing_hit(hit) for hit in hits]


def format_listing(
    query: str,
    hits: list[dict[str, Any]],
    *,
    total: int,
    page: int,
    page_size: int,
    extra_flags: str = "",
) -> str:
    if total <= 0:
        return f"query={query!r} 没有命中。换一组更常见的词。"
    shown = len(hits)
    leftover = max(0, total - ((page - 1) * page_size + shown))
    flag = f" {extra_flags}" if extra_flags else ""
    head = (
        f"query={query!r} 命中 {total} 条不同轨迹，展示 {shown} 条"
        f"（按相关度排序）。看内容用 --cards，一次最多 {CARDS_MAX} 个 id。"
    )
    if leftover > 0:
        head += f" 还有 {leftover} 条未列出，换一组词可以搜到别的。"
        head += (
            f" 下一页：xskill traj search {_cli_query_token(query)}"
            f" --page {page + 1}{flag}"
        )
    rows: list[str] = [head]
    for hit in hits:
        traj_id = str(hit.get("traj_id") or "-")
        line = hit.get("line")
        snippet = _one_line(str(hit.get("snippet") or ""), 100)
        mark = ""
        count = int(hit.get("hit_count") or 0)
        if count > 1:
            mark = f"（共命中 {count} 处）"
        context = hit.get("context") or []
        if context:
            rows.append(f"{traj_id}{mark}")
            for item in context:
                lineno = int(item.get("line") or 0)
                tag = "*" if item.get("hit") else " "
                text = _one_line(str(item.get("text") or ""), 90)
                if lineno:
                    rows.append(f"  L{lineno}{tag} {text}".rstrip())
            continue
        if line:
            rows.append(f"{traj_id}\tL{line}: {snippet}{mark}")
        else:
            rows.append(f"{traj_id}\t{snippet}{mark}")
    return "\n".join(rows)


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
        follow = None
        for assistant in assistants:
            if assistant.start > user.start:
                follow = assistant
                break
        if follow is not None and follow.start not in seen_answers:
            answer = _answer_snippet(_body(lines, follow))
            if answer:
                seen_answers.add(follow.start)
                rows.append(f"L{follow.start} 答: {answer}")
    if folded:
        rows.append(f"（另有 {folded} 条重复问句已折叠）")

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
    footer = (
        f'精读：xskill traj read {traj_id} --offset-start <上面的 L 行号>'
    )

    def assemble(body_rows: list[str]) -> str:
        parts = [header, *body_rows]
        if tail:
            parts.append(tail)
        parts.append(footer)
        return "\n".join(parts)

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


def lookup_traj_path(
    traj_id: str,
    dataset_dirs: list[tuple[str, Path]] | None = None,
) -> Path | None:
    for _user, path in iter_traj_md(dataset_dirs):
        if path.stem == traj_id:
            return path
    return None


def format_cards(
    traj_ids: list[str],
    *,
    dataset_dirs: list[tuple[str, Path]] | None = None,
    leftover: int = 0,
    query: str = "",
    page: int = 1,
    extra_flags: str = "",
) -> str:
    chunks: list[str] = []
    for traj_id in traj_ids:
        path = lookup_traj_path(traj_id, dataset_dirs)
        if path is None:
            chunks.append(
                f"--- {traj_id} ---\nerror: 找不到这条轨迹。先用 xskill traj search 确认 id。"
            )
            continue
        try:
            chunks.append(render_card(path))
        except OSError as error:
            chunks.append(f"--- {traj_id} ---\nerror: 读失败 {error}")
    head = f"cards={len(traj_ids)}（卡片只是索引，不算精读）"
    text = head + "\n\n" + "\n\n".join(chunks)
    if leftover > 0 and query:
        flag = f" {extra_flags}" if extra_flags else ""
        text += (
            f"\n\n还有 {leftover} 张未列出。"
            f" 下一页：xskill traj search {_cli_query_token(query)}"
            f" --cards --page {page + 1}{flag}"
        )
    return text


def public_hits_without_path(hits: list[TrajHit]) -> list[dict[str, Any]]:
    return [hit_to_public(hit) for hit in hits]
