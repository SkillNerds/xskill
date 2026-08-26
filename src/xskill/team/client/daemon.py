"""daemon.py — TeamClient 瘦客户端守护（SP1）

client 只干三件事：采集本地轨迹脱敏上传、持有 server 算出的 skill
working copy 并对齐 side、把本地手改推成 user-staging/<client_id> 分支。
零 LLM、零 git 写 main、零灰度判定。

_tick 一轮：
  collect_and_upload → sync → reconcile_skill_sides →
  reconcile_downloaded_skills → push_user_edits → cleanup
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import stat
import tempfile
import threading
import zipfile
from pathlib import Path

from xskill.skill.git import run_git
from xskill.ecosystems._history import InstallHistory
from xskill.ecosystems.installation import (
    GitHeadError,
    InstallSafetyError,
    InstallationMetadataError,
    copy_install_identity_matches,
    install_metadata_path,
    installed_mode,
    is_link_or_junction,
    link_install_metadata_is_current,
    read_install_metadata,
    read_install_metadata_file,
    read_skill_head_sha,
)
from xskill.team.client.state import ClientState
from xskill.team.client.collector import TeamCollector
from xskill.team.shared.git_bundle import apply_repo_bundle, make_branch_bundle
from xskill.team.shared.reconcile import reconcile_skill_side
from xskill.team.shared.protocol import (
    SyncResponse, UploadRequest, UploadTrajectory,
)

logger = logging.getLogger("xskill.team.client")


def register_with_server(
    http, *,
    token: str, label: str, hostname: str,
    existing_client_id: str | None = None,
    user_name: str | None = None,
) -> str:
    """跟 server 握手注册，返回 server 分配（或续用）的 client_id。

    ``existing_client_id`` 用于重连保持身份：调用方（CLI）若发现本地 state
    里已有 client_id，传过来；server 按 (user_name, claimed_client_id,
    fingerprint, new uuid) 四级优先级判定（详见 ClientRegistry.register）。

    ``user_name`` 即 ``--name <工号/userid>``：非空时 server 派生确定性 client_id
    （跨设备同 name 共享画像），优先于 claimed/fingerprint。
    """
    return register_with_server_full(
        http, token=token, label=label, hostname=hostname,
        existing_client_id=existing_client_id, user_name=user_name,
    )["client_id"]


def register_with_server_full(
    http, *,
    token: str, label: str, hostname: str,
    existing_client_id: str | None = None,
    user_name: str | None = None,
) -> dict:
    """同 ``register_with_server``，但返回完整响应 dict——CLI 用它拿
    ``dashboard_token``（P2-2.2）在 connect 成功时打印一次。"""
    from xskill import __version__ as _xskill_version
    body = {
        "token": token,
        "client_label": label,
        "hostname": hostname,
        "claimed_client_id": existing_client_id,
        # P2-2.10:server 写 clients.client_version,连接状态看板"落后"标注用
        "client_version": _xskill_version,
    }
    if user_name:
        body["user_name"] = user_name
    resp = http.post("/api/v1/team/register", json=body)
    if resp.status_code != 200:
        raise RuntimeError(
            f"register failed: HTTP {resp.status_code} — {resp.text}"
        )
    return resp.json()


class TeamClient:
    """team 瘦客户端。http 接受 httpx.Client 或 FastAPI TestClient。"""

    def __init__(
        self,
        *,
        state: ClientState,
        http,
        skill_dir: Path,
        cursor_path: Path,
        history_path: Path,
        home_root: Path | None = None,
        poll_interval: float = 30.0,
        quiet_seconds: int = 180,
        min_change_interval: int = 600,
        auto_update: bool = True,
        use_proxy: bool = False,
    ):
        self.state = state
        self.http = http
        # 普通 client：工作副本落 ~/.xskill/skill/，与 standalone 同一位置。
        # 本机已是 team server 时不能再用自有仓——cleanup 会按派发清单删目录。
        from xskill.config import resolve_team_client_skill_dir
        requested = Path(skill_dir)
        self.skill_dir = resolve_team_client_skill_dir(requested)
        if self.skill_dir != requested:
            logger.warning(
                "colocated team client skill_dir=%s (server canonical preserved)",
                self.skill_dir,
            )
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        self.home_root = Path(home_root) if home_root else Path.home()
        self.poll_interval = poll_interval
        self.history = InstallHistory(history_path)
        self.collector = TeamCollector(
            cursor_path=Path(cursor_path),
            quiet_seconds=quiet_seconds, home_root=self.home_root,
            min_change_interval=min_change_interval,
        )
        self._stop = threading.Event()
        self.auto_update = auto_update
        # updater 的 server 方向请求跟随 connect 的 --use-proxy；默认直连内网 server。
        self.use_proxy = use_proxy

    # ── HTTP 鉴权头 ──────────────────────────────────────────────
    def _hdr(self, extra: dict | None = None) -> dict:
        from xskill import __version__ as _xskill_version
        h = {"X-Xskill-Token": self.state.join_token,
             "X-Xskill-Client": self.state.client_id,
             # P2-2.10:每次 sync 携带版本,server touch 时 upsert 进 clients 表
             "X-Xskill-Version": _xskill_version}
        if extra:
            h.update(extra)
        return h

    # ── ① 采集 + 上传 ────────────────────────────────────────────
    def collect_and_upload(self) -> int:
        """扫 outbox 静默轨迹，脱敏后上传 server。返回成功上传条数。"""
        pending = self.collector.pending()
        if not pending:
            return 0
        req = UploadRequest(trajectories=[
            UploadTrajectory(traj_id=p.traj_id, content=p.content, sha256=p.sha256,
                             model=p.model, harness=p.harness)
            for p in pending
        ])
        resp = self.http.post("/api/v1/team/upload", headers=self._hdr(),
                              json=req.model_dump())
        if resp.status_code != 200:
            logger.warning("upload failed http_status=%s", resp.status_code)
            return 0
        accepted = set(resp.json().get("accepted", []))
        for p in pending:
            if p.traj_id in accepted:
                self.collector.mark_uploaded(p.traj_id, p.sha256)
        logger.info("uploaded %d trajectories", len(accepted))
        return len(accepted)

    # ── ② sync ──────────────────────────────────────────────────
    def sync(self) -> SyncResponse:
        """拉 server 现算的 skill manifest。"""
        resp = self.http.get("/api/v1/team/sync", headers=self._hdr())
        if resp.status_code != 200:
            raise RuntimeError(f"sync failed: HTTP {resp.status_code} — {resp.text}")
        return SyncResponse.model_validate(resp.json())

    @staticmethod
    def apply_client_take(manifest: SyncResponse) -> SyncResponse:
        """按 server 下发的 ``take_n`` 截取安装队列；``None``=装全部（旧 server）。

        截取后的 slots 同时驱动 reconcile 与 cleanup——减小 N 时尾部会被卸掉。
        """
        if manifest.take_n is None:
            return manifest
        n = max(0, int(manifest.take_n))
        if n >= len(manifest.slots):
            return manifest
        manifest.slots = list(manifest.slots[:n])
        return manifest

    def _refuse_canonical_skill_dir(self, action: str) -> bool:
        """本机 team server 自有仓禁止当 client working copy 来改、删。"""
        from xskill.config import is_team_server_canonical_skill_dir
        if not is_team_server_canonical_skill_dir(self.skill_dir):
            return False
        logger.error(
            "refusing client %s of team server skill repo",
            action,
        )
        return True

    # ── ③ reconcile ─────────────────────────────────────────────
    def reconcile_skill_sides(self, manifest: SyncResponse) -> None:
        """对 manifest 每个 slot：拉 bundle → 对齐 side → 装到本机生态。

        这是设计里约定的 reconcile_skill_sides——契约步骤 1（决定 target）
        就是读 manifest slot 的 side/sha；步骤 2/3/4 走共享
        reconcile_skill_side。
        """
        if self._refuse_canonical_skill_dir("reconcile"):
            return
        for slot in manifest.slots:
            repo_dir = self.skill_dir / slot.skill_name
            # 拉 bundle 落地/刷新本地 working copy
            r = self.http.get(f"/api/v1/team/skill/{slot.skill_name}/bundle",
                              headers=self._hdr())
            if r.status_code != 200:
                logger.warning(
                    "bundle fetch failed skill_id_hash=%s http_status=%s",
                    hashlib.sha256(
                        slot.skill_name.encode("utf-8"),
                    ).hexdigest()[:12],
                    r.status_code,
                )
                continue
            if getattr(slot, "source", "repo") == "skillhub":
                # 与 repo slot 的 on_changed 语义对齐：内容没变不重装生态
                if self._apply_skillhub_archive(
                    r.content, repo_dir, expected_sha=slot.sha,
                    display_name=slot.display_name,
                    source_path=slot.source_path,
                ):
                    self._install_to_ecosystems(repo_dir)
                continue
            apply_repo_bundle(r.content, repo_dir)
            # 步骤 1 = manifest 给的 (side, sha)；2/3/4 = 共享助手
            reconcile_skill_side(
                repo_dir=repo_dir, target_side=slot.side, target_sha=slot.sha,
                history=self.history, on_changed=self._install_to_ecosystems,
            )
        logger.info("reconciled %d skills", len(manifest.slots))

    def _apply_skillhub_archive(
        self, archive_bytes: bytes, dest_dir: Path, *, expected_sha: str,
        display_name: str | None, source_path: str | None,
    ) -> bool:
        return apply_skillhub_archive(
            archive_bytes, dest_dir, expected_sha=expected_sha,
            display_name=display_name, source_path=source_path,
        )

    def _install_to_ecosystems(self, repo_dir: Path) -> None:
        install_skill_to_ecosystems(repo_dir, home_root=self.home_root)

    def reconcile_downloaded_skills(self) -> int:
        """刷新显式下载项；持久下载不占 search LRU，随服务端版本继续更新。"""
        from xskill.team.client.search_slots import (
            DownloadedSkills,
            _valid_slot_id,
        )

        manager = DownloadedSkills(
            xskill_home=self.skill_dir.parent,
            home_root=self.home_root,
        )
        updated = 0
        for entry in manager.entries():
            skill_id = entry.get("skill_id")
            if not isinstance(skill_id, str) or not _valid_slot_id(skill_id):
                logger.warning(
                    "ignored invalid downloaded skill id error_type="
                    "DOWNLOAD_LEDGER_ID_INVALID",
                )
                continue
            skill_hash = hashlib.sha256(
                skill_id.encode("utf-8"),
            ).hexdigest()[:12]
            try:
                metadata_response = self.http.get(
                    f"/api/v1/team/skill_hub/entry/{skill_id}",
                    headers=self._hdr(),
                )
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "download refresh metadata failed skill_id_hash=%s "
                    "error_type=DOWNLOAD_REFRESH_REQUEST_FAILED",
                    skill_hash,
                )
                continue
            if metadata_response.status_code != 200:
                logger.warning(
                    "download refresh metadata failed skill_id_hash=%s "
                    "http_status=%s",
                    skill_hash, metadata_response.status_code,
                )
                continue
            try:
                metadata_payload = metadata_response.json()
            except (TypeError, ValueError):
                metadata_payload = {}
            result = (
                metadata_payload.get("result")
                if isinstance(metadata_payload, dict) else None
            )
            if not isinstance(result, dict):
                logger.warning(
                    "download refresh metadata invalid skill_id_hash=%s",
                    skill_hash,
                )
                continue
            local_skill = manager.skills_dir / skill_id / "SKILL.md"
            if (
                entry.get("sha") == result.get("content_sha")
                and local_skill.is_file()
            ):
                continue
            try:
                bundle = self.http.get(
                    f"/api/v1/team/skill/{skill_id}/bundle",
                    headers=self._hdr(),
                )
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "download refresh bundle failed skill_id_hash=%s "
                    "error_type=DOWNLOAD_REFRESH_REQUEST_FAILED",
                    skill_hash,
                )
                continue
            if bundle.status_code != 200:
                logger.warning(
                    "download refresh bundle failed skill_id_hash=%s "
                    "http_status=%s",
                    skill_hash, bundle.status_code,
                )
                continue
            stored_agents = entry.get("agents")
            refresh_agents = (
                [str(agent) for agent in stored_agents]
                if isinstance(stored_agents, list)
                and all(isinstance(agent, str) for agent in stored_agents)
                else None
            )
            try:
                manager.install(
                    result, bundle.content, ecosystems=refresh_agents,
                )
            except (
                OSError, RuntimeError, ValueError, zipfile.BadZipFile,
            ):
                logger.warning(
                    "download refresh install failed skill_id_hash=%s "
                    "error_type=DOWNLOAD_REFRESH_INSTALL_FAILED",
                    skill_hash,
                )
                continue
            updated += 1
            logger.info(
                "refreshed downloaded skill skill_id_hash=%s",
                skill_hash,
            )
        return updated

    # ── ④ push 用户手改 ──────────────────────────────────────────
    def push_user_edits(self) -> int:
        """检测本地 working copy 的未吸收手改，推成 user-staging/<client_id>。

        返回推送成功的 skill 数。client 是愚蠢且可能恶意的——它推过去的
        只能进隔离分支，永远碰不到 main。

        openclaw 用户改的是 dest copy（``~/.agents/skills/<name>/``），不会
        自动到 working copy。每个 skill 先跑 reverse_sync_openclaw_dest 把
        dest 改灌回 working copy，下面 git status 才能看到。
        """
        if self._refuse_canonical_skill_dir("push-edit"):
            return 0
        from xskill.agents.user_edit_absorb_agent import (
            ReverseSyncStatus,
            reverse_sync_openclaw_dest,
        )

        pushed = 0
        for repo_dir in sorted(self.skill_dir.iterdir()):
            if not (repo_dir / ".git").is_dir():
                continue

            # openclaw 回流（dest → working copy）— 没装到 openclaw 时 no-op
            dest_dir = self.home_root / ".agents" / "skills" / repo_dir.name
            try:
                reverse_status = reverse_sync_openclaw_dest(
                    dest_dir, repo_dir,
                )
            except Exception:
                logger.warning(
                    "openclaw reverse sync stopped skill_id_hash=%s "
                    "error_type=REVERSE_SYNC_UNEXPECTED",
                    hashlib.sha256(
                        repo_dir.name.encode("utf-8"),
                    ).hexdigest()[:12],
                )
                continue
            if reverse_status in {
                ReverseSyncStatus.RECENT_EDIT,
                ReverseSyncStatus.FAILED,
            }:
                logger.warning(
                    "openclaw reverse sync stopped skill_id_hash=%s "
                    "error_type=%s",
                    hashlib.sha256(
                        repo_dir.name.encode("utf-8"),
                    ).hexdigest()[:12],
                    (
                        "REVERSE_SYNC_RECENT_EDIT"
                        if reverse_status == ReverseSyncStatus.RECENT_EDIT
                        else "REVERSE_SYNC_FAILED"
                    ),
                )
                continue
            if reverse_status not in {
                ReverseSyncStatus.NO_EDIT,
                ReverseSyncStatus.SYNCED,
            }:
                logger.warning(
                    "openclaw reverse sync stopped skill_id_hash=%s "
                    "error_type=REVERSE_SYNC_INVALID_STATUS",
                    hashlib.sha256(
                        repo_dir.name.encode("utf-8"),
                    ).hexdigest()[:12],
                )
                continue

            # 用 git status 当门——直接看工作树相对 HEAD 的真实差异（含
            # untracked）。不用 has_pending_user_edit 的 mtime 启发式：
            # reconcile 刚做的 git checkout 会把 SKILL.md mtime 抬到 now，
            # 而 commit_ts 是几秒前的（commit ≥1s 早于 checkout）→ mtime
            # 启发式会对**每个 reconcile 过的 skill** 都误判"有手改"，造成
            # 每轮 _tick 给所有 skill 刷一次 commit 尝试和警告日志。
            code, status_out, _ = run_git(["status", "--porcelain"], cwd=str(repo_dir))
            if code != 0 or not status_out.strip():
                continue   # 无真实手改（含 untracked）
            # 把手改 commit 到 _useredit 分支（从当前 _active 起）
            run_git(["checkout", "-B", "_useredit"], cwd=str(repo_dir))
            run_git(["add", "-A"], cwd=str(repo_dir))
            code, out, err = run_git(
                ["commit", "-m", f"user edit from {self.state.client_id}"],
                cwd=str(repo_dir),
            )
            if code != 0:
                combined = (out + err).strip()
                # "nothing to commit" 走 stdout 不走 stderr；且既然
                # status --porcelain 之前非空,这里走到 nothing-to-commit
                # 多半是 .gitignore 把改动全屏蔽了——静默跳过不报警。
                if "nothing to commit" in combined:
                    continue
                logger.warning(
                    "commit user edit failed skill_id_hash=%s "
                    "error_type=GIT_COMMIT_FAILED",
                    hashlib.sha256(
                        repo_dir.name.encode("utf-8"),
                    ).hexdigest()[:12],
                )
                continue
            bundle = make_branch_bundle(repo_dir, "_useredit")
            resp = self.http.post(
                "/api/v1/team/push-edit",
                headers=self._hdr({"X-Xskill-Skill": repo_dir.name}),
                content=bundle,
            )
            if resp.status_code == 200:
                pushed += 1
                logger.info(
                    "pushed user edit skill_id_hash=%s",
                    hashlib.sha256(
                        repo_dir.name.encode("utf-8"),
                    ).hexdigest()[:12],
                )
            else:
                logger.warning(
                    "push-edit failed skill_id_hash=%s http_status=%s",
                    hashlib.sha256(
                        repo_dir.name.encode("utf-8"),
                    ).hexdigest()[:12],
                    resp.status_code,
                )
        return pushed

    # ── ⑤ cleanup ───────────────────────────────────────────────
    def cleanup(self, manifest: SyncResponse) -> None:
        """删掉本地 working copy 里 manifest 已不包含的 skill。

        client 的 skill 集合完全由 server 算出的 manifest 决定——server 把
        某 skill 移出 100 → 下次 sync 后本地也删，不自留。
        """
        from xskill.ecosystems.install_ledger import get_default_ledger

        get_default_ledger().migrate_from_sidecars(
            _ecosystem_skill_roots(self.home_root),
        )
        if self._refuse_canonical_skill_dir("cleanup"):
            return
        keep = {s.skill_name for s in manifest.slots}
        for repo_dir in sorted(self.skill_dir.iterdir()):
            if (
                not repo_dir.is_dir()
                or repo_dir.name.startswith(".")
                or repo_dir.name in keep
            ):
                continue
            # 先摘掉仍指向该 working copy 的生态安装，再删本地仓
            self._uninstall_from_ecosystems(repo_dir)
            shutil.rmtree(repo_dir, ignore_errors=True)
            logger.info(
                "cleanup removed stale skill skill_id_hash=%s",
                hashlib.sha256(
                    repo_dir.name.encode("utf-8"),
                ).hexdigest()[:12],
            )
        # 上面的 working-copy 驱动清理看不见"工作副本已被 out-of-band 删除、生态
        # link 却还在"的孤儿；按生态目录反向再收一遍。
        self._reap_orphaned_ecosystem_links(keep)
        # copy 孤儿（有老 meta、无账本或卸装拒删）同样逃出 working-copy 驱动清理；
        # 推荐流 delta 必须能清掉，否则 .agents 只增不减。
        self._reap_orphan_copy_dests(keep)

    def _reap_orphaned_ecosystem_links(self, keep: set[str]) -> None:
        """扫生态 dest 根目录，收掉 manifest 已不含、且指向 xskill 工作副本根的
        link/junction（含工作副本被删后留下的 dangling 孤儿）。

        working-copy 驱动的 cleanup 遍历 ``skill_dir``，看不到"工作副本已消失但
        生态 link 还在"的孤儿——它们永远清不掉，在 ``~/.claude/skills`` 等目录越积
        越多（Windows 卸 junction 失败尤甚）。这里按生态目录反向收敛：仅当 link 的
        realpath 落在 ``skill_dir`` 根内（= xskill 自己装的）且名字不在 keep 集时才
        删；真目录 / 手动建 / 指向别处的第三方 link 一律不碰。名字在 keep 里的
        dangling link 留给 reconcile 重装，不在这里删。
        """
        skill_root_key = _source_path_key(self.skill_dir)
        for root in _ecosystem_skill_roots(self.home_root):
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if entry.name in keep:
                    continue  # manifest 仍需要 → 保留（dangling 也留给 reconcile 重装）
                # 只收 link/junction：真目录（手动建 skill）与 copy 安装一律不碰。
                if not is_link_or_junction(entry):
                    continue
                entry_key = _source_path_key(entry)
                if not (entry_key == skill_root_key
                        or entry_key.startswith(skill_root_key + os.sep)):
                    continue  # link 不指向 xskill 工作副本根 → 第三方安装，跳过
                if _remove_owned_install_target(
                    entry, Path(_source_path_key(entry)),
                ):
                    logger.info(
                        "reaped orphaned ecosystem link target_hash=%s",
                        _target_path_hash(entry),
                    )

    def _reap_orphan_copy_dests(self, keep: set[str]) -> None:
        """收掉 manifest 已不含、带 dest 内老 install-meta 的 copy 真目录。

        身份：``dest/.xskill-install-meta.json`` 证明曾由 xskill copy 安装（相对
        无痕迹手建目录）。名字仍在 keep 的留给 reconcile，不删——避免把仍在
        推荐里、同样带老 meta 的正常 openclaw 安装每轮拆掉重装。

        手改保护：文件 mtime 相对 meta ``installed_at`` 已前进（含静默期内）则
        跳过，避免在回流/push-edit 之前清掉本地改动。无账本时 ``remove_owned_dest``
        拒删，故此处在安全判定后直接 ``rmtree``。
        """
        from xskill.ecosystems.install_ledger import get_default_ledger

        ledger = get_default_ledger()
        for root in _ecosystem_skill_roots(self.home_root):
            if not root.is_dir():
                continue
            try:
                entries = sorted(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name in keep:
                    continue
                if is_link_or_junction(entry):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if not (entry / _LEGACY_DEST_INSTALL_META).is_file():
                    continue
                # 有账本行：走正式卸装（指纹不符=用户改过则拒删，与现语义一致）
                if ledger.read_install(entry) is not None:
                    if _remove_owned_install_target(entry, None):
                        logger.info(
                            "reaped orphan copy dest via ledger "
                            "target_hash=%s",
                            _target_path_hash(entry),
                        )
                    continue
                if not _orphan_copy_content_matches_install(entry):
                    logger.info(
                        "skip orphan copy reap (dest edit markers) "
                        "target_hash=%s",
                        _target_path_hash(entry),
                    )
                    continue
                try:
                    shutil.rmtree(entry)
                except OSError:
                    logger.warning(
                        "reap orphan copy dest failed target_hash=%s "
                        "error_type=ORPHAN_COPY_REAP_FAILED",
                        _target_path_hash(entry),
                    )
                    continue
                logger.info(
                    "reaped orphan copy dest target_hash=%s",
                    _target_path_hash(entry),
                )

    def _uninstall_from_ecosystems(self, repo_dir: Path) -> None:
        uninstall_skill_from_ecosystems(
            repo_dir.name, home_root=self.home_root, source_dir=repo_dir,
        )

    # ── 守护循环 ─────────────────────────────────────────────────
    def _tick(self) -> None:
        try:
            self.collect_and_upload()
            manifest = self.apply_client_take(self.sync())
            self.reconcile_skill_sides(manifest)
            self.reconcile_downloaded_skills()
            self.push_user_edits()
            self.cleanup(manifest)
        except Exception as tick_error:
            logger.warning(
                "team client tick failed error_type=%s",
                type(tick_error).__name__,
            )

    def run_forever(self) -> None:
        """阻塞循环。先起 collector ingester，再每 poll_interval 跑一轮 _tick。"""
        from xskill.team.client.updater import AutoUpdater
        updater = AutoUpdater(
            server_url=self.state.server_url,
            client_id=self.state.client_id,
            join_token=self.state.join_token,
            use_proxy=self.use_proxy,
        ) if self.auto_update else None
        if updater:
            updater.start()
        self.collector.start_ingesters()
        logger.info(
            "team client running server_hash=%s client_id_hash=%s",
            hashlib.sha256(
                self.state.server_url.encode("utf-8"),
            ).hexdigest()[:12],
            hashlib.sha256(
                self.state.client_id.encode("utf-8"),
            ).hexdigest()[:12],
        )
        try:
            while not self._stop.is_set():
                self._tick()
                self._stop.wait(self.poll_interval)
        finally:
            if updater:
                updater.stop()
            self.collector.stop_ingesters()

    def stop(self) -> None:
        self._stop.set()


def apply_skillhub_archive(
    archive_bytes: bytes, dest_dir: Path, *, expected_sha: str,
    display_name: str | None, source_path: str | None,
    marker_name: str = ".xskill_skillhub.json",
    extra_meta: dict | None = None,
) -> bool:
    """把非 git 的 skillhub skill zip 原子落到本地目录（带路径穿越防护）。

    sync reconcile 与 `xskill search` 槽位共用；后者用 ``marker_name`` /
    ``extra_meta`` 换成自己的标记文件。安装判断使用实际 zip 内容哈希，而不是仅覆盖
    SKILL.md 的 ``expected_sha``，确保 scripts/references/assets 单独变化也会更新。

    返回是否真正落了新内容——``False`` 表示 zip 哈希命中现有安装、盘上没动。
    """
    dest_dir = Path(dest_dir)
    meta_path = dest_dir / marker_name
    archive_sha = hashlib.sha256(archive_bytes).hexdigest()
    if meta_path.is_file() and (dest_dir / "SKILL.md").is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("archive_sha") == archive_sha:
                return False
        except (OSError, ValueError):
            pass

    tmp_dir = dest_dir.with_name(f".{dest_dir.name}.tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        root = tmp_dir.resolve()
        for info in zf.infolist():
            target = (tmp_dir / info.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                raise RuntimeError(f"unsafe skillhub archive path: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    if not (tmp_dir / "SKILL.md").is_file():
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("skillhub archive missing SKILL.md")
    meta = {
        "sha": expected_sha,
        "archive_sha": archive_sha,
        "display_name": display_name,
        "source_path": source_path,
    }
    if extra_meta:
        meta.update(extra_meta)
    (tmp_dir / marker_name).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8",
    )
    shutil.rmtree(dest_dir, ignore_errors=True)
    tmp_dir.replace(dest_dir)
    return True


def _valid_skill_name(skill_name: str) -> bool:
    """安装目标名必须是单个路径段，不能逃出各生态的 skills 根目录。"""
    return bool(skill_name) and skill_name not in {".", ".."} \
        and "/" not in skill_name and "\\" not in skill_name \
        and "\x00" not in skill_name


def _targets_for_ecosystem(ecosystem: str, skill_name: str,
                           home_root: Path) -> list[Path]:
    """返回一个安装器本次可能写入的全部目标（Trae 可能有两个）。"""
    from xskill.ecosystems import (
        _agents_skills_path, _cc_skills_path, _cursor_skills_path,
        _nga3_skills_path, _ngagent_skills_path, _trae_skills_roots,
    )

    shared = _agents_skills_path(home_root) / skill_name
    roots = {
        "claude_code": [_cc_skills_path(home_root) / skill_name],
        "codex": [shared],
        "opencode": [shared],
        "openclaw": [shared],
        "ngagent": [_ngagent_skills_path(home_root) / skill_name],
        "nga3": [_nga3_skills_path(home_root) / skill_name],
        "cursor": [_cursor_skills_path(home_root) / skill_name],
        "trae": [root / skill_name for root in _trae_skills_roots(home_root)],
    }
    return roots.get(ecosystem, [])


def _ecosystem_skill_roots(home_root: Path) -> list[Path]:
    """所有安装器的 skill 目标根目录；不依赖生态当前是否仍可探测。"""
    from xskill.ecosystems import (
        _agents_skills_path, _cc_skills_path, _cursor_skills_path,
        _nga3_skills_path, _ngagent_skills_path,
    )

    return [
        _cc_skills_path(home_root),
        _agents_skills_path(home_root),
        _nga3_skills_path(home_root),
        _ngagent_skills_path(home_root),
        _cursor_skills_path(home_root),
        home_root / ".trae-cn" / "skills",
        home_root / ".trae" / "skills",
    ]


# openclaw / copy 安装写在 dest 内部的老 meta（与旁路 sidecar 不同）。
_LEGACY_DEST_INSTALL_META = ".xskill-install-meta.json"


def _orphan_copy_content_matches_install(dest: Path) -> bool:
    """无账本 copy 孤儿：内容是否仍像「刚装好、无手改」。

    与 reverse_sync 的 dest 判定同口径：可读 ``installed_at``，且工作区文件
    max(mtime) 相对安装时刻未前进 ≥1s。读失败或已有改动痕迹 → False（不删）。
    """
    meta_path = dest / _LEGACY_DEST_INSTALL_META
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return False
    if not isinstance(meta, dict):
        return False
    installed_at = meta.get("installed_at")
    if isinstance(installed_at, bool) or not isinstance(installed_at, (int, float)):
        return False
    max_mtime = 0.0
    try:
        for dirpath, dirnames, filenames in os.walk(dest):
            # 不把 meta / marker 自身的写入当成用户手改
            if Path(dirpath) == dest:
                dirnames[:] = [d for d in dirnames if d != ".git"]
                filenames = [
                    n for n in filenames
                    if n not in {
                        _LEGACY_DEST_INSTALL_META,
                        ".xskill-install-identity.json",
                    }
                ]
            for name in filenames:
                try:
                    max_mtime = max(
                        max_mtime,
                        (Path(dirpath) / name).lstat().st_mtime,
                    )
                except OSError:
                    return False
    except OSError:
        return False
    return max_mtime - float(installed_at) < 1.0


def _all_install_targets(skill_name: str, home_root: Path) -> list[Path]:
    """返回当前所有安装器使用的目标；不依赖生态当前是否仍可探测。"""
    return [root / skill_name for root in _ecosystem_skill_roots(home_root)]


def _lexical_path_key(path: Path) -> str:
    """不跟随 symlink 的绝对路径键，用于目标 allowlist 和去重。"""
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def _source_path_key(path: Path) -> str:
    """跟随 link 后的源路径键；Windows 上同时折叠大小写。"""
    if path.is_symlink():
        link_target = Path(os.readlink(path))
        if not link_target.is_absolute():
            link_target = path.parent / link_target
        resolved_path = os.path.abspath(
            os.path.normpath(str(link_target)),
        )
    else:
        resolved_path = str(path.resolve(strict=False))
    if os.name == "nt":
        if resolved_path.startswith("\\\\?\\UNC\\"):
            resolved_path = "\\\\" + resolved_path[8:]
        elif resolved_path.startswith("\\\\?\\"):
            resolved_path = resolved_path[4:]
    return os.path.normcase(resolved_path)


def _installation_metadata(dest: Path) -> dict | None:
    """读取安装器写在 target 旁的元数据；明确缺失时返回 ``None``。"""
    return read_install_metadata(dest)


def _installed_mode(dest: Path) -> str | None:
    return installed_mode(dest)


def _copy_target_matches_source(dest: Path, source_dir: Path) -> bool:
    """用版本元数据和 SKILL.md 内容确认 copy 目标是当前 source 的快照。

    只读取两个 SKILL.md 和常数个 marker/meta，不递归扫描整个 skill。调用方还会
    按唯一 ``(target, source)`` 缓存结果，多个共享 harness 不会重复计算。
    """
    install_meta = _installation_metadata(dest)
    if (
        install_meta is None
        or install_meta.get("mode") != "copy"
        or not isinstance(install_meta.get("source"), str)
        or _source_path_key(Path(install_meta["source"]))
        != _source_path_key(source_dir)
    ):
        return False

    skill_hashes: list[str] = []
    for skill_md in (source_dir / "SKILL.md", dest / "SKILL.md"):
        digest = hashlib.sha256()
        try:
            with open(skill_md, "rb") as skill_file:
                while chunk := skill_file.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            return False
        skill_hashes.append(digest.hexdigest())
    if skill_hashes[0] != skill_hashes[1]:
        return False

    recorded_sha = install_meta.get("source_sha")
    if isinstance(recorded_sha, str) and recorded_sha:
        return (
            read_skill_head_sha(source_dir) == recorded_sha
        )
    return (
        copy_install_identity_matches(
            dest, source_dir, metadata=install_meta,
        )
        and _matching_source_version_marker(dest, source_dir)
    )


def install_skill_to_ecosystems(
    repo_dir: Path, *, home_root: Path,
    ecosystems: list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    """把一个已就位的 skill 目录装到指定或本机已检测到的生态。

    working tree 已是最终内容，一律用 side='main' 语义（= 链接 / 拷贝整个
    目录）。openclaw 走 copy 不是 symlink（openclaw 拒收 escape-root 的
    symlink，详见 docs/ecosystem/openclaw-install-fix.md）；其他生态保持
    symlink-first 三阶 fallback。``ecosystems=None`` 保持旧行为，安装到所有
    已检测生态；显式列表则不依赖探测，按安装器固定顺序精确安装。返回每个生态
    的本次安装结果；共享目标的生态各保留一条记录，不能按 target 覆盖 harness
    归属。
    """
    from xskill.ecosystems import (
        detect_known_ecosystems, install_to_claude_code,
        install_to_codex, install_to_deepseek_harness, install_to_nga3,
        install_to_opencode, install_to_ngagent,
        install_to_openclaw, install_to_cursor, install_to_trae,
    )
    repo_dir = Path(repo_dir).resolve()
    home_root = Path(home_root).resolve(strict=False)
    if not _valid_skill_name(repo_dir.name):
        raise ValueError(f"invalid skill directory name: {repo_dir.name!r}")
    log_skill_id = hashlib.sha256(
        repo_dir.name.encode("utf-8"),
    ).hexdigest()[:12]

    installer = {
        "claude_code": install_to_claude_code,
        "codex": install_to_codex,
        "nga3": install_to_nga3,
        "opencode": install_to_opencode,
        "ngagent": install_to_ngagent,
        "openclaw": install_to_openclaw,
        "cursor": install_to_cursor,
        "trae": install_to_trae,
        "deepseek_harness": install_to_deepseek_harness,
    }
    if ecosystems is None:
        ecosystem_names = [
            str(det.get("ecosystem") or "")
            for det in detect_known_ecosystems(home_root=home_root)
        ]
    else:
        requested = set(ecosystems)
        unknown = sorted(requested.difference(installer))
        if unknown:
            raise ValueError(
                f"unsupported ecosystem(s): {', '.join(unknown)}"
            )
        # 固定顺序很重要：OpenClaw 必须在 Codex/OpenCode 之后，把共享目标
        # 收敛为 copy，不能让 --agent 参数顺序改变最终安装形态。
        ecosystem_names = [
            ecosystem for ecosystem in installer if ecosystem in requested
        ]
    installation_records: list[dict] = []
    for ecosystem in ecosystem_names:
        install_function = installer.get(ecosystem)
        if install_function is None:
            continue
        targets = _targets_for_ecosystem(
            ecosystem, repo_dir.name, home_root,
        )
        try:
            install_function(repo_dir, target_root=home_root, side="main")
            for target in targets:
                installation_records.append({
                    "ecosystem": ecosystem,
                    "target": str(target),
                    "source": str(repo_dir),
                })
            logger.info(
                "installed skill_id_hash=%s ecosystem=%s",
                log_skill_id, ecosystem,
            )
        except Exception as install_error:
            if isinstance(install_error, GitHeadError):
                error_code = "GIT_HEAD_INVALID"
                error_detail = "Git HEAD 校验失败，请检查 skill 仓库完整性"
            elif isinstance(install_error, InstallationMetadataError):
                if (
                    install_error.error_type
                    == "INSTALL_METADATA_WRITE_FAILED"
                ):
                    error_code = "INSTALL_METADATA_WRITE_FAILED"
                    error_detail = "安装元数据写入失败，请检查目标目录权限"
                else:
                    error_code = "INSTALL_METADATA_INVALID"
                    error_detail = "安装元数据损坏或不可读，请检查目标目录状态"
            elif isinstance(install_error, InstallSafetyError):
                if install_error.error_type == "REVERSE_SYNC_RECENT_EDIT":
                    error_code = "USER_EDIT_IN_PROGRESS"
                    error_detail = "检测到用户仍在编辑，已保留现有安装目录"
                else:
                    error_code = "REVERSE_SYNC_FAILED"
                    error_detail = "用户修改回流失败，已保留现有安装目录"
            elif isinstance(install_error, PermissionError):
                error_code = "TARGET_PERMISSION_DENIED"
                error_detail = "目标目录不可写，请检查目录权限"
            elif isinstance(install_error, FileNotFoundError):
                error_code = "INSTALL_PATH_NOT_FOUND"
                error_detail = "安装源或目标路径不存在"
            elif isinstance(install_error, NotADirectoryError):
                error_code = "INSTALL_PATH_NOT_DIRECTORY"
                error_detail = "安装路径不是目录"
            elif isinstance(install_error, OSError):
                error_code = "FILESYSTEM_ERROR"
                error_detail = "文件系统操作失败，请检查目录状态和可用空间"
            else:
                error_code = "INSTALLER_ERROR"
                error_detail = "安装器执行失败，请查看本机 xskill 日志"
            logger.warning(
                "install failed skill_id_hash=%s ecosystem=%s error_code=%s",
                log_skill_id, ecosystem, error_code,
            )
            for target in targets:
                installation_records.append({
                    "ecosystem": ecosystem,
                    "target": str(target),
                    "source": str(repo_dir),
                    "error_code": error_code,
                    "error": error_detail,
                })

    # OpenClaw 会把 Codex/OpenCode 共用的 link 目标改成 copy。全部安装器完成后
    # 对每个 ecosystem/target 读取最终状态：后续 harness 修复了共享 target 时，
    # 早先失败的 harness 也应标为可用；Trae 多 target 则逐目标独立判断。
    verification_cache: dict[
        tuple[str, str],
        tuple[str | None, bool, str | None, str | None],
    ] = {}
    for record in installation_records:
        target = Path(record["target"])
        verification_key = (
            _lexical_path_key(target), _source_path_key(repo_dir),
        )
        verified = verification_cache.get(verification_key)
        if verified is None:
            verification_error_code = None
            verification_error = None
            try:
                mode = _installed_mode(target)
                target_is_current = (
                    mode is not None
                    and _target_owned_by_source(target, repo_dir)
                )
                if target_is_current and mode == "copy":
                    target_is_current = _copy_target_matches_source(
                        target, repo_dir,
                    )
            except InstallationMetadataError:
                mode = None
                target_is_current = False
                verification_error_code = "INSTALL_METADATA_INVALID"
                verification_error = (
                    "安装元数据损坏或不可读，请检查目标目录状态"
                )
            except GitHeadError:
                mode = None
                target_is_current = False
                verification_error_code = "GIT_HEAD_INVALID"
                verification_error = (
                    "Git HEAD 校验失败，请检查 skill 仓库完整性"
                )
            verified = (
                mode,
                target_is_current,
                verification_error_code,
                verification_error,
            )
            verification_cache[verification_key] = verified
        (
            mode,
            target_is_current,
            verification_error_code,
            verification_error,
        ) = verified
        if (
            target_is_current
            and record.get("error_code") == "INSTALL_METADATA_WRITE_FAILED"
        ):
            try:
                target_is_current = link_install_metadata_is_current(
                    target, repo_dir,
                )
            except InstallationMetadataError:
                target_is_current = False
                verification_error_code = "INSTALL_METADATA_INVALID"
                verification_error = (
                    "安装元数据损坏或不可读，请检查目标目录状态"
                )
            except GitHeadError:
                target_is_current = False
                verification_error_code = "GIT_HEAD_INVALID"
                verification_error = (
                    "Git HEAD 校验失败，请检查 skill 仓库完整性"
                )
        if record.get("error_code") in {
            "USER_EDIT_IN_PROGRESS",
            "REVERSE_SYNC_FAILED",
        }:
            target_is_current = False
        if target_is_current:
            record["status"] = "installed"
            record["mode"] = mode
            record.pop("error_code", None)
            record.pop("error", None)
            continue
        record["status"] = "failed"
        record.pop("mode", None)
        if verification_error_code is not None:
            record["error_code"] = verification_error_code
            record["error"] = verification_error
        elif "error_code" not in record:
            if mode is None:
                record["error_code"] = "INSTALL_TARGET_MISSING"
                record["error"] = "安装完成后未找到目标目录"
            else:
                record["error_code"] = "INSTALL_TARGET_NOT_OWNED"
                record["error"] = "目标目录未指向本次下载的 skill"
        logger.warning(
            "install verification failed skill_id_hash=%s "
            "ecosystem=%s target_hash=%s",
            log_skill_id,
            record["ecosystem"],
            hashlib.sha256(
                _lexical_path_key(target).encode(
                    "utf-8", errors="surrogatepass",
                ),
            ).hexdigest()[:16],
        )
    return installation_records


def _matching_source_version_marker(
    dest: Path, source_dir: Path,
) -> bool:
    """只用于新鲜度验证，不参与 copy 删除所有权判定。"""
    for marker_name in (
        ".xskill_download.json",
        ".xskill_search.json",
        ".xskill_skillhub.json",
    ):
        source_marker = source_dir / marker_name
        dest_marker = dest / marker_name
        if not source_marker.is_file() or not dest_marker.is_file():
            continue
        try:
            source_meta = json.loads(source_marker.read_text(encoding="utf-8"))
            dest_meta = json.loads(dest_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (
            isinstance(source_meta, dict)
            and source_meta == dest_meta
        )
    return False


def _target_owned_by_source(dest: Path, source_dir: Path) -> bool:
    """根据当前文件系统状态判断目标是否仍由指定源安装。"""
    if is_link_or_junction(dest):
        try:
            return _source_path_key(dest) == _source_path_key(source_dir)
        except OSError:
            return False
    if not dest.is_dir():
        return False

    meta = read_install_metadata(dest)
    if (
        meta is not None
        and meta.get("mode") == "copy"
        and (
            not isinstance(meta.get("installation_id"), str)
            or not isinstance(meta.get("content_identity"), str)
        )
    ):
        _log_target_operation_error(
            dest, "INSTALL_LEGACY_COPY_IDENTITY_MISSING",
        )
        return False
    return (
        meta is not None
        and copy_install_identity_matches(
            dest, source_dir, metadata=meta,
        )
    )


def _target_path_hash(dest: Path) -> str:
    return hashlib.sha256(
        _lexical_path_key(dest).encode(
            "utf-8", errors="surrogatepass",
        ),
    ).hexdigest()[:16]


def _log_target_operation_error(dest: Path, error_type: str) -> None:
    logger.warning(
        "install target operation failed target_hash=%s error_type=%s",
        _target_path_hash(dest),
        error_type,
    )



def _remove_owned_install_target(
    dest: Path, source_dir: Path | None = None,
) -> bool:
    """通过 InstallLedger 卸装：世代匹配才删；旧事务被重装 supersede。

    不再在用户生态目录写 removal-transaction / removing 旁路文件。
    """
    from xskill.ecosystems.install_ledger import remove_owned_dest

    return remove_owned_dest(
        dest,
        source_dir,
        is_link_or_junction=is_link_or_junction,
    )



def uninstall_skill_from_ecosystems(
    skill_name: str, *, home_root: Path, source_dir: Path | None = None,
    installations: list[dict] | None = None,
) -> list[Path]:
    """仅清理当前仍由 ``source_dir`` 拥有的所有生态安装目标。

    ``installations`` 是 search 台账里的实际安装快照。当前安装器目标仍会完整
    扫描，以兼容没有该字段的旧台账；快照中的目标必须命中 allowlist，不能把
    损坏或篡改的台账路径变成删除入口。
    """
    if not _valid_skill_name(skill_name) or source_dir is None:
        return []
    home_root = Path(home_root).resolve(strict=False)
    source_dir = Path(source_dir).resolve(strict=False)
    allowed = {
        _lexical_path_key(path): path
        for path in _all_install_targets(skill_name, home_root)
    }
    if isinstance(installations, list):
        for record in installations:
            if not isinstance(record, dict) or not isinstance(
                record.get("target"), str,
            ):
                continue
            key = _lexical_path_key(Path(record["target"]))
            if key not in allowed:
                logger.warning(
                    "ignored install target outside ecosystem roots "
                    "target_hash=%s",
                    hashlib.sha256(
                        _lexical_path_key(Path(record["target"])).encode(
                            "utf-8", errors="surrogatepass",
                        ),
                    ).hexdigest()[:16],
                )

    removed: list[Path] = []
    for dest in allowed.values():
        if _remove_owned_install_target(dest, source_dir):
            removed.append(dest)
    return removed
