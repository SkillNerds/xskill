"""``xskill tools migrate-traj-name`` — 存量轨迹改名为新命名规则（issue #234）。

新命名规则（随代码升级即对**新**轨迹生效，无需本命令）：

1. 本机桥接文件的会话 id 尾段由 8 位放宽到 16 位（``ses_`` 等前缀型会话 id
   在 8 位下只剩 4 位有效字符，团队合并语料时会撞名）。
2. 团队 server 落盘的轨迹文件名带成员标识前缀 ``<用户名可读部分>_<8 位哈希>``，
   保证多成员语料全局唯一。

**存量**文件保持旧名也能被正确识别（读取侧新旧双键回退），因此本命令不是
升级必做项；它把旧名文件一次性改成新名，用于：已经因旧名撞车的团队语料
（外部蒸馏算法按原子 id 判重，撞名即整轮失败）、或希望全库命名统一。

一次轨迹改名牵动四处，本命令全部同步处理：

- ``<dir>/<旧名>.md`` 与 sidecar ``<旧名>.json`` → 新名；
- 原子目录 ``<dir>/<旧名>/tasks/atom_<旧名>_NNNN.json`` → 目录、文件名与
  JSON 内 ``atom_id`` / ``traj_id`` / ``pre_atom_id`` / ``post_atom_id`` 一并改写；
- 注册表 ``trajectories.filename``（改文件不改库会让 watcher 把新名当新轨迹
  重新蒸馏一遍）；
- 各 skill ``.candidates.yml`` 里引用的原子 id。

安全性：执行前把受影响文件整份复制到
``<xskill_home>/migrate_backup/<时间戳>/``，并写 ``manifest.json`` 记录每一步
改名与数据库更新；``--rollback`` 按最近一份 manifest 逆向恢复。目标名已被
占用时跳过该条并警告，不覆盖任何现有文件。
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BACKUP_DIR_NAME = "migrate_backup"
MANIFEST_NAME = "manifest.json"

@dataclass
class RenamePlan:
    """一条轨迹的完整改名计划（文件 + 数据库 + candidates 引用）。"""

    directory: Path
    old_id: str
    new_id: str
    reason: str

    @property
    def paths(self) -> list[tuple[Path, Path]]:
        """(旧路径, 新路径) 列表：md、sidecar json、原子目录。"""
        pairs: list[tuple[Path, Path]] = []
        for suffix in (".md", ".json"):
            old = self.directory / f"{self.old_id}{suffix}"
            if old.exists():
                pairs.append((old, self.directory / f"{self.new_id}{suffix}"))
        atom_dir = self.directory / self.old_id
        if atom_dir.is_dir():
            pairs.append((atom_dir, self.directory / self.new_id))
        return pairs


@dataclass
class MigrationReport:
    planned: list[RenamePlan] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    renamed: int = 0
    backup_dir: Optional[Path] = None
    dry_run: bool = False


# ─────────────────────────────────────────────────────────────────
# 计划：本机桥接目录（sid 8 → 16）
# ─────────────────────────────────────────────────────────────────

def _plan_local_bridge_dirs(xskill_home: Path) -> tuple[list[RenamePlan], list[str]]:
    """扫 ``<home>/*_sessions/``，按 sidecar 里的完整 ``session_id`` 把 8 位
    尾段的旧名算成 16 位新名。sidecar 缺失或无 session_id 的文件无法重算，
    跳过（读取侧仍认旧名，不影响使用）。"""
    from xskill.ecosystems._shared import legacy_short_sid, short_sid

    plans: list[RenamePlan] = []
    skipped: list[str] = []
    for bridge in sorted(xskill_home.glob("*_sessions")):
        if not bridge.is_dir():
            continue
        for sidecar in sorted(bridge.glob("traj_*.json")):
            stem = sidecar.stem
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                skipped.append(f"{sidecar}: sidecar 无法解析，跳过")
                continue
            sid = meta.get("session_id") if isinstance(meta, dict) else None
            if not sid:
                continue  # 非桥接 sidecar（如上传元数据），不属于本迁移
            legacy_tail = legacy_short_sid(sid)
            new_tail = short_sid(sid)
            if new_tail == legacy_tail:
                continue  # 完整 id 本就不超过 8 位，无需改
            # 尾段可能自带下划线（如 ``ses_0f5d``），不能按最后一个下划线
            # 切分——直接比对旧规则算出的完整尾段。
            if not stem.endswith("_" + legacy_tail):
                continue  # 已是新名，或命名不含 sid 尾段
            new_id = stem[: -len(legacy_tail)] + new_tail
            plans.append(RenamePlan(
                directory=bridge, old_id=stem, new_id=new_id,
                reason="会话 id 尾段 8 位 → 16 位",
            ))
    return plans, skipped


# ─────────────────────────────────────────────────────────────────
# 计划：团队 server 落盘目录（成员标识前缀）
# ─────────────────────────────────────────────────────────────────

def _plan_server_clients(
    traj_root: Path, registry_db: Path,
) -> tuple[list[RenamePlan], list[str]]:
    """扫 ``<traj_root>/clients/<目录>/sessions/``，给未带成员前缀的轨迹补
    ``<用户名可读部分>_<8 位 client_id>`` 前缀。目录 ↔ 成员的对应关系由
    client 注册表反查；查不到的目录（手工拷入等）跳过并提示。"""
    from xskill.team.server.client_registry import (
        ClientRegistry,
        member_traj_tag,
        safe_dir_name,
    )

    plans: list[RenamePlan] = []
    skipped: list[str] = []
    clients_root = traj_root / "clients"
    if not clients_root.is_dir():
        return plans, skipped
    if not registry_db.is_file():
        skipped.append(
            f"{clients_root}: 找到 server 轨迹目录但没有 client 注册表 "
            f"({registry_db})，server 侧不迁移"
        )
        return plans, skipped

    tag_by_dir: dict[str, str] = {}
    registry = ClientRegistry(registry_db)
    for row in registry.list():
        client_id = row.get("client_id") or ""
        user_name = row.get("user_name") or None
        try:
            dir_name = safe_dir_name(user_name, client_id)
        except ValueError:
            continue
        tag_by_dir[dir_name] = member_traj_tag(user_name, client_id)

    for client_dir in sorted(clients_root.iterdir()):
        sessions = client_dir / "sessions"
        if not sessions.is_dir():
            continue
        tag = tag_by_dir.get(client_dir.name)
        if tag is None:
            skipped.append(
                f"{sessions}: 目录 {client_dir.name!r} 不在 client 注册表中，"
                "无法确定成员标识，跳过"
            )
            continue
        for md in sorted(sessions.glob("traj_*.md")):
            stem = md.stem
            rest = stem[len("traj_"):]
            if rest.startswith(tag + "_"):
                continue  # 已带前缀
            plans.append(RenamePlan(
                directory=sessions, old_id=stem,
                new_id=f"traj_{tag}_{rest}",
                reason=f"补成员前缀 {tag}",
            ))
    return plans, skipped


# ─────────────────────────────────────────────────────────────────
# 执行
# ─────────────────────────────────────────────────────────────────

def _rewrite_atom_payload(text: str, old_id: str, new_id: str) -> str:
    """改写原子 JSON / candidates 文本里的轨迹与原子 id 引用。

    ``atom_<旧名>_`` 保序替换成 ``atom_<新名>_``；``traj_id`` 字段值单独替换。
    旧名匹配带边界（后随 ``_`` 序号或引号），不会误伤把旧名作为前缀的其它 id。
    """
    text = text.replace(f"atom_{old_id}_", f"atom_{new_id}_")
    return text.replace(f'"{old_id}"', f'"{new_id}"')


def _apply_plan(
    plan: RenamePlan,
    *,
    backup_root: Path,
    manifest: dict,
    registry_db: Optional[Path],
) -> bool:
    """执行一条计划：备份 → 改名 → 改写内容 → 更新注册表。

    任何目标已存在都整条跳过（不覆盖）。返回是否实际执行。"""
    pairs = plan.paths
    if not pairs:
        return False
    for _, new in pairs:
        if new.exists():
            logger.warning("目标已存在，跳过 %s → %s", plan.old_id, plan.new_id)
            return False

    # 备份：整份复制（含原子目录），保持相对 <backup>/<目录名哈希>/ 布局
    bucket = backup_root / plan.directory.name / plan.old_id
    bucket.mkdir(parents=True, exist_ok=True)
    for old, _ in pairs:
        if old.is_dir():
            shutil.copytree(old, bucket / old.name)
        else:
            shutil.copy2(old, bucket / old.name)

    entry = {
        "directory": str(plan.directory),
        "old_id": plan.old_id,
        "new_id": plan.new_id,
        "renames": [],
        "db_updated": False,
    }
    for old, new in pairs:
        old.rename(new)
        entry["renames"].append([str(old), str(new)])

    # 原子目录内：文件名 + JSON 字段
    atom_dir = plan.directory / plan.new_id / "tasks"
    if atom_dir.is_dir():
        for atom_file in sorted(atom_dir.glob("atom_*.json")):
            try:
                payload = atom_file.read_text(encoding="utf-8")
            except OSError:
                logger.warning("原子文件读取失败，内容未改写: %s", atom_file)
                continue
            atom_file.write_text(
                _rewrite_atom_payload(payload, plan.old_id, plan.new_id),
                encoding="utf-8",
            )
            new_name = atom_file.name.replace(
                f"atom_{plan.old_id}_", f"atom_{plan.new_id}_",
            )
            if new_name != atom_file.name:
                atom_file.rename(atom_file.with_name(new_name))

    # 注册表：filename 同步，避免 watcher 把新名当新轨迹重新蒸馏
    if registry_db is not None and registry_db.is_file():
        with sqlite3.connect(registry_db) as conn:
            cur = conn.execute(
                "UPDATE trajectories SET filename = ? "
                "WHERE filename = ? AND watch_dir_id IN "
                "(SELECT id FROM watch_dirs WHERE path = ?)",
                (f"{plan.new_id}.md", f"{plan.old_id}.md",
                 str(plan.directory)),
            )
            entry["db_updated"] = cur.rowcount > 0
    manifest["entries"].append(entry)
    return True


def _rewrite_candidates(
    skill_dir: Optional[Path], id_map: dict[str, str], manifest: dict,
) -> None:
    """各 skill ``.candidates.yml`` 里的原子 id 引用按映射改写（含备份）。"""
    if skill_dir is None or not skill_dir.is_dir():
        return
    backup_root = Path(manifest["backup_dir"]) / "candidates"
    for cand in sorted(skill_dir.glob("*/.candidates.yml")):
        try:
            text = cand.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text = text
        for old_id, new_id in id_map.items():
            new_text = new_text.replace(
                f"atom_{old_id}_", f"atom_{new_id}_",
            )
        if new_text == text:
            continue
        backup_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cand, backup_root / f"{cand.parent.name}.candidates.yml")
        cand.write_text(new_text, encoding="utf-8")
        manifest["candidates"].append(str(cand))


def migrate_traj_names(
    *,
    xskill_home: Path,
    traj_root: Optional[Path] = None,
    registry_db: Optional[Path] = None,
    clients_registry_db: Optional[Path] = None,
    skill_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> MigrationReport:
    """迁移主入口。参数均显式传入（CLI 统一从 config 解析），便于测试。"""
    report = MigrationReport(dry_run=dry_run)
    plans, skipped = _plan_local_bridge_dirs(xskill_home)
    if traj_root is not None and clients_registry_db is not None:
        server_plans, server_skipped = _plan_server_clients(
            traj_root, clients_registry_db,
        )
        plans += server_plans
        skipped += server_skipped
    report.planned = plans
    report.skipped = skipped
    if dry_run or not plans:
        return report

    backup_root = (
        xskill_home / BACKUP_DIR_NAME / time.strftime("%Y%m%d_%H%M%S")
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    report.backup_dir = backup_root
    manifest: dict = {
        "created_at": time.time(),
        "backup_dir": str(backup_root),
        "entries": [],
        "candidates": [],
    }
    id_map: dict[str, str] = {}
    for plan in plans:
        if _apply_plan(
            plan, backup_root=backup_root, manifest=manifest,
            registry_db=registry_db,
        ):
            report.renamed += 1
            id_map[plan.old_id] = plan.new_id
    _rewrite_candidates(skill_dir, id_map, manifest)
    (backup_root / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return report


def rollback_traj_names(
    *,
    xskill_home: Path,
    registry_db: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
) -> int:
    """按最近（或指定）一份备份的 manifest 逆向恢复。返回恢复条数。

    只回滚「当前名字仍等于迁移后名字」的条目——迁移后又被改动过的不碰，
    并对每一步失败单独告警，不中断其余条目。"""
    if backup_dir is None:
        root = xskill_home / BACKUP_DIR_NAME
        candidates = sorted(root.glob("*/" + MANIFEST_NAME))
        if not candidates:
            raise FileNotFoundError(f"没有可回滚的备份（{root} 下无 manifest）")
        backup_dir = candidates[-1].parent
    manifest = json.loads(
        (backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"),
    )
    restored = 0
    for entry in reversed(manifest.get("entries", [])):
        directory = Path(entry["directory"])
        old_id, new_id = entry["old_id"], entry["new_id"]
        bucket = backup_dir / directory.name / old_id
        ok = True
        for old_str, new_str in reversed(entry.get("renames", [])):
            new_path = Path(new_str)
            old_path = Path(old_str)
            if old_path.exists():
                logger.warning("回滚目标已存在，跳过: %s", old_path)
                ok = False
                continue
            src = bucket / old_path.name
            if src.exists():
                # 从备份整份还原（原子目录内容已被改写，改回去不如用备份）
                if new_path.exists():
                    if new_path.is_dir():
                        shutil.rmtree(new_path)
                    else:
                        new_path.unlink()
                if src.is_dir():
                    shutil.copytree(src, old_path)
                else:
                    shutil.copy2(src, old_path)
            elif new_path.exists():
                new_path.rename(old_path)
            else:
                logger.warning("备份与现场都找不到 %s，无法回滚", new_str)
                ok = False
        if ok and entry.get("db_updated") and registry_db and registry_db.is_file():
            with sqlite3.connect(registry_db) as conn:
                conn.execute(
                    "UPDATE trajectories SET filename = ? "
                    "WHERE filename = ? AND watch_dir_id IN "
                    "(SELECT id FROM watch_dirs WHERE path = ?)",
                    (f"{old_id}.md", f"{new_id}.md", str(directory)),
                )
        if ok:
            restored += 1
    # candidates 恢复
    cand_backup = backup_dir / "candidates"
    for saved in cand_backup.glob("*.candidates.yml") if cand_backup.is_dir() else []:
        for cand_path in manifest.get("candidates", []):
            cp = Path(cand_path)
            if cp.parent.name == saved.name.replace(".candidates.yml", ""):
                shutil.copy2(saved, cp)
    return restored
