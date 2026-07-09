from __future__ import annotations

import pytest

from xskill.team.client.state import ClientState, save_client_state, load_client_state


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "team_client.json"
    st = ClientState(server_url="http://1.2.3.4:8000", client_id="cid-1",
                     join_token="tok")
    save_client_state(st, p)
    back = load_client_state(p)
    assert back == st


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_client_state(tmp_path / "absent.json")


def test_save_then_load_roundtrip_with_pypi_url(tmp_path):
    p = tmp_path / "team_client.json"
    st = ClientState(server_url="http://1.2.3.4:8000", client_id="cid-1",
                     join_token="tok", pypi_url="http://mirror/simple")
    save_client_state(st, p)
    back = load_client_state(p)
    assert back == st


def test_load_old_format_without_pypi_url_defaults_none(tmp_path):
    """老版本写的 team_client.json 没有 pypi_url 字段，读取时应默认 None 而不是报错。"""
    import json
    p = tmp_path / "team_client.json"
    p.write_text(json.dumps({
        "server_url": "http://1.2.3.4:8000", "client_id": "cid-1", "join_token": "tok",
    }))
    back = load_client_state(p)
    assert back.pypi_url is None
