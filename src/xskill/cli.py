#!/usr/bin/env python3
"""
cli.py — xskill 紧凑 CLI
═══════════════════════════════════════════════════════
仅 5 个子命令（无 --no-watch / --no-ui / --skill-dir / --llm-* 这类散 flag）：
    xskill serve [--host] [--port]
    xskill registry add|remove|list <path>
    xskill search traj|skill <query> [--top-k]

所有筛选/格式化交给 shell（grep/awk）。状态/配置全在 ~/.xskill/。
"""

from __future__ import annotations

import argparse
import logging
import sys

from xskill import __version__
from xskill.config import set_overrides
from xskill.ecosystems import SQLITE_SPEC_BY_ECO


# ═══════════════════════════════════════════════════════════════
# 子命令
# ═══════════════════════════════════════════════════════════════

def cmd_serve(args, xskill) -> int:
    # --home 用于 debug 模式：生态扫描只看该目录下的 .claude/，不碰真实
    # $HOME。要求顶层 --debug 同时打开，避免生产环境误用。
    home_root = None
    if args.home:
        if not args.debug:
            print("error: --home 仅在 --debug 模式下生效；加 --debug 或去掉 --home",
                  file=sys.stderr)
            return 2
        from pathlib import Path
        home_root = Path(args.home).expanduser().resolve()
        if not home_root.is_dir():
            print(f"error: --home 目录不存在: {home_root}", file=sys.stderr)
            return 2
    from xskill.runtime import read_status, write_running
    # ── 单实例守卫：已有活 daemon 时拒绝启动 ──
    # 双 daemon 会抢同一 registry（rebuild 后旧 daemon 可能用旧模型抢先处理）。
    # read_status 已校验 pid 存活，陈旧运行态文件不会误拦。--force 强行接管。
    status = read_status()
    if status.get("running") and not args.force:
        print(
            f"✗ 已有 xskill daemon 在运行（pid {status.get('pid')}, "
            f"端口 {status.get('port')}）。",
            file=sys.stderr,
        )
        print(
            "  双 daemon 会抢同一 registry，导致换模型 rebuild 被旧 daemon 抢去用旧"
            "模型处理。\n  先停掉它再起；确认要强行接管可加 --force。",
            file=sys.stderr,
        )
        return 2
    write_running(port=args.port, mode="server" if args.server else "standalone")
    xskill.serve(host=args.host, port=args.port, home_root=home_root,
                 server_mode=args.server)
    return 0


def cmd_registry(args, xskill) -> int:
    action = args.registry_action
    if action == "add":
        wd = xskill.registry.add(args.path, label=args.label or "")
        print(f"Registered: {wd.path}  id={wd.id}  label={wd.label!r}")
        return 0
    if action == "remove":
        ok = xskill.registry.remove(args.path)
        print("Removed." if ok else "Not found.")
        return 0 if ok else 1
    if action == "list":
        dirs = xskill.registry.list()
        if not dirs:
            print("(no registered directories)")
            return 0
        # 列序: id  ecosystem  traj  indexed  label  path
        # ecosystem 是来源标签：``manual`` = 用户手动注册；其他如
        # ``claude_code`` = daemon 启动时自动 detect 出来的生态目录。
        # 同时用 codex / opencode 等其他工具时一眼能区分来源。
        # 表头与数据行都用 \t 分隔；解析方只取含 ecosystem 名的数据行即可。
        print("ID\tECOSYSTEM\tTRAJ\tINDEXED\tLABEL\tPATH")
        for w in dirs:
            print(
                f"{w.id}\t{w.ecosystem}\t{w.traj_count}\t{w.indexed_count}\t"
                f"{w.label or '-'}\t{w.path}"
            )
        return 0
    return 1


def _standalone_watch_dir_count() -> int:
    """轻量读 registry.db 里 watch_dirs 行数（不建表、不走 facade/config）。

    用于判断本机是否有 standalone/server 数据。库文件或表不存在都视作 0
    ——这是"尚未初始化"的正常状态，不是错误，故显式查表而非吞异常。
    """
    import sqlite3
    from xskill.config import get_registry_db_path
    db = get_registry_db_path()
    if not db.is_file():
        return 0
    conn = sqlite3.connect(str(db))
    try:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='watch_dirs'"
        ).fetchone()
        if not has_table:
            return 0
        return int(conn.execute("SELECT COUNT(*) FROM watch_dirs").fetchone()[0])
    finally:
        conn.close()


def cmd_registry_list_client() -> int:
    """team 客户端模式的 ``registry list``。

    瘦客户端不写 ``watch_dirs`` / ``trajectories`` 表（那是 standalone/server
    的存储），它靠实时 ``detect_known_ecosystems`` 采集 + client SQLite 状态
    记上传进度。所以这里**现算**视图：每个探测到的生态显示

        ECOSYSTEM  COLLECTED  UPLOADED  SOURCE

    - COLLECTED = 该生态 bridge 目录下 ``traj_*.json`` 数（已镜像采集的轨迹）
    - UPLOADED  = 上述轨迹里已记入 client_state.db（已上传 server）的数
    - SOURCE    = 用户真实的原生目录（如 ~/.claude/projects），非内部 bridge

    不依赖 config.yaml / XSkill 门面——纯客户端机器也能直接看。
    """
    from pathlib import Path
    from xskill.config import (
        XSKILL_HOME, get_team_client_state_path, get_team_client_cursor_path,
        get_team_client_state_db_path,
    )
    from xskill.ecosystems import detect_known_ecosystems
    from xskill.team.client.state import load_client_state
    from xskill.team.client.upload_state import TrajectoryUploadStateStore

    home = XSKILL_HOME.parent  # 与 XSKILL_HOME 同源,避免 home 解析漂移
    # 上传状态按 server 分目录（方案 A）——先读连接状态拿 server_url 才能定位。
    # 没连过 server（无 state）则没有任何上传状态，uploaded 全 0。
    uploaded_ids: set[str] = set()
    state_path = get_team_client_state_path()
    if state_path.is_file():
        state = load_client_state(state_path)
        store = TrajectoryUploadStateStore(
            db_path=get_team_client_state_db_path(state.server_url),
            legacy_cursor_path=get_team_client_cursor_path(state.server_url),
            home_root=home,
        )
        uploaded_ids = store.uploaded_trajectory_ids()

    dets = detect_known_ecosystems(home_root=home)
    if not dets:
        print("(no agent ecosystems detected)")
        return 0
    print("ECOSYSTEM\tCOLLECTED\tUPLOADED\tSOURCE")
    for det in dets:
        bridge = Path(det["bridge"])
        bridge_ids = (
            {p.stem for p in bridge.glob("traj_*.json")}
            if bridge.is_dir() else set()
        )
        collected = len(bridge_ids)
        uploaded = len(bridge_ids & uploaded_ids)
        print(f"{det['ecosystem']}\t{collected}\t{uploaded}\t{det['source']}")
    return 0


def cmd_connect(args) -> int:
    """team 瘦客户端：连上 server。

    ``xskill connect <host:port> --token <t>``  首次握手 + 落盘连接信息
    ``xskill connect``                          复用已存连接
    ``xskill connect ... --foreground``          前台阻塞跑守护循环

    默认（非 --foreground）：完成握手 / 校验连接信息后，把常驻循环交给操作系统的
    守护设施（Windows「计划任务」；Linux/WSL 优先 systemd user）在
    后台拉起，命令随即返回——用户不必一直开着终端。``--foreground`` 才是真正阻塞的
    轮询循环，也是后台任务实际 execute 的形态（见 team.client.service）。
    """
    from xskill.config import get_team_client_state_path
    from xskill.team.client.state import load_client_state

    state_path = get_team_client_state_path()

    # watchdog 模式：不握手，直接进 supervisor 循环（由它拉 --foreground
    # 子进程并崩溃自愈）。仅供 service 后端内部拉起，不面向用户。
    if getattr(args, "supervise", False):
        if not state_path.is_file():
            print("error: 尚未连接过 server，supervisor 无从拉起。先跑：\n"
                  "  xskill connect <host:port> --token <token>",
                  file=sys.stderr)
            return 2
        from xskill.team.client.supervisor import run_supervisor
        return run_supervisor()

    if args.address:
        state = _connect_handshake(args, state_path)
        if state is None:
            return 1 if args.token else 2
    else:
        try:
            state = load_client_state(state_path)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    from xskill.team.client.service import ServiceError, get_backend
    backend = get_backend()

    # 前台模式，或本平台还没有原生常驻后端：直接阻塞跑守护循环。
    if args.foreground or not backend.supported:
        print(f"reconnecting: client_id={state.client_id}  server={state.server_url}")
        _run_team_client_forever(state, use_proxy=args.use_proxy,
                                 auto_update=not args.no_auto_update)
        return 0

    # 默认（有原生后端的平台，如 Windows）：交给操作系统守护设施后台拉起。
    try:
        st = backend.install_and_start()
    except ServiceError as e:
        print(f"error: 后台常驻启动失败：{e}", file=sys.stderr)
        print("  可先用 `xskill connect --foreground` 在前台验证连接是否正常。",
              file=sys.stderr)
        return 1
    print(f"background task started: {st.get('task_name') or st.get('backend')}"
          f"  (pid={st.get('pid')})")
    print("  用 `xskill status` 查看，`xskill stop` 停止。")
    return 0


def _connect_handshake(args, state_path):
    """带 address 的首次/重连握手：register → 落盘 state。返回 ClientState 或 None。"""
    import socket as _socket
    from xskill.team.client.state import (
        ClientState, load_client_state, save_client_state,
    )
    from xskill.team.client.daemon import register_with_server_full

    if not args.token:
        print("error: 首次 connect 必须带 --token（server 启动时打印的 join token）",
              file=sys.stderr)
        return None
    server_url = args.address
    if not server_url.startswith("http"):
        server_url = f"http://{server_url}"
    # 带参 connect 也尽量保身份不漂移：本地 state 文件若存在就读出已有 client_id，
    # 作为 ``claimed_client_id`` 一起发给 server——server 按 (claimed/fingerprint/
    # new) 三级判定续用。state 不在 → existing_client_id=None，让 server 按指纹回查。
    existing_client_id: str | None = None
    if state_path.is_file():
        try:
            existing_client_id = load_client_state(state_path).client_id
        except Exception:
            # state 文件损坏不阻断重连——按"无本地身份"处理，让 server 走指纹回查
            # 或新发。损坏的 state 接下来会被新的 save 覆盖。
            existing_client_id = None
    import httpx
    # 默认 trust_env=False：team server 是已知、可直连的内网主机，绕开公司代理
    # （SWG）才是正确语义——经代理常因代理出口连不上 server 而 504。--use-proxy 时
    # 恢复读取系统/环境代理（含 Windows 注册表代理）。
    http = httpx.Client(base_url=server_url, timeout=30.0,
                        trust_env=args.use_proxy)
    try:
        reg = register_with_server_full(
            http, token=args.token,
            label=args.label or _socket.gethostname(),
            hostname=_socket.gethostname(),
            existing_client_id=existing_client_id,
            user_name=args.name or None,
        )
        client_id = reg["client_id"]
    except Exception as e:
        print(f"error: 注册失败: {e}", file=sys.stderr)
        return None
    state = ClientState(server_url=server_url, client_id=client_id,
                        join_token=args.token)
    save_client_state(state, state_path)
    name_hint = f"  (--name={args.name})" if args.name else ""
    print(f"connected: client_id={client_id}  server={server_url}{name_hint}")
    # P2-2.2(Q2a):server 为命名用户发放 dashboard 登录 token,这里打印一次。
    # token 幂等(重连拿到同一个),丢了重新 connect 即可再看到。
    dash_token = reg.get("dashboard_token")
    if dash_token:
        print(f"dashboard 登录: 用户名 {args.name} + token {dash_token}"
              f"  (server 看板地址 {server_url}/)")
    return state


def _run_team_client_forever(state, *, use_proxy: bool,
                             auto_update: bool = True) -> None:
    """构造 TeamClient 并阻塞跑守护循环。"""
    import httpx
    from xskill.config import (
        XSKILL_HOME, get_team_client_cursor_path, get_team_client_history_path,
    )
    from xskill.team.client.daemon import TeamClient

    http = httpx.Client(base_url=state.server_url, timeout=30.0,
                        trust_env=use_proxy)
    client = TeamClient(
        state=state, http=http,
        skill_dir=XSKILL_HOME / "skill",
        cursor_path=get_team_client_cursor_path(state.server_url),
        history_path=get_team_client_history_path(state.server_url),
        auto_update=auto_update,
    )
    client.run_forever()   # 阻塞


def _print_connect_status(st: dict, as_json: bool) -> None:
    """渲染 `xskill status` 输出。"""
    import json as _json
    if as_json:
        printable = {k: v for k, v in st.items() if k != "schtasks_query"}
        print(_json.dumps(printable, ensure_ascii=False, indent=2))
        return
    running = st.get("running")
    mark = "● running" if running else ("○ stopped" if st.get("installed")
                                        else "— not installed")
    print(f"connect daemon: {mark}")
    if st.get("task_name"):
        print(f"  task     : {st['task_name']} ({st.get('backend')})")
    elif st.get("unit_name"):
        print(f"  service  : {st['unit_name']} ({st.get('backend')})")
    elif st.get("backend"):
        print(f"  backend  : {st['backend']}")
    if st.get("platform"):
        print(f"  platform : {st['platform']}")
    if st.get("method"):
        print(f"  method   : {st['method']}")
    if st.get("pid"):
        print(f"  pid      : {st['pid']}")
    if st.get("watchdog_pid"):
        print(f"  watchdog : {st['watchdog_pid']}"
              + ("" if st.get("watchdog_alive", True) else "  (dead)"))
    if st.get("crash_recovery"):
        print(f"  自愈     : {st['crash_recovery']}")
    if st.get("boot_autostart"):
        print(f"  开机自启 : {st['boot_autostart']}")
    if st.get("log_path"):
        print(f"  log      : {st['log_path']}")
    if st.get("server_url"):
        print(f"  server   : {st['server_url']}")
    if st.get("client_id"):
        print(f"  client_id: {st['client_id']}")
    for msg in st.get("degraded") or []:
        print(f"  degraded : {msg}")
    if st.get("warning"):
        print(f"  warning  : {st['warning']}")


def cmd_start(args) -> int:
    """安装并启动 connect 常驻任务（未 connect 过则提示先 connect）。"""
    from xskill.config import get_team_client_state_path
    from xskill.team.client.service import ServiceError, get_backend
    quiet = getattr(args, "quiet", False)
    if not get_team_client_state_path().is_file():
        if not quiet:
            print("error: 尚未连接过 server。先跑一次：\n"
                  "  xskill connect <host:port> --token <token>",
                  file=sys.stderr)
        return 2
    backend = get_backend()
    if quiet:
        # 自启触发器（cron @reboot / Windows 计划任务）幂等入口：已在跑
        # 什么都不做；没在跑就装起，但保持静默（触发器无人看输出）。
        try:
            if backend.status().get("running"):
                return 0
            backend.install_and_start()
            return 0
        except ServiceError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    try:
        st = backend.install_and_start()
    except ServiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_connect_status(st, as_json=getattr(args, "json", False))
    return 0


def cmd_update(args) -> int:
    """立即检查 PyPI 是否有新版 xskill，有则升级并重启。"""
    from xskill.team.client.updater import (
        AutoUpdater, _current_version, _latest_pypi_version, _restart,
    )
    current = _current_version("xskill")
    if not current:
        print("error: 无法读取当前版本", file=sys.stderr)
        return 1
    print(f"当前版本: {current}")
    print("正在查询 PyPI...")
    latest = _latest_pypi_version("xskill")
    if not latest:
        print("error: 查询 PyPI 失败，请检查网络", file=sys.stderr)
        return 1
    try:
        from packaging.version import Version
        if Version(latest) <= Version(current):
            print(f"已是最新版本 ({current})")
            return 0
    except Exception:
        pass
    print(f"发现新版本: {latest}，开始升级...")
    # install_and_verify：装完先健康检查，坏版本自动回滚 + 拉黑，
    # 手动 update 与后台自动更新同一套安全网。
    if not AutoUpdater().install_and_verify(latest, current):
        print("error: 升级失败（或新版本健康检查未通过已回滚），"
              "详见日志", file=sys.stderr)
        return 1
    print(f"升级到 {latest} 成功，正在重启...")
    _restart()
    return 0  # 不会到达这里


def cmd_stop(args) -> int:
    """停止并撤销 connect 常驻任务。"""
    from xskill.team.client.service import ServiceError, get_backend
    try:
        st = get_backend().stop()
    except ServiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        _print_connect_status(st, as_json=True)
        return 0
    if st.get("warning"):
        print(f"warning: {st['warning']}", file=sys.stderr)
    print("stopped.")
    return 0


def cmd_status(args) -> int:
    """汇报 connect 常驻任务状态。"""
    from xskill.team.client.service import ServiceError, get_backend
    try:
        st = get_backend().status()
    except ServiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_connect_status(st, as_json=getattr(args, "json", False))
    return 0


def cmd_stats(args) -> int:
    """token/成本统计。直接读 registry(~/.xskill/registry.db)。

    模型分布的 unknown 兜底标签复用 config 的 ``dashboard.default_model``——与看板
    口径一致，让"没记到模型名"的存量轨迹在 stats 里也归到指定模型而非 unknown。
    经 ``dashboard_attribution_defaults`` 读取：只看 dashboard 段、不校验
    llm/embedding key，config.yaml 缺失则退 'unknown'，瘦客户端无 config 也能用。
    纯展示——不改库里真实值、不影响 canary（灰度走 runner 里另一条默认 unknown 的
    路径，与此互不串）。
    """
    import json as _json
    import time
    from xskill.config import dashboard_attribution_defaults
    from xskill.pipeline.registry import model_share, usage_summary
    from xskill.runtime import read_status
    from xskill.usage import render_stats

    unknown_model = dashboard_attribution_defaults()["model"]

    def _emit() -> None:
        s = usage_summary()
        st = read_status()
        ms = model_share(unknown_label=unknown_model)
        if args.json:
            print(_json.dumps({"status": st, "cost": s, "models": ms},
                              ensure_ascii=False, indent=2))
        else:
            print(render_stats(s, status=st, models=ms))

    if args.watch and not args.json:
        try:
            while True:
                print("\033[2J\033[H", end="")  # 清屏 + 光标归位
                _emit()
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
    _emit()
    return 0


def cmd_search(args, xskill) -> int:
    target = args.search_target
    if target == "traj":
        hits = xskill.search_trajectories(args.query, top_k=args.top_k)
        for h in hits:
            traj = h.trajectory
            status = traj.status or "-"
            skill_used = traj.skill_used or "-"
            side = traj.canary_side or "-"
            print(f"{h.similarity:.3f}\t{status}\t{skill_used}\t{side}\t{traj.path}")
        return 0
    if target == "skill":
        hits = xskill.search_skills(args.query, top_k=args.top_k)
        for h in hits:
            s = h.skill
            avg = s.ux_avg(side="main", days=30)
            n = len([x for x in s.recent_ux_scores(side="main", days=30)
                     if x.get("score") is not None])
            ux_col = f"{avg:.1f}({n})" if avg is not None else "-"
            canary = s.canary_status()
            canary_col = "staging" if canary == "staging_active" else "-"
            print(f"{h.similarity:.3f}\t{s.name}\t{s.use_count}\t{ux_col}\t{canary_col}")
        return 0
    return 1


def cmd_read(args, xskill) -> int:
    """`xskill read <PATH> --eco ngagent` —— 批量把 db 文件桥接入库。"""
    from xskill.pipeline.db_ingest import read_db_files
    try:
        summary = read_db_files(
            args.path,
            eco=args.eco,
            register=not args.no_register,
            recursive=args.recursive,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(
        f"read: {len(summary['db_files'])} db 文件 → 桥接 {summary['bridged']} "
        f"条轨迹到 {summary['target_dir']}"
    )
    if not args.no_register:
        print("已注册为 watch_dir —— 启动 `xskill serve` 后将自动拆分入库。")
    return 0


def cmd_rebuild(args, _xskill) -> int:
    """`xskill rebuild [--force]` —— 用现有原始轨迹重跑蒸馏。

    默认：删除已拆 atom + index.pkl、轨迹状态翻回 discovered，让运行中的 watcher
    从头重拆重聚（删 atom 是真正触发重拆的动作——splitter 续接点取自 atom 文件，
    不读 DB offset）。``--force``：额外先清空 skill 仓、看板派生埋点和安装历史。

    换模型护栏：rebuild 的重跑是交给**正在运行的 daemon**，而 daemon 的模型是
    启动时缓存的（改 config 不重启不生效）。若 daemon 在跑且其模型 ≠ 当前 config
    模型 → 默认拒绝并提示先重启 serve，否则会静默用旧模型重生成（`--ignore-
    model-mismatch` 可强行用当前运行的模型重跑）。
    """
    from xskill.config import XSKILL_HOME
    from xskill.pipeline.registry import reset_trajectories
    from xskill.runtime import config_models, read_status

    # ── 换模型护栏（先于任何清仓/重置）──
    status = read_status()
    if status.get("running") and not args.ignore_model_mismatch:
        daemon_model = status.get("llm_model")
        config_model = config_models().get("llm_model")
        if daemon_model != config_model:
            print(
                f"✗ 运行中的 daemon 在用模型 {daemon_model!r}，但 config.yaml "
                f"现在是 {config_model!r}。",
                file=sys.stderr,
            )
            print(
                "  daemon 的模型是启动时缓存的——直接 rebuild 会用旧模型重生成。\n"
                "  换模型请先干净重启：停掉 serve（确认进程真退了）→ 重新 "
                "`xskill serve` → 再 rebuild。\n"
                "  确认就是要用当前运行的模型重跑，可加 --ignore-model-mismatch。",
                file=sys.stderr,
            )
            return 2

    if args.force:
        from xskill.config import get_skill_dir
        from xskill.pipeline.registry import clear_rebuild_derived_state
        from xskill.skill.repo import SkillRepo
        skill_count = SkillRepo(get_skill_dir()).wipe_all_skills()
        print(f"--force: 清空 skill 仓（删 {skill_count} 个 skill）")
        deleted_counts = clear_rebuild_derived_state()
        print(
            "--force: 清空看板派生数据（"
            f"recommendation_log={deleted_counts['recommendation_log']}, "
            f"atom_adoption={deleted_counts['atom_adoption']}, "
            f"canary_decision={deleted_counts['canary_decision']}, "
            f"skill_trigger_eval={deleted_counts['skill_trigger_eval']}）"
        )
        install_history_path = XSKILL_HOME / "install_history.jsonl"
        if install_history_path.is_file():
            install_history_path.unlink()
            print("--force: 删除安装历史 install_history.jsonl")
        else:
            print("--force: 安装历史为空")

    trajectory_count = reset_trajectories(eco=args.eco, traj_id=args.traj)
    print(f"rebuild: 重置 {trajectory_count} 条轨迹（已删 atom + index.pkl，将从头重拆）")

    from xskill.pipeline.cold_start import ColdStartSignal
    cold_start_signal = ColdStartSignal(XSKILL_HOME)
    signal_path = cold_start_signal.create()
    print(
        "cold-start: 已写入内部信号文件，watcher 会在本次 rebuild 处理完成后 flush "
        f"({signal_path})"
    )

    if read_status().get("running"):
        print("watcher 运行中 —— 30s 内将自动重跑这些轨迹。")
    else:
        print("⚠ 未检测到运行中的 daemon —— 请 `xskill serve` 启动后才会重跑。")
    return 0


# ═══════════════════════════════════════════════════════════════
# argparse
# ═══════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xskill",
        description="xskill — distill reusable Skills from AI Agent trajectories",
    )
    # -v / --version 唯一从 xskill.__version__ 读取，而 __version__ 在
    # src/xskill/__init__.py 里只 import 自 setuptools_scm 写出的 _version.py
    # —— 即 git tag 是单一真源，不在任何代码里硬编。
    p.add_argument(
        "-v", "--version",
        action="version",
        version=f"xskill {__version__}",
    )
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--quiet", action="store_true", help="quiet mode")
    sub = p.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Start daemon (FastAPI + watcher)")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument(
        "--home", type=str, default=None,
        help="[debug only] 把生态扫描的 home 指向此目录，只看该目录下的 "
             ".claude/projects/*.jsonl + 装 skill 到 .claude/skills/。"
             "必须同时 --debug。用于隔离调试 (e.g. /tmp/xskill-test-home)。",
    )
    p_serve.add_argument(
        "--server", action="store_true",
        help="team server 模式：收 client 上传轨迹、跑全部 agent、"
             "提供 /api/v1/team/* 同步接口。不加则 standalone（仅本机）。",
    )
    p_serve.add_argument(
        "--force", action="store_true",
        help="已有 daemon 在跑时强行接管（默认拒绝启动，防双 daemon 抢 registry）",
    )

    p_reg = sub.add_parser("registry", help="Manage watched directories")
    p_reg.add_argument("registry_action", choices=["add", "remove", "list"])
    p_reg.add_argument("path", nargs="?", type=str,
                       help="directory path (for add/remove)")
    p_reg.add_argument("--label", type=str, default="",
                       help="human-friendly label (for add)")

    p_search = sub.add_parser(
        "search", help="Search trajectories or skills (cross-registry)"
    )
    p_search.add_argument("search_target", choices=["traj", "skill"])
    p_search.add_argument("query", type=str)
    p_search.add_argument("--top-k", "-k", type=int, default=5)

    p_conn = sub.add_parser(
        "connect", help="Join a team server as a thin client",
    )
    p_conn.add_argument(
        "address", nargs="?", default=None,
        help="server 地址 host:port。省略则复用已存连接（~/.xskill/team_client.json）。",
    )
    p_conn.add_argument("--token", default=None,
                        help="join token（server 启动 `xskill serve --server` 时打印）")
    p_conn.add_argument("--label", default="",
                        help="本 client 的可读标签（默认主机名）")
    p_conn.add_argument(
        "--use-proxy", action="store_true",
        help="经系统/环境代理连 server（默认直连，绕开公司 SWG 代理）。"
             "仅当本机唯一出网路径是代理、且代理能到 server 时才需要。",
    )
    p_conn.add_argument(
        "--name", default=None, metavar="EMPLOYEE_ID",
        help="工号 / 用户 ID（推荐必填）。server 用它派生确定性 client_id——"
             "同一工号在不同设备或重装后身份保持一致，推荐算法也能跨设备积累。"
             "server 若设置了 allow_anonymous: false，则不带 --name 会被拒绝（403）。",
    )
    p_conn.add_argument(
        "--foreground", action="store_true",
        help="前台阻塞运行守护循环（默认交给操作系统守护设施后台常驻）。"
             "常驻任务内部 execute 的就是这个形态；调试时也可手动用。",
    )
    p_conn.add_argument(
        "--no-auto-update", action="store_true", dest="no_auto_update",
        help="禁用自动更新检查（默认每小时查一次 PyPI，有新版则升级重启）。",
    )
    p_conn.add_argument(
        # 内部形态：supervisor watchdog 主体（team.client.supervisor）。由
        # service 后端在无 systemd 平台上 detach 拉起，用户无需手动使用。
        "--supervise", action="store_true", help=argparse.SUPPRESS,
    )

    p_start = sub.add_parser(
        "start", help="把 connect 装成后台常驻（开机自启 + 崩溃自愈）",
    )
    p_start.add_argument("--json", action="store_true", help="机读 JSON 输出")
    p_start.add_argument(
        "--quiet", action="store_true",
        help="已在运行则静默退出 0；供开机自启触发器（cron @reboot / Windows"
             " 计划任务）幂等调用。",
    )

    p_stop = sub.add_parser("stop", help="停止并撤销 connect 常驻任务")
    p_stop.add_argument("--json", action="store_true", help="机读 JSON 输出")

    p_status = sub.add_parser("status", help="查看 connect 常驻任务状态")

    sub.add_parser("update", help="立即检查 PyPI 新版并升级（有新版则重启）")
    p_status.add_argument("--json", action="store_true", help="机读 JSON 输出")

    p_stats = sub.add_parser(
        "stats", help="Show token usage & estimated cost (Issue #43)",
    )
    p_stats.add_argument("--json", action="store_true", help="机读 JSON 输出")
    p_stats.add_argument("--watch", action="store_true",
                         help="htop 式整屏刷新（每 2s）")

    p_read = sub.add_parser(
        "read", help="批量从指定位置读取 db 文件并入库（ngagent/opencode）",
    )
    p_read.add_argument("path", type=str,
                        help="db 文件，或包含 db 文件的目录")
    p_read.add_argument("--eco", default="ngagent",
                        choices=sorted(SQLITE_SPEC_BY_ECO),
                        help="db 所属生态（默认 ngagent）")
    p_read.add_argument("--recursive", "-r", action="store_true",
                        help="目录模式下递归查找 *.db")
    p_read.add_argument("--no-register", action="store_true",
                        help="只桥接不注册 watch_dir（一般不用）")

    p_rebuild = sub.add_parser(
        "rebuild", help="用现有原始轨迹重跑蒸馏（换强模型重生成 skill）",
    )
    p_rebuild.add_argument(
        "--force", action="store_true",
        help="先清空 skill 仓 + 已拆原子再全量重跑（删除重建）",
    )
    p_rebuild.add_argument("--eco", default=None,
                           help="只重跑某生态的轨迹（默认全部）")
    p_rebuild.add_argument("--traj", default=None,
                           help="只重跑某条轨迹 id（调试用）")
    p_rebuild.add_argument(
        "--ignore-model-mismatch", action="store_true",
        help="跳过'daemon 模型≠config 模型'护栏，用当前运行的模型重跑",
    )

    return p


def _setup_logging(debug: bool, quiet: bool, *, command: str = "") -> None:
    """配置 logging。

    - ``serve``：用 ``log_setup.configure_logging`` 拆 component 到独立文件
      （~/.xskill/logs/xskill.<component>.log）+ stdout 简略输出，方便
      tail -f 单独跟某条流水。
    - 其他短命令（``search`` / ``registry``）：保留旧 basicConfig，stdout
      only，不创建文件 handler——这些命令几秒就退，没必要落日志。
    """
    if command in ("serve", "connect"):
        # serve / connect 都是长跑守护，用 file-split 模式落文件日志
        from xskill.config import get_logs_dir
        from xskill.utils.logging import configure_logging
        configure_logging(get_logs_dir(), debug=debug, quiet=quiet, stdout=True)
        return

    # 老 basicConfig 路径（短命令）
    if debug:
        level, fmt = logging.DEBUG, "%(asctime)s [%(name)s] %(levelname)s %(message)s"
    elif quiet:
        level, fmt = logging.WARNING, "%(message)s"
    else:
        level, fmt = logging.INFO, "%(asctime)s [%(name)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "openai", "xskill.utils.llm", "agno"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ═══════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    # Windows 两个输出流暗坑，都会把进程直接带崩，必须在最入口兜底：
    # 1. pythonw（schtasks/启动文件夹常驻用它免弹窗）下 sys.stdout/stderr
    #    是 None——任何 print 即 AttributeError，`connect --foreground`
    #    开头那句 "reconnecting: ..." 就足以让常驻进程秒崩、pid 永远拿
    #    不到（Windows e2e 实测抓出）。补 devnull 流。
    # 2. 控制台编码常是 cp1252 等单字节页，中文输出 UnicodeEncodeError，
    #    且炸点可能在 schtasks 任务已装好之后——用户看到 traceback +
    #    退出码 1，实际却成功了。统一重配 UTF-8（errors=replace）。
    if sys.platform == "win32":
        import os as _os
        if sys.stdout is None:
            sys.stdout = open(_os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(_os.devnull, "w", encoding="utf-8")
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass

    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    set_overrides(debug=args.debug, quiet=args.quiet)
    _setup_logging(args.debug, args.quiet, command=args.command)

    if args.command == "registry" and args.registry_action in ("add", "remove"):
        if not args.path:
            parser.error(f"path is required for 'registry {args.registry_action}'")

    # connect 是瘦客户端：不读 config.yaml / 不需要 llm.api_key / 不构造 XSkill 门面
    if args.command == "connect":
        return cmd_connect(args)

    # start/stop/status 管理 connect 常驻任务——同样是瘦客户端侧，不碰 config.yaml。
    if args.command == "start":
        return cmd_start(args)
    if args.command == "stop":
        return cmd_stop(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "update":
        return cmd_update(args)

    # stats 只读 registry，不需要 config.yaml / llm.api_key / facade
    if args.command == "stats":
        return cmd_stats(args)

    # read / rebuild 只动 registry + 文件，不需要 llm.api_key / facade——
    # 重跑由运行中的 watcher 完成，本命令只做"重置/桥接"。
    if args.command == "read":
        return cmd_read(args, None)
    if args.command == "rebuild":
        return cmd_rebuild(args, None)

    # team 客户端的 `registry list`：本机是 client（有 team_client.json）且没有
    # standalone 数据（watch_dirs 为空）时，改走现算视图。放在 config/facade
    # 之前——纯客户端没 config.yaml 也能直接看。standalone/server 机（watch_dirs
    # 非空）走原路，不受影响（哪怕本机也存了 team_client.json）。
    if args.command == "registry" and args.registry_action == "list":
        from xskill.config import get_team_client_state_path
        if (get_team_client_state_path().is_file()
                and _standalone_watch_dir_count() == 0):
            return cmd_registry_list_client()

    # 首次运行 auto-init：serve / registry / search 都需要 config.yaml。
    # 不存在就写一份模板并要求用户填 key 后重跑——比直接抛 traceback 友好。
    from xskill.config import CONFIG_PATH, ensure_config_exists
    if not ensure_config_exists():
        print(
            f"\n  Created a config template at {CONFIG_PATH}\n"
            f"  Edit it — fill in llm.api_key and embedding.api_key — "
            f"then run `xskill {args.command}` again.\n",
            file=sys.stderr,
        )
        return 0

    from xskill import XSkill
    xskill = XSkill()

    handler = {
        "serve":    cmd_serve,
        "registry": cmd_registry,
        "search":   cmd_search,
    }.get(args.command)
    return handler(args, xskill) if handler else (parser.print_help() or 1)


if __name__ == "__main__":
    sys.exit(main() or 0)
