"""
skill/frontmatter.py -- YAML frontmatter parser/serializer for SKILL.md v2
====================================================================
Small helper module to round-trip `---\\n<yaml>\\n---\\n<body>` markdown files.

Follows the Anthropic Agent Skill open standard:
- Opening fence is `---` on the first line
- Closing fence is `---` on its own line
- YAML between fences becomes a dict
- Everything after the closing fence (with one leading newline eaten) is body

The round-trip property: `parse(serialize(fm, body)) == (fm, body)` for any
dict/str pair where `body` starts with content (not a raw `---`).
"""

from __future__ import annotations

import yaml


class FrontmatterError(ValueError):
    """Raised by :func:`parse_strict` when a SKILL.md frontmatter is illegal.

    Carries a human-readable reason and, where available, the 1-based line
    number inside the source text at which the problem was detected. The
    string form should read like a lint message so the calling agent (or a
    human) can fix the file without re-deriving the cause.

    Attributes
    ----------
    reason:
        Short description of why the frontmatter is rejected.
    line:
        1-based line number in the original text, or ``None`` if not known.
    """

    def __init__(self, reason: str, line: int | None = None) -> None:
        self.reason = reason
        self.line = line
        if line is not None:
            super().__init__(f"line {line}: {reason}")
        else:
            super().__init__(reason)


def _require_opening_fence(text: str, lines: list[str]) -> None:
    if not text or not text.startswith("---"):
        raise FrontmatterError(
            "缺少开头的 `---` frontmatter 围栏（文件首行必须是 `---`）", 1
        )
    if lines[0].rstrip("\r").strip() != "---":
        raise FrontmatterError("首行必须正好是 `---`（不能有多余字符）", 1)


def _find_closing_fence(lines: list[str]) -> int:
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r").strip() == "---":
            return i
    raise FrontmatterError(
        "缺少结尾的 `---` frontmatter 围栏（需要一行单独的 `---` 收尾）", None
    )


def _split_strict_parts(lines: list[str], closing_idx: int) -> tuple[str, str]:
    yaml_block = "\n".join(lines[1:closing_idx])
    body_lines = lines[closing_idx + 1:]
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    return yaml_block, "\n".join(body_lines)


def _load_strict_yaml(yaml_block: str) -> dict:
    if not yaml_block.strip():
        raise FrontmatterError("frontmatter 为空：必须包含 name 与 description", 2)
    try:
        fm = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        # PyYAML 标 0-based 行号，且相对 yaml_block；+1 转 1-based，再 +1 跨过首行围栏
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 2) if mark is not None else None
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise FrontmatterError(f"frontmatter 不是合法 YAML：{problem}", line) from exc
    if not isinstance(fm, dict):
        raise FrontmatterError(
            f"frontmatter 必须解析为 mapping（键值对），实得 {type(fm).__name__}", 2
        )
    return fm


def _validate_required_fields(fm: dict) -> None:
    if "name" not in fm:
        raise FrontmatterError("缺少必填字段 name", None)
    if not str(fm.get("name") or "").strip():
        raise FrontmatterError("name 不能为空", None)

    if "description" not in fm:
        raise FrontmatterError("缺少必填字段 description", None)
    desc = fm.get("description")
    if not isinstance(desc, str):
        raise FrontmatterError(
            "description 必须是字符串；多行描述需用块标量 `|` 或加引号包裹"
            f"（当前被 YAML 解析成 {type(desc).__name__}）",
            None,
        )
    if not desc.strip():
        raise FrontmatterError("description 不能为空字符串", None)


def parse_strict(text: str) -> tuple[dict, str]:
    """Strictly parse a SKILL.md-style file into (frontmatter_dict, body_str).

    Unlike :func:`parse`, this entry point NEVER silently passes through a
    malformed file. It raises :class:`FrontmatterError` (with a line number
    and reason where possible) whenever the frontmatter is illegal, and only
    returns ``(fm, body)`` for a fully valid file.

    Validation contract (to be implemented by the programming sub-agent):
      - The file MUST open with a `---` fence and have a closing `---` fence;
        the block between fences MUST be valid YAML parsing to a mapping.
      - Required keys ``name`` and ``description`` MUST be present.
      - ``description`` MUST be a non-empty string (this specifically catches
        multi-line descriptions written without a block scalar / quotes, which
        YAML parses into a broken or non-string value).
      - ``body`` (everything after the closing fence) MUST be non-empty.

    On success returns ``(fm, body)``; the lax :func:`parse` is left untouched
    for read paths that must tolerate legacy/partial files.
    """
    lines = text.split("\n")
    _require_opening_fence(text, lines)
    yaml_block, body = _split_strict_parts(lines, _find_closing_fence(lines))
    fm = _load_strict_yaml(yaml_block)
    _validate_required_fields(fm)

    if not body.strip():
        raise FrontmatterError(
            "body 为空：frontmatter 之后必须有正文内容", None
        )

    return fm, body


def parse(text: str) -> tuple[dict, str]:
    """Parse a SKILL.md-style file into (frontmatter_dict, body_str).

    If no valid frontmatter is present, returns ({}, text) and leaves the
    input verbatim. Handles:
      - Missing opening `---`                -> ({}, text)
      - Missing closing `---`                -> ({}, text)  (treat as no frontmatter)
      - Empty frontmatter block              -> ({}, body)
      - Non-dict YAML (e.g. list, scalar)    -> ({}, text)  (invalid for our use)
    """
    if not text:
        return {}, ""

    # Normalize line endings a little; keep body bytes intact otherwise.
    # We only split on \n so Windows \r\n will be preserved inside the body.
    if not text.startswith("---"):
        return {}, text

    lines = text.split("\n")
    # First line must be exactly '---' (optionally with trailing CR).
    if lines[0].rstrip("\r").strip() != "---":
        return {}, text

    # Find the closing '---'
    closing_idx = -1
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r").strip() == "---":
            closing_idx = i
            break

    if closing_idx == -1:
        return {}, text

    yaml_block = "\n".join(lines[1:closing_idx])
    body_lines = lines[closing_idx + 1:]
    # Eat a single leading blank line after the closing fence, if present,
    # so that on the next serialize we can insert one cleanly without stacking.
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)

    try:
        fm = yaml.safe_load(yaml_block) if yaml_block.strip() else {}
    except yaml.YAMLError:
        return {}, text

    if not isinstance(fm, dict):
        return {}, text

    return fm, body


def serialize(fm: dict, body: str) -> str:
    """Serialize (frontmatter_dict, body_str) back into a SKILL.md string.

    Layout:
        ---
        <yaml>
        ---
        <body>

    If `fm` is empty/None, returns `body` unchanged (no fences).
    """
    if not fm:
        return body or ""

    yaml_text = yaml.safe_dump(
        fm,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    # yaml.safe_dump always ends with a single \n; strip to avoid double blanks.
    yaml_text = yaml_text.rstrip("\n")

    body = body or ""
    if not body.startswith("\n"):
        body_sep = "\n"
    else:
        body_sep = ""

    return f"---\n{yaml_text}\n---\n{body_sep}{body}"


def update_frontmatter(text: str, patch: dict) -> str:
    """Convenience: parse, deep-merge patch into frontmatter, re-serialize,
    body preserved byte-for-byte.
    """
    fm, body = parse(text)
    _deep_merge(fm, patch)
    return serialize(fm, body)


def _deep_merge(dst: dict, src: dict) -> None:
    """In-place recursive merge. src wins on scalar conflicts."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
