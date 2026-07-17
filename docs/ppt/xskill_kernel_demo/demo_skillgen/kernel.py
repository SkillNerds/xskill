"""算法包与 XSkill 稳定接口之间的薄适配层。"""

from __future__ import annotations

from typing import Any, Mapping

from xskill_kernel_sdk import (
    KernelManifest,
    KernelRunRequest,
    KernelRunResult,
    KernelServices,
    SkillArtifact,
    SkillGenerationKernel,
)

from .algo_core import KeywordClusterer, MarkdownSkillWriter, SimpleSplitter


class DemoAtomTaskKernel(SkillGenerationKernel):
    def __init__(self, config: Mapping[str, Any]):
        self.config = dict(config)
        self.splitter = SimpleSplitter()
        self.clusterer = KeywordClusterer()
        self.writer = MarkdownSkillWriter()

    @classmethod
    def manifest(cls) -> KernelManifest:
        return KernelManifest(
            name="demo-atomtask",
            version="1.0.0",
            description="按用户回合拆分、关键词聚类并生成 Markdown Skill",
            capabilities=("batch", "lineage", "offline-eval"),
        )

    @classmethod
    def config_schema(cls) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "min_cluster_size": {"type": "integer", "minimum": 1},
            },
            "required": ["min_cluster_size"],
            "additionalProperties": False,
        }

    def validate_config(self) -> None:
        value = self.config.get("min_cluster_size")
        if not isinstance(value, int) or value < 1:
            raise ValueError("min_cluster_size 必须是大于等于 1 的整数")

    def run(
        self,
        request: KernelRunRequest,
        services: KernelServices,
    ) -> KernelRunResult:
        self.validate_config()
        atoms = []
        for trajectory in request.trajectories:
            text = services.read_text(trajectory.path)
            atoms.extend(self.splitter.split(trajectory.trajectory_id, text))
        services.emit_event("atoms.ready", {"count": len(atoms)})

        clusters = self.clusterer.cluster(
            atoms,
            min_cluster_size=self.config["min_cluster_size"],
        )
        artifacts = [
            SkillArtifact(
                name=name,
                content=self.writer.write(name, members),
                source_trajectory_ids=tuple(
                    sorted({member.trajectory_id for member in members})
                ),
                metadata={"atom_count": len(members)},
            )
            for name, members in clusters.items()
        ]
        return KernelRunResult(
            artifacts=artifacts,
            metrics={
                "atom_count": float(len(atoms)),
                "skill_count": float(len(artifacts)),
            },
            lineage={
                artifact.name: list(artifact.source_trajectory_ids)
                for artifact in artifacts
            },
        )

