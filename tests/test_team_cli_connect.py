from __future__ import annotations

from xskill.cli import build_parser, cmd_connect


def test_connect_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(["connect", "1.2.3.4:8000", "--token", "tok",
                              "--label", "alice"])
    assert args.address == "1.2.3.4:8000"
    assert args.token == "tok"
    assert args.label == "alice"
    # 无参形式（复用已存连接）
    args2 = parser.parse_args(["connect"])
    assert args2.address is None


def test_connect_no_address_no_saved_state_errors(tmp_path, monkeypatch, capsys):
    # 无 address 且无 team_client.json → 返回非 0
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: tmp_path / "absent.json")
    parser = build_parser()
    args = parser.parse_args(["connect"])
    rc = cmd_connect(args)
    assert rc != 0


def test_connect_with_address_requires_token():
    parser = build_parser()
    args = parser.parse_args(["connect", "1.2.3.4:8000"])  # 没 --token
    rc = cmd_connect(args)
    assert rc != 0


# ── 代理绕行：默认直连（trust_env=False）绕开公司 SWG，--use-proxy 时恢复读取 ──

class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"client_id": "cid-1"}


def _install_fakes(monkeypatch):
    """拦截 httpx.Client 记录构造 kwargs；register 走真实逻辑但 post 被打桩。"""
    captured: dict = {}

    class _Client:
        def __init__(self, **kw):
            captured.update(kw)

        def post(self, *a, **k):
            return _Resp()

    class _FakeTeam:
        def __init__(self, **kw):
            pass

        def run_forever(self):  # 不阻塞
            return None

    monkeypatch.setattr("httpx.Client", _Client)
    monkeypatch.setattr("xskill.team.client.daemon.TeamClient", _FakeTeam)
    return captured


def test_use_proxy_flag_defaults_false():
    parser = build_parser()
    args = parser.parse_args(["connect", "1.2.3.4:8000", "--token", "t"])
    assert args.use_proxy is False
    args2 = parser.parse_args(
        ["connect", "1.2.3.4:8000", "--token", "t", "--use-proxy"])
    assert args2.use_proxy is True


def test_connect_register_bypasses_proxy_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: tmp_path / "team_client.json")
    captured = _install_fakes(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(["connect", "1.2.3.4:8000", "--token", "t"])
    rc = cmd_connect(args)
    assert rc == 0
    assert captured["trust_env"] is False  # 默认绕开公司代理


def test_connect_register_honors_proxy_with_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: tmp_path / "team_client.json")
    captured = _install_fakes(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        ["connect", "1.2.3.4:8000", "--token", "t", "--use-proxy"])
    rc = cmd_connect(args)
    assert rc == 0
    assert captured["trust_env"] is True  # 显式要求走代理


def test_connect_reuse_path_also_bypasses_proxy(tmp_path, monkeypatch):
    # 复用已存连接的后台同步同样默认直连，避免"注册过了同步全 504"
    from xskill.team.client.state import ClientState, save_client_state
    state_path = tmp_path / "team_client.json"
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: state_path)
    save_client_state(
        ClientState(server_url="http://1.2.3.4:8000",
                    client_id="cid-1", join_token="t"),
        state_path,
    )
    captured = _install_fakes(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(["connect"])  # 无 address → 复用分支
    rc = cmd_connect(args)
    assert rc == 0
    assert captured["trust_env"] is False
