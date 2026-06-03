"""xskill.ecosystems — 把蒸馏出的 Skill 装进各 AI-agent 生态 + 轨迹格式适配。

包结构（一平台一文件，每平台的「轨迹适配（读）」与「安装/ingest（写）」同处一个文件）：
  _shared.py    — 跨平台共享件：EcosystemSpec / SqliteEcosystemSpec、
                  detect_known_ecosystems、JsonlIngester 基类、_install_skill_into、
                  adapt_trajectory / submit_trajectory / generate_traj_id 分发层
  claude_code.py— Claude Code：_adapt_claude_code_jsonl + install_to_claude_code
                  + ingest_claude_code_sessions + CCSessionIngester
  codex.py      — Codex CLI：适配 + install/ingest
  cursor.py     — Cursor：适配 + install/ingest
  trae.py       — Trae IDE / Trae Agent：适配 + install/ingest
  openclaw.py   — OpenClaw：适配 + install/ingest + canary flip hook
  opencode.py   — OpenCode：SqliteIngester + install/ingest
  ngagent.py    — ngagent（opencode 企业分支）：复用 SqliteIngester + 独立 install 路径
  _fallback.py  — 跨平台目录安装的三阶 fallback
  _history.py   — daemon 自己装到 ~/.claude/skills/ 的 side 历史

本 ``__init__`` 只做 re-export：保持 ``from xskill.ecosystems import X``
对历史调用方（watcher / server / team / 测试）一致可用。
"""

from __future__ import annotations

from xskill.ecosystems._shared import (
    # specs
    EcosystemSpec,
    SqliteEcosystemSpec,
    # ingesters (base)
    JsonlIngester,
    # detection
    detect_known_ecosystems,
    # adapt / submit dispatch layer
    adapt_trajectory,
    submit_trajectory,
    generate_traj_id,
    # path helpers (used cross-package by watcher / team / tests)
    _agents_skills_path,
    # filename helper (used by tests)
    _sanitize_for_filename,
)
from xskill.ecosystems.claude_code import (
    CC_SPEC,
    CCSessionIngester,
    install_to_claude_code,
    install_all_to_claude_code,
    ingest_claude_code_sessions,
    _cc_projects_path,
    _cc_skills_path,
    _cc_traj_id,
)
from xskill.ecosystems.codex import (
    CODEX_SPEC,
    install_to_codex,
    install_all_to_codex,
    ingest_codex_sessions,
    _codex_sessions_path,
    _codex_session_id_from_path,
    _read_cwd_from_codex_jsonl,
)
from xskill.ecosystems.cursor import (
    CURSOR_SPEC,
    install_to_cursor,
    install_all_to_cursor,
    ingest_cursor_sessions,
    _cursor_projects_path,
    _cursor_skills_path,
    _cursor_session_id_from_path,
    _read_cwd_from_cursor_jsonl,
)
from xskill.ecosystems.trae import (
    TraeIngester,
    install_to_trae,
    install_all_to_trae,
    ingest_trae_sessions,
    detect_trae_record,
    _trae_skills_roots,
    _trae_workspace_storage_roots,
    _sessions_from_chat_blob,
)
from xskill.ecosystems.openclaw import (
    OPENCLAW_SPEC,
    install_to_openclaw,
    install_all_to_openclaw,
    ingest_openclaw_sessions,
    make_openclaw_canary_flip_hook,
    _openclaw_agents_path,
    _openclaw_session_id_from_path,
    _read_workspace_dir_from_openclaw_jsonl,
)
from xskill.ecosystems.opencode import (
    OPENCODE_SPEC,
    SqliteIngester,
    install_to_opencode,
    install_all_to_opencode,
    _opencode_db_path,
)
from xskill.ecosystems.ngagent import (
    NGAGENT_SPEC,
    install_to_ngagent,
    install_all_to_ngagent,
    _ngagent_db_path,
    _ngagent_skills_path,
)

__all__ = [
    "EcosystemSpec", "SqliteEcosystemSpec",
    "CC_SPEC", "CODEX_SPEC", "OPENCLAW_SPEC", "CURSOR_SPEC", "OPENCODE_SPEC",
    "NGAGENT_SPEC",
    "JsonlIngester", "SqliteIngester", "CCSessionIngester",
    "detect_known_ecosystems",
    "install_to_claude_code", "install_to_codex", "install_to_cursor",
    "install_to_trae",
    "install_to_openclaw", "install_to_opencode", "install_to_ngagent",
    "install_all_to_claude_code", "install_all_to_codex",
    "install_all_to_cursor", "install_all_to_trae",
    "install_all_to_openclaw",
    "install_all_to_opencode", "install_all_to_ngagent",
    "ingest_claude_code_sessions", "ingest_codex_sessions",
    "ingest_cursor_sessions", "ingest_trae_sessions",
    "ingest_openclaw_sessions",
    "TraeIngester", "detect_trae_record",
    "make_openclaw_canary_flip_hook",
    "adapt_trajectory", "submit_trajectory", "generate_traj_id",
]
