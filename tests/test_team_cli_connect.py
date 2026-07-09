from __future__ import annotations

from xskill.cli import build_parser, cmd_connect


def test_connect_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(["connect", "1.2.3.4:8000", "--token", "tok",
                              "--label", "alice"])
    assert args.address == "1.2.3.4:8000"
    assert args.token == "tok"
    assert args.label == "alice"
    assert args.foreground is False  # 默认后台托管
    # 无参形式（复用已存连接）
    args2 = parser.parse_args(["connect"])
    assert args2.address is None
    # --foreground 显式前台阻塞
    args3 = parser.parse_args(["connect", "--foreground"])
    assert args3.foreground is True


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
    """拦截 httpx.Client 记录构造 kwargs；register 走真实逻辑但 post 被打桩。

    同时把 service 后端的后台托管打成 no-op——这些用例只关心握手时 httpx 的
    trust_env，不验证后台任务；托管留给 test_connect_service.py 专测。
    """
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
    # 默认（非 foreground）会调后端托管——打成返回一个假的 status，不真起任务
    monkeypatch.setattr(
        "xskill.team.client.service.get_backend",
        lambda: _FakeBackend(),
    )
    return captured


class _FakeBackend:
    name = "fake"
    supported = True

    def install_and_start(self):
        return {"running": True, "pid": 4242, "task_name": "Xskill_Connect",
                "backend": self.name}

    def stop(self):
        return {"running": False, "backend": self.name}

    def status(self):
        return {"running": True, "pid": 4242, "installed": True,
                "backend": self.name}


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
    # 复用已存连接、前台跑时的后台同步同样默认直连，避免"注册过了同步全 504"
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
    # --foreground 才会真正构造 httpx.Client 跑同步循环
    args = parser.parse_args(["connect", "--foreground"])
    rc = cmd_connect(args)
    assert rc == 0
    assert captured["trust_env"] is False


# ── 默认后台托管 vs --foreground 阻塞 ────────────────────────────

def test_connect_default_hands_off_to_backend(tmp_path, monkeypatch):
    """默认（非 foreground）握手成功后调后端 install_and_start，不阻塞。"""
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: tmp_path / "team_client.json")
    _install_fakes(monkeypatch)
    started = {"n": 0}
    backend = _FakeBackend()
    orig = backend.install_and_start

    def _spy():
        started["n"] += 1
        return orig()
    backend.install_and_start = _spy
    monkeypatch.setattr("xskill.team.client.service.get_backend",
                        lambda: backend)
    parser = build_parser()
    args = parser.parse_args(["connect", "1.2.3.4:8000", "--token", "t"])
    rc = cmd_connect(args)
    assert rc == 0
    assert started["n"] == 1  # 走了后台托管


def test_connect_foreground_runs_forever_not_backend(tmp_path, monkeypatch):
    """--foreground 走阻塞 run_forever，不碰后端托管。"""
    from xskill.team.client.state import ClientState, save_client_state
    state_path = tmp_path / "team_client.json"
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: state_path)
    save_client_state(
        ClientState(server_url="http://1.2.3.4:8000",
                    client_id="cid-1", join_token="t"),
        state_path,
    )
    ran = {"forever": 0}

    class _FakeTeam:
        def __init__(self, **kw):
            pass

        def run_forever(self):
            ran["forever"] += 1

    monkeypatch.setattr("httpx.Client", lambda **kw: object())
    monkeypatch.setattr("xskill.team.client.daemon.TeamClient", _FakeTeam)

    class _NoStartBackend(_FakeBackend):
        def install_and_start(self):
            raise AssertionError("foreground 不该调后端托管")
    monkeypatch.setattr("xskill.team.client.service.get_backend",
                        lambda: _NoStartBackend())

    parser = build_parser()
    args = parser.parse_args(["connect", "--foreground"])
    rc = cmd_connect(args)
    assert rc == 0
    assert ran["forever"] == 1


def test_connect_unsupported_platform_falls_back_to_foreground(
    tmp_path, monkeypatch,
):
    """Linux/macOS 尚无原生后端：默认 connect 应退化成前台阻塞（历史行为），
    而不是报错——用户仍可用自己的 init 系统托管。"""
    from xskill.team.client.state import ClientState, save_client_state
    state_path = tmp_path / "team_client.json"
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: state_path)
    save_client_state(
        ClientState(server_url="http://1.2.3.4:8000",
                    client_id="cid-1", join_token="t"),
        state_path,
    )
    ran = {"forever": 0}

    class _FakeTeam:
        def __init__(self, **kw):
            pass

        def run_forever(self):
            ran["forever"] += 1

    class _Unsupported(_FakeBackend):
        supported = False

        def install_and_start(self):
            raise AssertionError("不支持的平台不该走后台托管")

    monkeypatch.setattr("httpx.Client", lambda **kw: object())
    monkeypatch.setattr("xskill.team.client.daemon.TeamClient", _FakeTeam)
    monkeypatch.setattr("xskill.team.client.service.get_backend",
                        lambda: _Unsupported())

    parser = build_parser()
    args = parser.parse_args(["connect"])  # 无 --foreground
    rc = cmd_connect(args)
    assert rc == 0
    assert ran["forever"] == 1  # 退化成前台阻塞


# ── --pypi-url：内网镜像地址落盘，供自动更新回退用 ──

def test_pypi_url_flag_parses():
    parser = build_parser()
    args = parser.parse_args(["connect", "1.2.3.4:8000", "--token", "t"])
    assert args.pypi_url is None
    args2 = parser.parse_args(
        ["connect", "1.2.3.4:8000", "--token", "t",
         "--pypi-url", "http://mirror/simple"])
    assert args2.pypi_url == "http://mirror/simple"


def test_pypi_url_persisted_on_first_connect(tmp_path, monkeypatch):
    from xskill.team.client.state import load_client_state
    state_path = tmp_path / "team_client.json"
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: state_path)
    _install_fakes(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        ["connect", "1.2.3.4:8000", "--token", "t",
         "--pypi-url", "http://mirror/simple"])
    rc = cmd_connect(args)
    assert rc == 0
    assert load_client_state(state_path).pypi_url == "http://mirror/simple"


def test_pypi_url_preserved_on_reconnect_without_flag(tmp_path, monkeypatch):
    """重连时不重复传 --pypi-url，应沿用上次已存的镜像地址，而不是被清空。"""
    from xskill.team.client.state import ClientState, load_client_state, save_client_state
    state_path = tmp_path / "team_client.json"
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: state_path)
    save_client_state(
        ClientState(server_url="http://1.2.3.4:8000", client_id="cid-1",
                    join_token="old-tok", pypi_url="http://mirror/simple"),
        state_path,
    )
    _install_fakes(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(["connect", "1.2.3.4:8000", "--token", "t"])
    rc = cmd_connect(args)
    assert rc == 0
    assert load_client_state(state_path).pypi_url == "http://mirror/simple"
