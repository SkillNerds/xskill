"""Real SkillOpt SDK bridge for XSkill.

This example intentionally imports and calls SkillOpt itself.  SkillOpt's
benchmark/model configuration stays private in this directory; XSkill only
provides the invocation workspace and managed Skill publication gateway.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import yaml

try:
    import skillopt
    from skillopt.config import flatten_config, load_config
    from skillopt.engine.trainer import ReflACTTrainer
    from skillopt.envs.spreadsheetbench.adapter import SpreadsheetBenchAdapter
except ImportError as exc:
    raise ImportError(
        "SkillOptKernel requires SkillOpt; install the provider package before "
        "selecting this kernel"
    ) from exc

from xskill.kernels import (
    BaseKernel,
    KernelContext,
    KernelManifest,
    KernelRunResult,
    SkillSubmission,
)


class SkillOptKernel(BaseKernel):
    """Run SkillOpt's SpreadsheetBench trainer and publish its best Skill."""

    manifest = KernelManifest(
        id="skillopt",
        name="SkillOpt",
        version=str(getattr(skillopt, "__version__", "unknown")),
        description=(
            "Real SkillOpt SpreadsheetBench SDK bridge (evaluation adapter; "
            "not yet an online trajectory-equivalent kernel)."
        ),
        triggers=("manual", "evaluation"),
        api_version=2,
    )

    def run(self, context: KernelContext) -> KernelRunResult:
        bridge = self._load_bridge_config(context.config_path)
        provider_config = Path(str(bridge["skillopt_config"])).expanduser()
        if not provider_config.is_absolute():
            provider_config = (context.config_path.parent / provider_config).resolve()
        overrides = bridge.get("overrides", [])
        if not isinstance(overrides, list) or not all(
            isinstance(item, str) for item in overrides
        ):
            raise ValueError("skillopt overrides must be a list of key=value strings")

        structured = load_config(str(provider_config), overrides=overrides)
        cfg = flatten_config(structured)
        out_root = context.workspace / "skillopt" / context.run_id
        cfg["out_root"] = str(out_root)
        cfg["env"] = "spreadsheetbench"

        signature = inspect.signature(SpreadsheetBenchAdapter.__init__)
        accepted = set(signature.parameters) - {"self"}
        adapter = SpreadsheetBenchAdapter(**{
            key: value for key, value in cfg.items() if key in accepted
        })
        summary = ReflACTTrainer(cfg, adapter).train()
        best_skill = out_root / "best_skill.md"
        if not best_skill.is_file():
            raise RuntimeError(f"SkillOpt did not produce {best_skill}")

        name = str(bridge.get("skill_name") or "spreadsheet-skillopt")
        description = str(bridge.get("description") or (
            "Spreadsheet manipulation strategy optimized by SkillOpt."
        ))
        generated = best_skill.read_text(encoding="utf-8").strip()
        skill_md = (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "metadata:\n"
            "  algorithm: skillopt\n"
            "---\n\n"
            f"{generated}\n"
        )

        base_commit = None
        try:
            base_commit = context.skills.get(name).main_commit_sha
        except KeyError:
            pass
        context.publisher.submit(SkillSubmission(
            name=name,
            skill_md=skill_md,
            message="publish SkillOpt best_skill.md",
            base_commit_sha=base_commit,
        ))
        safe_metrics = {
            key: summary.get(key)
            for key in (
                "baseline_selection", "best_selection", "selection_delta",
                "baseline_test", "best_test", "test_delta", "total_tokens",
                "wall_time_seconds",
            )
            if summary.get(key) is not None
        }
        safe_metrics["provider"] = "skillopt"
        safe_metrics["online_parity"] = False
        return KernelRunResult(
            submitted_skills=(name,),
            metrics=safe_metrics,
            notes=(
                "SkillOpt consumes the benchmark configured in its private "
                "config; this bridge is evaluation-only and does not claim "
                "online trajectory parity."
            ),
        )

    @staticmethod
    def _load_bridge_config(path: Path) -> dict:
        if not path.is_file():
            raise FileNotFoundError(
                f"copy config.yaml.example to {path} and edit provider paths"
            )
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("SkillOpt bridge config must be a mapping")
        if not loaded.get("skillopt_config"):
            raise ValueError("SkillOpt bridge config requires skillopt_config")
        return loaded


KERNEL_CLASS = SkillOptKernel
