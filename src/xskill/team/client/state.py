"""state.py — team client 端连接信息持久化（SP1）

瘦客户端不读 config.yaml（无 llm.api_key）。它要记住的只有连上谁：
server_url / client_id / join_token，落 ~/.xskill/team_client.json。

``xskill connect <addr> --token <t>`` 首次握手后写这个文件；后续
``xskill connect``（无参）直接读它复用连接。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ClientState:
    server_url: str          # 形如 http://1.2.3.4:8000
    client_id: str
    join_token: str
    pypi_url: str | None = None   # 内网 PyPI 镜像，供自动更新回退用；None=只用公网 PyPI


def save_client_state(state: ClientState, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    path.chmod(0o600)


def load_client_state(path: Path | str) -> ClientState:
    """读连接状态。文件不存在抛 FileNotFoundError（CLAUDE.md：遇问题 throw）。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"team client state not found: {path}\n"
            f"先跑一次 `xskill connect <host:port> --token <token>` 建立连接。"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return ClientState(
        server_url=data["server_url"],
        client_id=data["client_id"],
        join_token=data["join_token"],
        pypi_url=data.get("pypi_url"),
    )
