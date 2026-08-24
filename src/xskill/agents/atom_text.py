"""Deterministic text guards used by the TaskAgent Atom write boundary.

The helpers in this module deliberately avoid model, embedding, and network calls.
They operate on the short ``intent`` / ``summary`` fields and on user-message text,
so their cost stays linear in the number of submitted Atoms for one trajectory.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


_FENCED_CODE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_SHELL_PROMPT_LINE_RE = re.compile(
    r"^\s*(?:\$|%|PS>)\s+.*$",
    re.IGNORECASE | re.MULTILINE,
)
_BARE_COMMAND_LINE_RE = re.compile(
    r"^\s*(?:"
    r"git\s+(?:add|branch|checkout|clone|commit|diff|fetch|init|log|merge|pull|"
    r"push|rebase|remote|reset|restore|show|status|switch|tag|worktree)\b|"
    r"docker\s+(?:build|compose|exec|images|logs|ps|pull|push|run|stop)\b|"
    r"(?:npm|pnpm|yarn)\s+(?:add|build|install|run|start|test)\b|"
    r"(?:cargo|go)\s+(?:build|check|clean|fmt|install|run|test|vet)\b|"
    r"(?:pip|pytest|python\d*|uv)\s+(?:-[^\s]+|[^\s]*[/\\.][^\s]*)(?:\s+.*)?"
    r").*$",
    re.IGNORECASE | re.MULTILINE,
)
_URL_RE = re.compile(r"\b(?:https?|file)://\S+", re.IGNORECASE)
_PATH_TOKEN_RE = re.compile(
    r"(?<!\w)(?:[A-Za-z]:)?(?:[.~]?[/\\])?"
    r"(?:[\w@+-]+[/\\])+[\w.@+-]+|"
    r"(?<!\w)[\w@+-]+\."
    r"(?:py|js|jsx|ts|tsx|json|ya?ml|toml|md|sql|sh|ps1|go|rs|java|kt|"
    r"swift|c|cc|cpp|h|hpp)(?!\w)",
    re.IGNORECASE,
)
_TECHNICAL_IDENTIFIER_RE = re.compile(
    r"\b(?:"
    r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|"
    r"[A-Z][A-Z0-9_]{1,}|"
    r"[A-Za-z]+(?:[A-Z][A-Za-z0-9]*)+"
    r")\b"
)
_EN_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_ZH_PROSE_CUE_RE = re.compile(
    r"请|帮(?:我|忙)?|把|将|需要|能否|是否|如何|怎么|为什么|"
    r"修复|优化|增加|添加|删除|修改|更新|检查|支持|实现|保持|"
    r"改成|调整|继续|完成|生成|这个|那个|的"
)
_EN_PROSE_CUE_RE = re.compile(
    r"\b(?:please|can|could|would|should|fix|create|update|add|remove|"
    r"change|implement|improve|keep|continue|need|want|the|this|that)\b",
    re.IGNORECASE,
)

# Keep this list intentionally small: it normalizes grammatical filler and the
# generic verbs/nouns that produced the known #21 utility/helper duplicate.  It
# must not collapse domain modifiers such as ``csv`` / ``json`` or
# ``login`` / ``logout``.
_EN_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "is",
    "of",
    "on",
    "requested",
    "reusable",
    "same",
    "task",
    "the",
    "to",
    "turn",
    "turns",
}
_EN_CANONICAL = {
    "build": "create",
    "built": "create",
    "building": "create",
    "create": "create",
    "created": "create",
    "creating": "create",
    "helper": "helper",
    "helpers": "helper",
    "implement": "create",
    "implemented": "create",
    "make": "create",
    "utility": "helper",
    "utilities": "helper",
    "write": "create",
    "written": "create",
    "writing": "create",
}
_STRONG_CONTINUATION_RE = re.compile(
    r"(?:\b(?:continue|keep|still|same|the\s+(?:helper|utility))\b|"
    r"继续|还是|仍然|刚才|刚刚|同一个|不要另|补充|再完善)",
    re.IGNORECASE,
)

# Conservative thresholds calibrated against the checked-in replay's
# ``en-near-duplicate-observation`` case plus distinct CSV/JSON and
# login/logout controls in the unit tests.
_MIN_INTENT_TOKENS = 3
_INTENT_CONTAINMENT_MIN = 1.0
_COMBINED_CONTAINMENT_MIN = 0.75
_INTENT_JACCARD_MIN = 0.70


def strip_technical_text(text: str) -> str:
    """Remove code, command lines, URLs, and path-like tokens from ``text``."""
    natural = _FENCED_CODE_RE.sub(" ", str(text or ""))
    natural = _INLINE_CODE_RE.sub(" ", natural)
    natural = _SHELL_PROMPT_LINE_RE.sub(" ", natural)
    natural = _BARE_COMMAND_LINE_RE.sub(" ", natural)
    natural = _URL_RE.sub(" ", natural)
    return _PATH_TOKEN_RE.sub(" ", natural)


def detect_source_language(text: str) -> str:
    """Return ``zh``, ``en``, or ``unknown`` for natural-language text.

    Code-like identifiers are ignored and common prose cues disambiguate mixed
    technical requests before falling back to the replay baseline's 20% CJK
    threshold.
    """
    natural = _TECHNICAL_IDENTIFIER_RE.sub(" ", strip_technical_text(text))
    cjk = sum(
        "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff" for char in natural
    )
    latin = sum(char.isascii() and char.isalpha() for char in natural)
    other = sum(
        char.isalpha()
        and not char.isascii()
        and not ("\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff")
        for char in natural
    )
    if other > max(cjk, latin) or (
        other > 0 and other * 2 >= cjk and other >= latin
    ):
        return "unknown"
    total = cjk + latin
    if total == 0:
        return "unknown"
    zh_cue = bool(_ZH_PROSE_CUE_RE.search(natural))
    en_cue = bool(_EN_PROSE_CUE_RE.search(natural))
    if zh_cue != en_cue:
        return "zh" if zh_cue else "en"
    return "zh" if cjk / total >= 0.20 else "en"


def dominant_language(blocks: Iterable[str]) -> str:
    """Detect the dominant language across source user-message blocks."""
    return detect_source_language("\n".join(str(block) for block in blocks))


def output_language_matches(text: str, source_language: str) -> bool:
    """Whether output text is compatible with a detected source language.

    ``unknown`` output is accepted because short labels or technical names may not
    contain enough natural-language evidence.  A confidently opposite language is
    rejected at ``submit_atom`` so the agent can correct it before persistence.
    """
    expected = str(source_language or "unknown").lower()
    if expected not in {"en", "zh"}:
        return True
    actual = detect_source_language(text)
    return actual in {"unknown", expected}


def _lexical_tokens(text: str) -> set[str]:
    natural = strip_technical_text(text).lower()
    tokens: set[str] = set()
    for token in _EN_TOKEN_RE.findall(natural):
        if token in _EN_STOPWORDS:
            continue
        tokens.add(_EN_CANONICAL.get(token, token))
    for run in _CJK_RUN_RE.findall(natural):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _containment(left: set[str], right: set[str]) -> float:
    smaller = min(len(left), len(right))
    return len(left & right) / smaller if smaller else 0.0


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def adjacent_atoms_are_near_duplicates(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    current_user_text: str = "",
) -> bool:
    """High-precision lexical duplicate check for adjacent submitted Atoms.

    The check requires substantial intent and combined intent/summary containment.
    The smaller intent must be fully contained by the larger one and the new user
    turn must carry a strong continuation cue.  This avoids silently merging
    merely related sibling tasks that differ by one domain term.
    """
    previous_intent = _lexical_tokens(str(previous.get("intent") or ""))
    current_intent = _lexical_tokens(str(current.get("intent") or ""))
    if min(len(previous_intent), len(current_intent)) < _MIN_INTENT_TOKENS:
        return False
    if _containment(previous_intent, current_intent) < _INTENT_CONTAINMENT_MIN:
        return False

    previous_combined = _lexical_tokens(
        f"{previous.get('intent') or ''}\n{previous.get('summary') or ''}"
    )
    current_combined = _lexical_tokens(
        f"{current.get('intent') or ''}\n{current.get('summary') or ''}"
    )
    if _containment(previous_combined, current_combined) < _COMBINED_CONTAINMENT_MIN:
        return False

    if not _STRONG_CONTINUATION_RE.search(strip_technical_text(current_user_text)):
        return False
    return _jaccard(previous_intent, current_intent) >= _INTENT_JACCARD_MIN


def stable_union(left: Iterable[Any], right: Iterable[Any]) -> list[str]:
    """Return a normalized, order-preserving union of two metadata lists."""
    result: list[str] = []
    seen: set[str] = set()
    for values in (left, right):
        for value in values:
            normalized = str(value).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
    return result
