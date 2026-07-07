"""_sqlite_base.py — SQLite 存储基类（消除 ProfileStore / RecoStore 的样板重复）

提供连接管理 + schema 初始化钩子，子类只给 ``_SCHEMA`` 和 db_path。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


class _SqliteStore:
    """SQLite 存储基类：每操作开新连接（规模小），``_SCHEMA`` 由子类提供。

    子类需设类属性 ``_SCHEMA``（CREATE TABLE IF NOT EXISTS ... 脚本）。
    可重写 ``_migrate(conn)`` 做幂等加列迁移。
    """

    _SCHEMA: str = ""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        try:
            conn.executescript(self._SCHEMA)
            conn.commit()
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """子类重写：幂等加列 / 数据迁移。缺省 no-op。"""
