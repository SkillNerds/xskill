"""xskill.ecosystems — 把蒸馏出的 Skill 装进各 AI-agent 生态 + 轨迹格式适配。

包结构（一平台一文件，每平台的「轨迹适配（读）」与「安装/ingest（写）」同处一个文件）：
  _shared.py    — 跨平台共享件：EcosystemSpec / SqliteEcosystemSpec、
                  detect_known_ecosystems、JsonlIngester 基类、_install_skill_into、
                  adapt_trajectory / submit_trajectory / generate_traj_id 分发层
  claude_code.py— Claude Code：_adapt_claude_code_jsonl + install_to_claude_code
                  + ingest_claude_code_sessions + CCSessionIngester
  codex.py      — Codex CLI：适配 + install/ingest
  nga3.py       — nga3 / CodeAgent3：适配 + install/ingest
  cursor.py     — Cursor：适配 + install/ingest
  deepseek_harness.py — DeepSeek Harness (dsh)：适配 + install/ingest
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
    bridge_dir_for,
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
    ensure_claude_code_install,
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
from xskill.ecosystems.nga3 import (
    NGA3_SPEC,
    install_to_nga3,
    install_all_to_nga3,
    ingest_nga3_sessions,
    _nga3_projects_path,
    _nga3_skills_path,
    _nga3_session_id_from_path,
    _read_cwd_from_nga3_jsonl_content,
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
from xskill.ecosystems.deepseek_harness import (
    DSH_SPEC,
    ensure_zstandard_for_dsh,
    install_to_deepseek_harness,
    install_all_to_deepseek_harness,
    ingest_deepseek_harness_sessions,
    _dsh_sessions_path,
    _dsh_skills_path,
    _dsh_session_id_from_path,
    _read_cwd_from_dsh_jsonl,
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
from xskill.ecosystems.installation import (
    COPY_INSTALL_MARKER_NAME,
    GitHeadError,
    InstallSafetyError,
    InstallMode,
    InstallationMetadataError,
    copy_install_identity_matches,
    copy_install_is_current,
    ensure_link_install_metadata,
    install_metadata_path,
    installed_mode,
    is_link_or_junction,
    link_install_metadata_is_current,
    read_copy_install_baseline,
    read_install_metadata,
    read_install_metadata_file,
    read_skill_head_sha,
    write_install_metadata,
)
from xskill.ecosystems.install_ledger import (
    InstallLedger,
    get_default_ledger,
    remove_owned_dest,
)

# sqlite-back 生态 id → spec 映射（`xskill read` / 上传入库按 --eco 选 spec）。
# 只含 source_kind="sqlite" 的生态；JSONL 生态（cc/codex/...）不在此表。
SQLITE_SPEC_BY_ECO: dict = {
    "opencode": OPENCODE_SPEC,
    "ngagent": NGAGENT_SPEC,
}

__all__ = [
    "EcosystemSpec", "SqliteEcosystemSpec",
    "CC_SPEC", "CODEX_SPEC", "NGA3_SPEC", "OPENCLAW_SPEC", "CURSOR_SPEC",
    "DSH_SPEC",
    "OPENCODE_SPEC", "NGAGENT_SPEC",
    "SQLITE_SPEC_BY_ECO", "bridge_dir_for",
    "JsonlIngester", "SqliteIngester", "CCSessionIngester",
    "detect_known_ecosystems",
    "ensure_claude_code_install",
    "ensure_zstandard_for_dsh",
    "install_to_claude_code", "install_to_codex", "install_to_nga3",
    "install_to_cursor",
    "install_to_deepseek_harness",
    "install_to_trae",
    "install_to_openclaw", "install_to_opencode", "install_to_ngagent",
    "install_all_to_claude_code", "install_all_to_codex",
    "install_all_to_nga3", "install_all_to_cursor",
    "install_all_to_deepseek_harness", "install_all_to_trae",
    "install_all_to_openclaw",
    "install_all_to_opencode", "install_all_to_ngagent",
    "ingest_claude_code_sessions", "ingest_codex_sessions",
    "ingest_nga3_sessions", "ingest_cursor_sessions",
    "ingest_deepseek_harness_sessions", "ingest_trae_sessions",
    "ingest_openclaw_sessions",
    "TraeIngester", "detect_trae_record",
    "make_openclaw_canary_flip_hook",
    "adapt_trajectory", "submit_trajectory", "generate_traj_id",
    "COPY_INSTALL_MARKER_NAME", "GitHeadError", "InstallMode",
    "InstallSafetyError",
    "InstallationMetadataError",
    "copy_install_identity_matches", "copy_install_is_current",
    "install_metadata_path", "installed_mode",
    "ensure_link_install_metadata",
    "is_link_or_junction", "read_install_metadata",
    "read_copy_install_baseline",
    "read_install_metadata_file", "read_skill_head_sha",
    "link_install_metadata_is_current",
    "write_install_metadata",
    "InstallLedger", "get_default_ledger", "remove_owned_dest",
    # Compatibility re-exports used by historical callers and tests.
    "_agents_skills_path", "_sanitize_for_filename",
    "_cc_projects_path", "_cc_skills_path", "_cc_traj_id",
    "_codex_sessions_path", "_codex_session_id_from_path",
    "_read_cwd_from_codex_jsonl",
    "_nga3_projects_path", "_nga3_skills_path",
    "_nga3_session_id_from_path", "_read_cwd_from_nga3_jsonl_content",
    "_cursor_projects_path", "_cursor_skills_path",
    "_cursor_session_id_from_path", "_read_cwd_from_cursor_jsonl",
    "_dsh_sessions_path", "_dsh_skills_path",
    "_dsh_session_id_from_path", "_read_cwd_from_dsh_jsonl",
    "_trae_skills_roots", "_trae_workspace_storage_roots",
    "_sessions_from_chat_blob",
    "_openclaw_agents_path", "_openclaw_session_id_from_path",
    "_read_workspace_dir_from_openclaw_jsonl",
    "_opencode_db_path", "_ngagent_db_path", "_ngagent_skills_path",
]
