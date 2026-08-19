"""xskill.tools — 一次性运维命令（``xskill tools <name>``）。

与日常命令（serve/connect/...）分开：这里的命令改动存量数据、要求手动
触发、且都带备份与回滚。当前仅 ``migrate-traj-name``（issue #234）。
"""

from xskill.tools.migrate_traj_name import (
    migrate_traj_names,
    rollback_traj_names,
)

__all__ = ["migrate_traj_names", "rollback_traj_names"]
