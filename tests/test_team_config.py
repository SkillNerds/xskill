from __future__ import annotations

from xskill import config as C


def test_team_paths_under_xskill_home():
    assert C.get_team_server_state_path() == C.XSKILL_HOME / "team_server.json"
    assert C.get_team_clients_db_path() == C.XSKILL_HOME / "team_clients.db"
    assert C.get_team_client_state_path() == C.XSKILL_HOME / "team_client.json"


# ── team.server 槽位 parser(热生效的唯一来源) ──────────────────────
# 这几个值曾被 init_team_context 快照进 api._ctx（只在 serve 启动时填一次），
# 导致面板改完必须重启才生效。现在改由读方每请求现取 → 必须 fail-loud 校验。

def test_team_server_slots_defaults():
    assert C.team_server_slots_config({}) == {"skill_slots": 100, "ranked_slots": 80}
    assert C.team_server_slots_config(None) == {"skill_slots": 100, "ranked_slots": 80}


def test_team_server_slots_reads_configured_values():
    cfg = {"team": {"server": {"skill_slots": 7, "ranked_slots": 3}}}
    assert C.team_server_slots_config(cfg) == {"skill_slots": 7, "ranked_slots": 3}


def test_team_server_slots_zero_means_distribution_disabled():
    """skill_slots=0 是合法的"停止分发"配置(team_sync 直接短路),
    此时 ranked_slots 仍是常规 80——不得因 ranked>skill 就误拒。"""
    cfg = {"team": {"server": {"skill_slots": 0}}}
    assert C.team_server_slots_config(cfg) == {"skill_slots": 0, "ranked_slots": 80}


def test_team_server_slots_rejects_non_int():
    import pytest
    for bad in ("100", 1.5, True, None):
        with pytest.raises(ValueError, match="必须是整数"):
            C.team_server_slots_config(
                {"team": {"server": {"skill_slots": bad}}})


def test_team_server_slots_rejects_negative():
    import pytest
    with pytest.raises(ValueError, match="不能为负"):
        C.team_server_slots_config(
            {"team": {"server": {"ranked_slots": -1}}})


def test_team_server_slots_bare_team_key_means_defaults():
    """回归:YAML 里一个光杆 `team:` 会解析成 {"team": None}——
    `.get("team", {})` 的默认值**不生效**(键存在),会 None.get → AttributeError
    穿透调用方的 except ValueError,把 /admin/config/reload 的 400 变成 500,
    并让每次 /sync 都炸。光杆 team: = 没配 → 走默认值。"""
    assert C.team_server_slots_config({"team": None}) == {
        "skill_slots": 100, "ranked_slots": 80}
    assert C.team_server_slots_config({"team": {"server": None}}) == {
        "skill_slots": 100, "ranked_slots": 80}
    # allow_anonymous_user 同一个段,同样不能炸(否则校验放行的 config 会砖掉 startup)
    assert C.allow_anonymous_user({"team": None}) is True
    assert C.allow_anonymous_user({"team": {"server": None}}) is True


def test_team_server_section_malformed_raises_valueerror_not_attributeerror():
    """畸形 team 段必须抛带原因的 ValueError(调用方据此返 400),
    而不是 AttributeError(会变成 500)。"""
    import pytest
    for cfg in ({"team": "foo"}, {"team": ["a"]}):
        with pytest.raises(ValueError, match="team 必须是 mapping"):
            C.team_server_slots_config(cfg)
    with pytest.raises(ValueError, match="team.server 必须是 mapping"):
        C.team_server_slots_config({"team": {"server": "foo"}})
    # allow_anonymous_user 走同一个 section 解析,行为一致
    with pytest.raises(ValueError, match="team 必须是 mapping"):
        C.allow_anonymous_user({"team": "foo"})


def test_resolve_team_client_skill_dir_relocates_when_colocated(tmp_path, monkeypatch):
    xhome = tmp_path / ".xskill"
    canonical = xhome / "skill"
    canonical.mkdir(parents=True)
    (xhome / "team_server.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(C, "XSKILL_HOME", xhome)
    monkeypatch.setattr(C, "get_team_server_state_path", lambda: xhome / "team_server.json")
    monkeypatch.setattr(C, "get_skill_dir", lambda: canonical)
    assert C.resolve_team_client_skill_dir(canonical) == xhome / "client_skill"
    other = tmp_path / "elsewhere" / "skill"
    other.mkdir(parents=True)
    assert C.resolve_team_client_skill_dir(other) == other


def test_resolve_team_client_skill_dir_unchanged_without_server(tmp_path, monkeypatch):
    xhome = tmp_path / ".xskill"
    canonical = xhome / "skill"
    canonical.mkdir(parents=True)
    monkeypatch.setattr(C, "XSKILL_HOME", xhome)
    monkeypatch.setattr(C, "get_team_server_state_path", lambda: xhome / "missing.json")
    monkeypatch.setattr(C, "get_skill_dir", lambda: canonical)
    assert C.resolve_team_client_skill_dir(canonical) == canonical
