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
    from xskill.runtime import write_running
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
    的存储），它靠实时 ``detect_known_ecosystems`` 采集 + ``team_client_cursor``
    记上传进度。所以这里**现算**视图：每个探测到的生态显示

        ECOSYSTEM  COLLECTED  UPLOADED  SOURCE

    - COLLECTED = 该生态 bridge 目录下 ``traj_*.json`` 数（已镜像采集的轨迹）
    - UPLOADED  = 上述轨迹里已记入 cursor（已上传 server）的数
    - SOURCE    = 用户真实的原生目录（如 ~/.claude/projects），非内部 bridge

    不依赖 config.yaml / XSkill 门面——纯客户端机器也能直接看。
    """
    import json
    from pathlib import Path
    from xskill.config import (
        XSKILL_HOME, get_team_client_state_path, get_team_client_cursor_path,
    )
    from xskill.ecosystems import detect_known_ecosystems
    from xskill.team.client.state import load_client_state

    home = XSKILL_HOME.parent  # 与 XSKILL_HOME 同源,避免 home 解析漂移
    # 游标按 server 分目录（方案 A）——先读连接状态拿 server_url 才能定位。
    # 没连过 server（无 state）则没有任何上传游标，uploaded 全 0。
    uploaded_ids: set[str] = set()
    state_path = get_team_client_state_path()
    if state_path.is_file():
        cursor_path = get_team_client_cursor_path(
            load_client_state(state_path).server_url)
        if cursor_path.is_file():
            uploaded_ids = set(json.loads(cursor_path.read_text(encoding="utf-8")))

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
    """team 瘦客户端：连上 server，跑采集/同步/对齐守护循环。

    ``xskill connect <host:port> --token <t>``  首次握手 + 落盘连接信息
    ``xskill connect``                          复用已存连接
    """
    import socket as _socket
    from xskill.config import (
        get_team_client_state_path, XSKILL_HOME,
        get_team_client_cursor_path, get_team_client_history_path,
    )
    from xskill.team.client.state import (
        ClientState, load_client_state, save_client_state,
    )
    from xskill.team.client.daemon import TeamClient, register_with_server

    state_path = get_team_client_state_path()

    if args.address:
        if not args.token:
            print("error: 首次 connect 必须带 --token（server 启动时打印的 join token）",
                  file=sys.stderr)
            return 2
        server_url = args.address
        if not server_url.startswith("http"):
            server_url = f"http://{server_url}"
        # 带参 connect 也尽量保身份不漂移：本地 state 文件若存在就读出
        # 已有 client_id，作为 ``claimed_client_id`` 一起发给 server——
        # server 按 (claimed/fingerprint/new) 三级判定续用。state 不在 →
        # existing_client_id=None，让 server 按指纹回查或新发。
        existing_client_id: str | None = None
        if state_path.is_file():
            try:
                existing_client_id = load_client_state(state_path).client_id
            except Exception:
                # state 文件损坏不阻断重连——按"无本地身份"处理，让 server
                # 走指纹回查或新发。损坏的 state 接下来会被新的 save 覆盖。
                existing_client_id = None
        import httpx
        # 默认 trust_env=False：team server 是已知、可直连的内网主机，绕开公司
        # 代理（SWG）才是正确语义——经代理常因代理出口连不上 server 而 504。
        # --use-proxy 时恢复读取系统/环境代理（含 Windows 注册表代理）。
        http = httpx.Client(base_url=server_url, timeout=30.0,
                            trust_env=args.use_proxy)
        try:
            client_id = register_with_server(
                http, token=args.token,
                label=args.label or _socket.gethostname(),
                hostname=_socket.gethostname(),
                existing_client_id=existing_client_id,
            )
        except Exception as e:
            print(f"error: 注册失败: {e}", file=sys.stderr)
            return 1
        state = ClientState(server_url=server_url, client_id=client_id,
                            join_token=args.token)
        save_client_state(state, state_path)
        print(f"connected: client_id={client_id}  server={server_url}")
    else:
        try:
            state = load_client_state(state_path)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        import httpx
        # 同上：复用连接的后台同步也走直连，否则"注册过了同步全 504"。
        http = httpx.Client(base_url=state.server_url, timeout=30.0,
                            trust_env=args.use_proxy)
        print(f"reconnecting: client_id={state.client_id}  server={state.server_url}")

    # skill working copies 复用标准 skill_dir（~/.xskill/skill/）——瘦客户端
    # 没有 config.yaml，直接用默认路径，不走 get_skill_dir()（那会 load_config）。
    # 游标 / 去抖 / 安装历史按 server 分目录（方案 A）——换 server 不再被上一个
    # server 的"已上传"游标静默压制对新 server 的上传。skill 工作副本仍复用共享
    # 的 skill_dir（cleanup 已按 manifest 摘除旧 server 的残留 skill）。
    client = TeamClient(
        state=state, http=http,
        skill_dir=XSKILL_HOME / "skill",
        cursor_path=get_team_client_cursor_path(state.server_url),
        history_path=get_team_client_history_path(state.server_url),
    )
    client.run_forever()   # 阻塞
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


def _find_skill_dir_for_conflict(xskill, skill_name: str):
    sd = xskill.skill_repo.root / skill_name
    if not sd.is_dir():
        print(f"error: skill not found: {skill_name}", file=sys.stderr)
        return None
    return sd


def _all_skill_dirs(xskill):
    root = xskill.skill_repo.root
    if not root.is_dir():
        return []
    return [p for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]


def cmd_conflict(args, xskill) -> int:
    from xskill.skill import candidates as C

    action = args.conflict_action
    if action == "list":
        sd = _find_skill_dir_for_conflict(xskill, args.skill_name)
        if sd is None:
            return 1
        data = C.load_conflicts(sd)
        conflicts = data.get("conflicts", []) or []
        if not conflicts:
            print(f"(no conflicts for {args.skill_name})")
            return 0
        print("ID\tTYPE\tSTATUS\tSUMMARY")
        for item in conflicts:
            status = "resolved" if item.get("resolution") else "unresolved"
            print(
                f"{item.get('id')}\t{item.get('type')}\t{status}\t"
                f"{item.get('conflict_summary', '')}"
            )
        return 0

    if action == "show":
        import yaml
        for sd in _all_skill_dirs(xskill):
            data = C.load_conflicts(sd)
            for item in data.get("conflicts", []) or []:
                if item.get("id") == args.conflict_id:
                    print(f"skill: {sd.name}")
                    print(yaml.safe_dump(item, allow_unicode=True, sort_keys=False))
                    return 0
        print(f"error: conflict not found: {args.conflict_id}", file=sys.stderr)
        return 1

    if action == "resolve":
        sd = _find_skill_dir_for_conflict(xskill, args.skill_name)
        if sd is None:
            return 1
        data = C.load_conflicts(sd)
        conflicts = data.get("conflicts", []) or []
        unresolved = [
            item for item in conflicts
            if item.get("type") == "hard" and not item.get("resolution")
        ]
        if not unresolved:
            print(f"(no unresolved hard conflicts for {args.skill_name})")
            return 0
        changed = False
        for item in unresolved:
            atoms = item.get("atoms", []) or []
            print("")
            print(f"Skill {args.skill_name} conflict {item.get('id')}:")
            print(f"  {item.get('conflict_summary', '')}")
            for idx, atom in enumerate(atoms):
                label = chr(ord("A") + idx)
                print(
                    f"  [{label}] {atom.get('position', '')} "
                    f"({atom.get('weightscore', 0)}分, "
                    f"{atom.get('supporting_trajs', 1)}条轨迹支持)"
                )
            choice = input("选择: [A/B...] winner / [M] 合并为条件分支 / [S] 跳过: ")
            choice = choice.strip().upper()
            if choice == "S" or not choice:
                continue
            from datetime import datetime
            resolved_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            if choice == "M":
                merged = input("合并内容: ").strip()
                item["resolution"] = {
                    "strategy": "conditional_merge",
                    "merged_content": merged,
                    "resolved_by": "user",
                    "resolved_at": resolved_at,
                }
                changed = True
                continue
            index = ord(choice[0]) - ord("A")
            if index < 0 or index >= len(atoms):
                print("  invalid choice; skipped")
                continue
            item["resolution"] = {
                "strategy": "manual",
                "winner": atoms[index].get("atom_id"),
                "resolved_by": "user",
                "resolved_at": resolved_at,
            }
            changed = True
        if changed:
            C.save_conflicts(sd, {"conflicts": conflicts})
            print("resolved")
        return 0

    return 1


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

    p_stats = sub.add_parser(
        "stats", help="Show token usage & estimated cost (Issue #43)",
    )
    p_stats.add_argument("--json", action="store_true", help="机读 JSON 输出")
    p_stats.add_argument("--watch", action="store_true",
                         help="htop 式整屏刷新（每 2s）")

    p_conflict = sub.add_parser(
        "conflict", help="Inspect and resolve skill candidate conflicts",
    )
    p_conflict.add_argument("conflict_action", choices=["list", "show", "resolve"])
    p_conflict.add_argument("skill_name", nargs="?",
                            help="skill name for list/resolve")
    p_conflict.add_argument("conflict_id", nargs="?",
                            help="conflict id for show")

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
    if args.command == "conflict":
        if args.conflict_action in ("list", "resolve") and not args.skill_name:
            parser.error(f"skill_name is required for 'conflict {args.conflict_action}'")
        if args.conflict_action == "show" and not (args.conflict_id or args.skill_name):
            parser.error("conflict_id is required for 'conflict show'")
        if args.conflict_action == "show" and not args.conflict_id:
            args.conflict_id = args.skill_name

    # connect 是瘦客户端：不读 config.yaml / 不需要 llm.api_key / 不构造 XSkill 门面
    if args.command == "connect":
        return cmd_connect(args)

    # stats 只读 registry，不需要 config.yaml / llm.api_key / facade
    if args.command == "stats":
        return cmd_stats(args)

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
        "conflict": cmd_conflict,
    }.get(args.command)
    return handler(args, xskill) if handler else (parser.print_help() or 1)


if __name__ == "__main__":
    sys.exit(main() or 0)
