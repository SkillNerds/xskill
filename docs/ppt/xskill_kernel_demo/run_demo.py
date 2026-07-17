"""本地运行：python run_demo.py"""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import yaml

from xskill_kernel_sdk import KernelRunRequest, KernelServices, TrajectoryRef


def load_class(class_path: str):
    module_name, class_name = class_path.split(":", 1)
    return getattr(importlib.import_module(module_name), class_name)


def main() -> None:
    root = Path(__file__).resolve().parent
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    selected = config["skill_generation"]
    kernel_class = load_class(selected["active"]["class_path"])
    kernel = kernel_class(selected["config"])

    with tempfile.TemporaryDirectory(prefix="xskill-kernel-demo-") as temp_dir:
        trajectory_path = Path(temp_dir) / "traj_demo.md"
        trajectory_path.write_text(
            "# Demo\n## User\n修复 Python 依赖冲突\n"
            "## Assistant\n检查依赖树并约束版本\n"
            "## User\n修复 Python 导入错误\n"
            "## Assistant\n检查模块路径\n",
            encoding="utf-8",
        )
        events: list[tuple[str, dict]] = []
        result = kernel.run(
            KernelRunRequest(
                run_id="run-demo-001",
                trajectories=[
                    TrajectoryRef(
                        trajectory_id="traj-demo",
                        path=trajectory_path,
                        user_id="demo-user",
                    )
                ],
                config_revision=selected["config_revision"],
            ),
            KernelServices(
                read_text=lambda path: path.read_text(encoding="utf-8"),
                emit_event=lambda name, data: events.append((name, dict(data))),
            ),
        )
        print("manifest:", kernel.manifest())
        print("events:", events)
        print("metrics:", dict(result.metrics))
        print("skills:", [artifact.name for artifact in result.artifacts])
        print("lineage:", dict(result.lineage))


if __name__ == "__main__":
    main()

