"""tests/test_log_setup.py — log file routing"""
from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """每个 test 跑完把 root logger handlers 清干净——xskill 装的 handler
    会污染下一个 test 的 caplog 等。"""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_xskill_managed", False):
            root.removeHandler(h)
    # 清掉所有 named logger 上 xskill 加的 handler
    for name in list(logging.Logger.manager.loggerDict.keys()):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            if getattr(h, "_xskill_managed", False):
                lg.removeHandler(h)


def test_creates_log_files_for_each_component(tmp_path):
    from xskill.utils.logging import configure_logging
    configure_logging(tmp_path, debug=False, quiet=False, stdout=False)

    logging.getLogger("xskill.watcher").info("watcher said hi")
    logging.getLogger("xskill.canary").info("canary said hi")
    logging.getLogger("agno").warning("agno noise")
    logging.getLogger("xskill.ecosystems").info("ecosystems said hi")
    logging.getLogger("xskill.kernel.openearth").info(
        "run_id=probe stage=run_started"
    )

    # 强制 flush（RotatingFileHandler 用 buffered IO）
    for h in logging.getLogger().handlers:
        h.flush()
    for name in (
        "xskill.watcher", "xskill.canary", "agno", "xskill.ecosystems",
        "xskill.kernel",
    ):
        for h in logging.getLogger(name).handlers:
            h.flush()

    assert (tmp_path / "xskill.watcher.log").read_text(encoding="utf-8").find("watcher said hi") >= 0
    assert (tmp_path / "xskill.canary.log").read_text(encoding="utf-8").find("canary said hi") >= 0
    assert (tmp_path / "agno.log").read_text(encoding="utf-8").find("agno noise") >= 0
    assert (tmp_path / "xskill.ecosystems.log").read_text(encoding="utf-8").find("ecosystems said hi") >= 0
    kernel_log = (tmp_path / "xskill.kernel.log").read_text(encoding="utf-8")
    assert "stage=run_started" in kernel_log
    assert "xskill.kernel.openearth" in kernel_log


def test_xskill_log_aggregates_all_xskill_messages(tmp_path):
    """xskill.* 下游所有子 logger 的消息都应当冒泡进 xskill.log 全合并视图。"""
    from xskill.utils.logging import configure_logging
    configure_logging(tmp_path, debug=False, quiet=False, stdout=False)

    logging.getLogger("xskill.watcher").info("from watcher")
    logging.getLogger("xskill.canary").info("from canary")
    logging.getLogger("xskill.kernel.openearth").info("from openearth")

    for h in logging.getLogger().handlers:
        h.flush()
    for name in ("xskill", "xskill.watcher", "xskill.canary", "xskill.kernel"):
        for h in logging.getLogger(name).handlers:
            h.flush()

    aggregate = (tmp_path / "xskill.log").read_text(encoding="utf-8")
    assert "from watcher" in aggregate
    assert "from canary" in aggregate
    assert "from openearth" in aggregate


def test_idempotent_no_duplicate_handlers(tmp_path):
    from xskill.utils.logging import configure_logging
    configure_logging(tmp_path, stdout=False)
    n_first = sum(1 for h in logging.getLogger().handlers
                  if getattr(h, "_xskill_managed", False))
    configure_logging(tmp_path, stdout=False)
    n_second = sum(1 for h in logging.getLogger().handlers
                   if getattr(h, "_xskill_managed", False))
    assert n_first == n_second, "calling configure_logging twice should not add new handlers"


def test_quieter_loggers_default_to_warning(tmp_path):
    from xskill.utils.logging import configure_logging
    configure_logging(tmp_path, stdout=False)
    for noisy in ("httpx", "httpcore", "openai"):
        assert logging.getLogger(noisy).level == logging.WARNING, noisy


def test_debug_flag_sets_root_to_debug(tmp_path):
    from xskill.utils.logging import configure_logging
    configure_logging(tmp_path, debug=True, stdout=False)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("xskill.watcher").level == logging.DEBUG


# ── 日志有效性三判据（0.6.1a1 子项目 C）────────────────────────────

def _flush_all():
    for name in list(logging.Logger.manager.loggerDict.keys()) + [""]:
        for h in logging.getLogger(name).handlers:
            try:
                h.flush()
            except Exception:  # pylint: disable=broad-exception-caught
                logging.getLogger(__name__).debug(
                    "handler flush failed", exc_info=True,
                )


# (primary logger, declared file, level) —— 每个声明文件至少有一个主 logger 往里写
# 只列真会写 INFO 的 logger——死/只 WARN 的(task_agent/task_cluster_agent/
# ux_score/registry)已不单开文件,故不在此矩阵里。
_COMPONENT_MATRIX = [
    ("xskill", "xskill.log", logging.INFO),
    ("xskill.watcher", "xskill.watcher.log", logging.INFO),
    ("xskill.process", "xskill.watcher.log", logging.INFO),
    ("xskill.server", "xskill.server.log", logging.INFO),
    ("xskill.canary", "xskill.canary.log", logging.INFO),
    ("xskill.ecosystems", "xskill.ecosystems.log", logging.INFO),
    ("xskill.skill_edit_agent", "xskill.skill_edit_agent.log", logging.INFO),
    ("xskill.kernel", "xskill.kernel.log", logging.INFO),
    ("agno", "agno.log", logging.WARNING),
    ("httpx", "httpx.log", logging.WARNING),
]


def test_delay_no_empty_file_for_silent_logger(tmp_path):
    """best practice 回归：配置后但没往某 component 写过 → 它的 .log **不存在**
    （delay=True 让文件只在第一次真写入时才创建,死/事件型 logger 永不产空文件）。"""
    from xskill.utils.logging import configure_logging
    configure_logging(tmp_path, debug=False, stdout=False)
    # 只往 watcher 写,不碰 canary / ecosystems / skill_edit
    logging.getLogger("xskill.watcher").info("only watcher writes")
    _flush_all()
    assert (tmp_path / "xskill.watcher.log").is_file()           # 写了 → 有
    assert not (tmp_path / "xskill.canary.log").exists()         # 没写 → 不建空文件
    assert not (tmp_path / "xskill.ecosystems.log").exists()
    assert not (tmp_path / "xskill.skill_edit_agent.log").exists()
    assert not (tmp_path / "xskill.kernel.log").exists()


def test_no_dead_logger_files_declared():
    """回归：不再声明"死 logger"专属文件（源码从不打 INFO 的那些）——
    task_agent/task_cluster_agent 源码零 logger 调用,ux_score/registry 只 WARN,
    它们的真日志都在 xskill.watcher 名下。声明专属文件 = 永远 0 字节空文件。"""
    from xskill.utils.logging import _PER_LOGGER_FILES
    dead = {"xskill.task_agent", "xskill.task_cluster_agent",
            "xskill.ux_score", "xskill.registry"}
    assert dead.isdisjoint(_PER_LOGGER_FILES), (
        "死 logger 不该单开文件(会永远空): "
        f"{dead & set(_PER_LOGGER_FILES)}")


def test_judge1_no_empty_component_files(tmp_path):
    """判据①：每个声明的 .log 在对应主 logger 写一条后都非空——
    杜绝 unfunctional 空文件（旧版 canary.log 因 logger 名错配永远空）。"""
    from xskill.utils.logging import configure_logging
    configure_logging(tmp_path, debug=False, quiet=False, stdout=False)

    for logger_name, _fname, level in _COMPONENT_MATRIX:
        logging.getLogger(logger_name).log(level, "probe from %s", logger_name)
    _flush_all()

    declared = {f for _, f, _ in _COMPONENT_MATRIX}
    for fname in declared:
        fpath = tmp_path / fname
        assert fpath.is_file(), f"{fname} not created"
        assert fpath.read_text(encoding="utf-8").strip(), f"{fname} is empty (unfunctional)"


def test_judge2_no_cross_talk_between_components(tmp_path):
    """判据②：不串台——一个组件的日志不出现在别的组件文件里。
    watcher 的消息只进 watcher.log（+ 冒泡进 xskill.log 汇总），绝不进 server.log。"""
    from xskill.utils.logging import configure_logging
    configure_logging(tmp_path, debug=False, stdout=False)

    logging.getLogger("xskill.watcher").info("WATCHER_ONLY_MARKER")
    logging.getLogger("xskill.server").info("SERVER_ONLY_MARKER")
    _flush_all()

    server_log = (tmp_path / "xskill.server.log").read_text(encoding="utf-8")
    watcher_log = (tmp_path / "xskill.watcher.log").read_text(encoding="utf-8")
    assert "WATCHER_ONLY_MARKER" not in server_log, "watcher 串进了 server.log"
    assert "SERVER_ONLY_MARKER" not in watcher_log, "server 串进了 watcher.log"
    # 但两者都应冒泡进 xskill.log 汇总
    agg = (tmp_path / "xskill.log").read_text(encoding="utf-8")
    assert "WATCHER_ONLY_MARKER" in agg and "SERVER_ONLY_MARKER" in agg


def test_judge3_key_events_land_in_correct_file(tmp_path):
    """判据③：关键事件（拆分/cluster/edit/灰度决策/install）各自落到正确的
    component 文件——核心 agent 与灰度/生态事件可独立排查。"""
    from xskill.utils.logging import configure_logging
    configure_logging(tmp_path, debug=False, stdout=False)

    # 拆分/聚类的运行日志实际在 xskill.watcher 名下(runner.py),不是 agent 自己打。
    events = {
        "xskill.watcher": ("xskill.watcher.log", "EV_SPLIT_CLUSTER"),
        "xskill.skill_edit_agent": ("xskill.skill_edit_agent.log", "EV_EDIT"),
        "xskill.canary": ("xskill.canary.log", "EV_CANARY_DECISION"),
        "xskill.ecosystems": ("xskill.ecosystems.log", "EV_INSTALL"),
        "xskill.kernel.openearth": ("xskill.kernel.log", "stage=distillation_started"),
    }
    for logger_name, (_f, marker) in events.items():
        logging.getLogger(logger_name).info(marker)
    _flush_all()

    for _logger_name, (fname, marker) in events.items():
        content = (tmp_path / fname).read_text(encoding="utf-8")
        assert marker in content, f"{marker} 未落到 {fname}"
