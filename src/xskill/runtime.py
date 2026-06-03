"""runtime.py — serve daemon 运行态(进程 / 端口 / 部署角色 / 处理模型)。

只服务于 ``xskill stats`` 的"谁在跑、谁在处理"展示。serve 启动时写
``~/.xskill/serve_runtime.json``,退出时清掉;stats 读它 + 校验 pid 存活。
纯旁路,读写失败一律忽略,不影响主流程。
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import time
from typing import Optional

import yaml

from xskill.config import (
    CONFIG_PATH, XSKILL_HOME,
    get_team_client_state_path, get_team_server_state_path,
)

logger = logging.getLogger("xskill.runtime")
RUNTIME_PATH = XSKILL_HOME / "serve_runtime.json"


def _models() -> dict:
    """从 config.yaml 直接读处理模型(不走 load_config,瘦客户端无 key 也能读)。"""
    try:
        c = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:  # pylint: disable=broad-exception-caught
        return {}
    llm, emb = c.get("llm", {}), c.get("embedding", {})
    return {"llm_model": llm.get("model"), "llm_base_url": llm.get("base_url"),
            "embed_model": emb.get("model")}


def write_running(*, port: int, mode: str) -> None:
    """serve 启动时调用,登记本进程;注册 atexit 自动清理。"""
    try:
        RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_PATH.write_text(json.dumps(
            {"pid": os.getpid(), "port": port, "mode": mode,
             "started_at": int(time.time()), **_models()}), encoding="utf-8")
        atexit.register(_clear)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("write runtime status failed", exc_info=True)


def _clear() -> None:
    try:
        RUNTIME_PATH.unlink(missing_ok=True)
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def _alive(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 存在但非本用户 → 仍算活
    return True


def role() -> str:
    """部署角色:server / client / standalone(看 team 状态文件)。"""
    if get_team_server_state_path().is_file():
        return "server"
    if get_team_client_state_path().is_file():
        return "client"
    return "standalone"


def read_status() -> dict:
    """汇总运行态供 stats 展示。daemon 不在跑也尽量给出角色 + 处理模型。"""
    st: dict = {"running": False, "role": role()}
    if RUNTIME_PATH.is_file():
        try:
            d = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        except Exception:  # pylint: disable=broad-exception-caught
            d = None
        if d and _alive(d.get("pid")):
            st.update(running=True, pid=d.get("pid"), port=d.get("port"),
                      mode=d.get("mode"), llm_model=d.get("llm_model"),
                      llm_base_url=d.get("llm_base_url"),
                      embed_model=d.get("embed_model"))
    if not st.get("llm_model"):     # daemon 没跑也补一份处理模型
        st.update(_models())
    return st
