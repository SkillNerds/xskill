#!/usr/bin/env python3.11
"""存量回填:把 C/S server 上历史轨迹的 user agent 模型补齐(Issue #43 关联)。

背景:server 端历史轨迹(team upload 落的)只有 ``traj_*.md``,没有 model——
因为上传协议早先不带 model。增量已由 "上传带 model" 修复;本脚本只管**存量**。

关键约束(用户明确要求):**绝不触发全量 DB 重建**。
- discover 对"已在 registry 里的行"只更 mtime、**不重读 sidecar、不重处理**,
  所以光写 ``.json`` 对存量无效 → 必须**直接 UPDATE source_model 一列**。
- 写 ``.json`` + UPDATE 一列,都不碰 status/has_meta/has_embedding/.md mtime,
  pipeline 重处理是靠这些触发的 → **零重建**,纯外科手术。
- 写 ``.json`` 的意义:给未来万一的全量 reindex 留文件源(source-of-truth)。

两段式(因为 model 在 host 的本机 sidecar,数据在容器里):
  1. ``export``  (host 跑):扫 ~/.xskill/*_sessions/traj_*.json 建 traj_id→model
                 映射,导出小 JSON。
  2. ``apply``   (容器内跑):读映射,遍历 team_trajectories/clients/*/sessions/
                 traj_*.md,命中的写 {stem}.json + 直接 UPDATE registry。幂等。

用法:
  # host:
  python3.11 scripts/backfill_server_source_model.py export --out /tmp/model_map.json
  docker cp /tmp/model_map.json xskill-server:/tmp/model_map.json
  docker cp scripts/backfill_server_source_model.py xskill-server:/tmp/bf.py
  # 容器内(先 dry-run 看命中,再正式跑):
  docker exec xskill-server python3.11 /tmp/bf.py apply --map /tmp/model_map.json --dry-run
  docker exec xskill-server python3.11 /tmp/bf.py apply --map /tmp/model_map.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

LIVE_DIRS = ["cc_sessions", "codex_sessions", "opencode_sessions", "ngagent_sessions"]


def cmd_export(args: argparse.Namespace) -> int:
    """host 侧:本机 sidecar → {traj_id: model} 映射。"""
    xs = Path(args.xskill_home).expanduser()
    mapping: dict[str, str] = {}
    for eco in LIVE_DIRS:
        d = xs / eco
        if not d.is_dir():
            continue
        for jf in sorted(d.glob("traj_*.json")):
            try:
                model = json.loads(jf.read_text(encoding="utf-8")).get("model")
            except (OSError, json.JSONDecodeError):
                continue
            if model:
                mapping[jf.stem] = str(model)
    out = Path(args.out)
    out.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[export] {len(mapping)} traj_id→model 写入 {out}")
    return 0


def _ensure_column(reg: sqlite3.Connection) -> None:
    """幂等保证 source_model 列存在(新代码启动时已迁移,这里兜底)。"""
    try:
        reg.execute("ALTER TABLE trajectories ADD COLUMN source_model TEXT")
    except sqlite3.OperationalError:
        pass   # 列已存在


def cmd_apply(args: argparse.Namespace) -> int:
    """容器侧:写 json sidecar + 直接 UPDATE registry,零重建。"""
    mapping: dict[str, str] = json.loads(Path(args.map).read_text(encoding="utf-8"))
    traj_root = Path(args.traj_root)
    sessions = sorted(traj_root.glob("clients/*/sessions"))
    reg_path = Path(args.registry)
    reg = sqlite3.connect(reg_path) if reg_path.is_file() else None
    if reg is not None and not args.dry_run:
        _ensure_column(reg)

    hit = miss = wrote_json = updated = 0
    for sess in sessions:
        for md in sorted(sess.glob("traj_*.md")):
            model = mapping.get(md.stem)
            if not model:
                miss += 1
                continue
            hit += 1
            jp = md.with_suffix(".json")
            already = jp.is_file() and _json_model(jp) == model
            if not args.dry_run and not already:
                jp.write_text(json.dumps({"model": model}, ensure_ascii=False),
                              encoding="utf-8")
            if not already:
                wrote_json += 1
            if reg is not None and not args.dry_run:
                cur = reg.execute(
                    "UPDATE trajectories SET source_model=? WHERE filename=?",
                    (model, md.name))
                updated += cur.rowcount
    if reg is not None and not args.dry_run:
        reg.commit()
        reg.close()

    tag = "[apply DRY-RUN]" if args.dry_run else "[apply]"
    print(f"{tag} 命中 model 的 md={hit}  未命中(保持 unknown)={miss}")
    if not args.dry_run:
        print(f"{tag} 写/更新 json sidecar={wrote_json}  registry 行 UPDATE={updated}")
    print(f"{tag} 注:未触发任何 reindex / 重处理(只动 source_model 列 + sidecar)")
    return 0


def _json_model(jp: Path) -> str:
    try:
        return str(json.loads(jp.read_text(encoding="utf-8")).get("model") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="host:建 traj_id→model 映射")
    e.add_argument("--xskill-home", default="~/.xskill")
    e.add_argument("--out", default="/tmp/model_map.json")
    e.set_defaults(func=cmd_export)

    a = sub.add_parser("apply", help="容器:写 sidecar + UPDATE registry(零重建)")
    a.add_argument("--map", required=True)
    a.add_argument("--traj-root", default="/root/.xskill/team_trajectories")
    a.add_argument("--registry", default="/root/.xskill/registry.db")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
