"""daemon.py — TeamClient 瘦客户端守护（SP1）

client 只干三件事：采集本地轨迹脱敏上传、持有 server 算出的 skill
working copy 并对齐 side、把本地手改推成 user-staging/<client_id> 分支。
零 LLM、零 git 写 main、零灰度判定。

_tick 一轮：
  collect_and_upload → sync → reconcile_skill_sides → push_user_edits → cleanup
"""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from xskill.skill.git import run_git
from xskill.ecosystems._history import InstallHistory
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
) -> str:
    """跟 server 握手注册，返回 server 分配（或续用）的 client_id。

    ``existing_client_id`` 用于重连保持身份：调用方（CLI）若发现本地 state
    里已有 client_id，传过来；server 按 (claimed_client_id, fingerprint,
    new uuid) 三级优先级判定（详见 ClientRegistry.register）。
    """
    resp = http.post("/api/v1/team/register", json={
        "token": token,
        "client_label": label,
        "hostname": hostname,
        "claimed_client_id": existing_client_id,
    })
    if resp.status_code != 200:
        raise RuntimeError(
            f"register failed: HTTP {resp.status_code} — {resp.text}"
        )
    return resp.json()["client_id"]


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
    ):
        self.state = state
        self.http = http
        # skill working copies 落标准 skill_dir（= ~/.xskill/skill/）——与
        # standalone 模式同一个位置，不另开 team_skills/。一台机器要么
        # standalone 要么 client，这个目录谁来管取决于模式。
        self.skill_dir = Path(skill_dir)
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

    # ── HTTP 鉴权头 ──────────────────────────────────────────────
    def _hdr(self, extra: dict | None = None) -> dict:
        h = {"X-Xskill-Token": self.state.join_token,
             "X-Xskill-Client": self.state.client_id}
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
            logger.warning("upload failed: HTTP %s — %s", resp.status_code, resp.text)
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

    # ── ③ reconcile ─────────────────────────────────────────────
    def reconcile_skill_sides(self, manifest: SyncResponse) -> None:
        """对 manifest 每个 slot：拉 bundle → 对齐 side → 装到本机生态。

        这是设计里约定的 reconcile_skill_sides——契约步骤 1（决定 target）
        就是读 manifest slot 的 side/sha；步骤 2/3/4 走共享
        reconcile_skill_side。
        """
        for slot in manifest.slots:
            repo_dir = self.skill_dir / slot.skill_name
            # 拉 bundle 落地/刷新本地 working copy
            r = self.http.get(f"/api/v1/team/skill/{slot.skill_name}/bundle",
                              headers=self._hdr())
            if r.status_code != 200:
                logger.warning("bundle fetch failed: %s HTTP %s",
                               slot.skill_name, r.status_code)
                continue
            apply_repo_bundle(r.content, repo_dir)
            # 步骤 1 = manifest 给的 (side, sha)；2/3/4 = 共享助手
            reconcile_skill_side(
                repo_dir=repo_dir, target_side=slot.side, target_sha=slot.sha,
                history=self.history, on_changed=self._install_to_ecosystems,
            )
        logger.info("reconciled %d skills", len(manifest.slots))

    def _install_to_ecosystems(self, repo_dir: Path) -> None:
        """把一个已 checkout 好的 skill working copy 装到本机所有生态。

        working tree 已是 server 指定 side 的内容，所以一律用 side='main'
        语义（= 链接 / 拷贝整个 working tree 目录）。

        注意 openclaw 走 copy 不是 symlink（openclaw 拒收 escape-root 的
        symlink，详见 docs/ecosystem/openclaw-install-fix.md）。其他生态保持
        symlink-first 三阶 fallback。
        """
        from xskill.ecosystems import (
            detect_known_ecosystems, install_to_claude_code,
            install_to_codex, install_to_opencode, install_to_ngagent,
            install_to_openclaw, install_to_cursor, install_to_trae,
        )
        installer = {
            "claude_code": install_to_claude_code,
            "codex": install_to_codex,
            "opencode": install_to_opencode,
            "ngagent": install_to_ngagent,
            "openclaw": install_to_openclaw,
            "cursor": install_to_cursor,
            "trae": install_to_trae,
        }
        for det in detect_known_ecosystems(home_root=self.home_root):
            fn = installer.get(det["ecosystem"])
            if fn is None:
                continue
            try:
                fn(repo_dir, target_root=self.home_root, side="main")
                logger.info("installed %s to %s", repo_dir.name, det["ecosystem"])
            except Exception:
                logger.warning("install %s to %s failed",
                               repo_dir.name, det["ecosystem"], exc_info=True)

    # ── ④ push 用户手改 ──────────────────────────────────────────
    def push_user_edits(self) -> int:
        """检测本地 working copy 的未吸收手改，推成 user-staging/<client_id>。

        返回推送成功的 skill 数。client 是愚蠢且可能恶意的——它推过去的
        只能进隔离分支，永远碰不到 main。

        openclaw 用户改的是 dest copy（``~/.agents/skills/<name>/``），不会
        自动到 working copy。每个 skill 先跑 reverse_sync_openclaw_dest 把
        dest 改灌回 working copy，下面 git status 才能看到。
        """
        from xskill.agents.user_edit_absorb_agent import reverse_sync_openclaw_dest

        pushed = 0
        for repo_dir in sorted(self.skill_dir.iterdir()):
            if not (repo_dir / ".git").is_dir():
                continue

            # openclaw 回流（dest → working copy）— 没装到 openclaw 时 no-op
            dest_dir = self.home_root / ".agents" / "skills" / repo_dir.name
            try:
                reverse_sync_openclaw_dest(dest_dir, repo_dir)
            except Exception:
                logger.warning("openclaw reverse_sync failed: %s",
                               repo_dir.name, exc_info=True)

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
                logger.warning("commit user edit failed: %s: %s",
                               repo_dir.name, combined)
                continue
            bundle = make_branch_bundle(repo_dir, "_useredit")
            resp = self.http.post(
                "/api/v1/team/push-edit",
                headers=self._hdr({"X-Xskill-Skill": repo_dir.name}),
                content=bundle,
            )
            if resp.status_code == 200:
                pushed += 1
                logger.info("pushed user edit: %s -> %s",
                            repo_dir.name, resp.json()["branch"])
            else:
                logger.warning("push-edit failed: %s HTTP %s",
                               repo_dir.name, resp.status_code)
        return pushed

    # ── ⑤ cleanup ───────────────────────────────────────────────
    def cleanup(self, manifest: SyncResponse) -> None:
        """删掉本地 working copy 里 manifest 已不包含的 skill。

        client 的 skill 集合完全由 server 算出的 manifest 决定——server 把
        某 skill 移出 100 → 下次 sync 后本地也删，不自留。
        """
        keep = {s.skill_name for s in manifest.slots}
        for repo_dir in sorted(self.skill_dir.iterdir()):
            if not repo_dir.is_dir() or repo_dir.name in keep:
                continue
            # 先摘生态里的安装（symlink），再删本地仓
            self._uninstall_from_ecosystems(repo_dir.name)
            shutil.rmtree(repo_dir, ignore_errors=True)
            logger.info("cleanup removed stale skill: %s", repo_dir.name)

    def _uninstall_from_ecosystems(self, skill_name: str) -> None:
        from xskill.ecosystems import _cc_skills_path, _agents_skills_path
        for root_fn in (_cc_skills_path, _agents_skills_path):
            dest = root_fn(self.home_root) / skill_name
            if dest.is_symlink():
                try:
                    dest.unlink()
                except OSError:
                    logger.warning("failed to unlink %s", dest, exc_info=True)

    # ── 守护循环 ─────────────────────────────────────────────────
    def _tick(self) -> None:
        try:
            self.collect_and_upload()
            manifest = self.sync()
            self.reconcile_skill_sides(manifest)
            self.push_user_edits()
            self.cleanup(manifest)
        except Exception:
            logger.exception("team client tick failed")

    def run_forever(self) -> None:
        """阻塞循环。先起 collector ingester，再每 poll_interval 跑一轮 _tick。"""
        self.collector.start_ingesters()
        logger.info("team client running: server=%s client_id=%s",
                    self.state.server_url, self.state.client_id)
        try:
            while not self._stop.is_set():
                self._tick()
                self._stop.wait(self.poll_interval)
        finally:
            self.collector.stop_ingesters()

    def stop(self) -> None:
        self._stop.set()
