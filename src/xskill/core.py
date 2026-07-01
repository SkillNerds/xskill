"""
xskill.py — XSkill 顶层门面
═══════════════════════════════════════════════════════
唯一对外入口。持 config + registry + skill_repo，
提供 search / serve / score_trajectory_ux 三个动作方法。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from xskill.config import XSkillConfig, load_config
from xskill.container import XSkillContainer
from xskill.pipeline.trajectory import Trajectory
from xskill.types import SkillHit, TrajectoryHit, UxScoreResult


class XSkill:
    """xskill 顶层门面。

    用法：
        from xskill import XSkill
        xskill = XSkill()                       # 默认 ~/.xskill/config.yaml
        xskill = XSkill(config_path=Path(...))  # 显式

        # 检索
        hits = xskill.search_skills("django form")
        hits = xskill.search_trajectories("query")

        # daemon
        xskill.serve(host="0.0.0.0", port=8000)

        # 主动 UX 打分（维护性，watcher 会自动跑）
        xskill.score_trajectory_ux(traj)

        # 子系统访问
        xskill.registry.list()
        xskill.skill_repo["fix-foo"]
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        config: XSkillConfig | Mapping[str, Any] | None = None,
        container: XSkillContainer | None = None,
    ):
        if container is not None and (config_path is not None or config is not None):
            raise ValueError("container cannot be combined with config_path or config")
        if config_path is not None and config is not None:
            raise ValueError("config_path and config cannot both be provided")
        if container is None:
            if config is None:
                config_object = load_config(config_path)
            elif isinstance(config, XSkillConfig):
                config_object = config
            else:
                config_object = XSkillConfig.from_dict(config)
            container = XSkillContainer()
            container.config.override(config_object)
        self.container = container
        self.config = self.container.config()
        self.registry = self.container.registry()
        self.skill_repo = self.container.skill_repo()

    # ─── lazy LLM / embed clients ──────────────────────────────
    @property
    def llm(self):
        return self.container.llm_client()

    @property
    def embed(self):
        return self.container.embed_client()

    # ─── 检索（跨所有 registry）─────────────────────────────────
    def search_trajectories(self, query: str, top_k: int = 5,
                            min_similarity: float = 0.0) -> list[TrajectoryHit]:
        """跨所有注册目录搜索轨迹。"""
        from xskill.utils.search import search_all
        results = search_all(
            query, top_k=top_k,
            min_similarity=min_similarity,
            success_filter="all",
            config=self.config,
        )
        out: list[TrajectoryHit] = []
        for r in results:
            md = r.get("md_path") or r.get("traj_path")
            if not md:
                continue
            try:
                traj = Trajectory.load(md, registry=self.registry)
            except FileNotFoundError:
                continue
            out.append(TrajectoryHit(trajectory=traj,
                                     similarity=float(r.get("similarity", 0.0))))
        return out

    def search_skills(self, query: str, top_k: int = 5) -> list[SkillHit]:
        """跨 skill_repo 搜索 skill。"""
        from xskill.skill.repo import search_skill_index
        items = search_skill_index(
            skill_dir=self.skill_repo.root,
            query=query,
            embed_client=self.embed,
            top_k=top_k,
        )
        out: list[SkillHit] = []
        for item in items:
            name = item.get("skill_name")
            if not name:
                continue
            skill = self.skill_repo.get(name)
            if skill is None:
                continue
            out.append(SkillHit(
                skill=skill,
                similarity=float(item.get("similarity", 0.0)),
            ))
        return out[:top_k]

    # ─── UX 打分（主动；v2 atom 粒度）──────────────────────────
    def score_trajectory_ux(self, traj: Trajectory) -> UxScoreResult:
        """主动给一条 traj 的所有 atom 补 UX 分（幂等：已落盘的跳过）。

        v2: 打分对象是 AtomTask，不是整条 traj。前置条件——该 traj 已被
        watcher 走完 split 阶段（atoms 落在 ``<traj_dir>/<traj_id>/tasks/``）。
        没拆过的 traj 调本方法 ``scored=0``，因为 store 里没东西。

        watcher 自动跑；本方法用于 watcher 漏打 / 手动重打。
        """
        from xskill.pipeline.atom import score_and_record_atoms
        from xskill.pipeline.atom import AtomTaskStore
        from xskill.canary import CanaryConfig
        from xskill.pipeline.trajectory import parse_traj_header

        md = traj.md_text
        header = parse_traj_header(md)
        if not header or not header.get("skill") or not header.get("side"):
            return UxScoreResult(
                scored=False, score=None,
                reasons="trajectory missing xskill header (skill/side)",
                decision={"action": "no_header"},
            )
        skill_name = header["skill"]
        skill = self.skill_repo.get(skill_name)
        if skill is None:
            return UxScoreResult(
                scored=False, score=None,
                reasons=f"skill not found in repo: {skill_name}",
                decision={"action": "skill_missing"},
            )
        canary_cfg = CanaryConfig.from_dict(self.config.get("canary", {}))
        store = AtomTaskStore(root=traj.path.parent)
        d = score_and_record_atoms(
            llm=self.llm,
            skill_dir=skill.path,
            store=store,
            traj_id=traj.path.stem,
            skill_name=skill_name,
            side=header["side"],
            commit_sha=header.get("sha", ""),
            canary_config=canary_cfg,
        )
        # v2 返回 {scored: int, skipped: int, decision}；UxScoreResult.scored 是 bool
        return UxScoreResult(
            scored=bool(d.get("scored", 0) > 0),
            score=None,  # 多 atom 没有单一分数；调用方需读 .ux_scores.jsonl 细看
            reasons=f"scored={d.get('scored')}, skipped={d.get('skipped')}",
            decision=d.get("decision", {}),
        )

    # ─── daemon ────────────────────────────────────────────────
    def serve(self, host: str = "0.0.0.0", port: int = 8000,
              *, home_root: Path | str | None = None,
              server_mode: bool = False) -> None:
        """启动 FastAPI server（含 watcher 后台线程）。阻塞。

        Args:
            home_root: 可选，debug 模式下指向自选目录（只扫描该目录下的
                       ``.claude/``）。生产环境留 None 用真实 ``$HOME``。
            server_mode: True = team server 模式（收 client 上传、跑全部
                       agent、提供 /api/v1/team/* 同步接口）。
        """
        import uvicorn
        from xskill.api import create_app
        app = create_app(home_root=home_root, team_server=server_mode)
        if server_mode:
            from xskill.team.server.state import ensure_join_token
            from xskill.config import get_team_server_state_path
            token = ensure_join_token(get_team_server_state_path())
            print(f"xskill team server at http://{host}:{port}/")
            print(f"  clients join with:")
            print(f"    xskill connect <THIS_HOST>:{port} --token {token}")
        elif home_root:
            print(f"xskill serve at http://{host}:{port}/  [debug home: {home_root}]")
        else:
            print(f"xskill serve at http://{host}:{port}/")
            print(f"  standalone mode — skills 留在本机。team 共享用:"
                  f" xskill serve --server")
        uvicorn.run(app, host=host, port=port)

    def __repr__(self) -> str:
        return (f"XSkill(skill_repo_root={self.skill_repo.root}, "
                f"registry_dirs={len(self.registry.list())}, "
                f"skills={len(self.skill_repo)})")
