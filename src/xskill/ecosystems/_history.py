"""
ecosystems/_history.py -- daemon 自己装到 ~/.claude/skills/ 的 side 历史
========================================================================

灰度链路里 CC 直接读磁盘上的 ``SKILL.md`` 一份文件——CC 既不知道也不关心
xskill 是不是在做 A/B。要做"半数 CC session 看到 main、半数看到 staging"，
daemon 唯一能动的就是周期性地翻磁盘文件，然后**记住自己什么时候装了哪边**。

这个文件是那份"记账"。append-only jsonl，每行一条 install 记录：

  {"t": 1700000000.123, "skill": "list-py-files", "side": "main", "sha": "abc1234"}

CC session 桥进 xskill 这边时：
  - 读 JSONL 第一条事件 → session_start_t
  - 用 lookup(session_start_t) 找出"那一刻盘上装的是哪 side"
  - 据此给桥过来的 traj 写 xskill header

整套思路在 daemon 单边自洽：不挂 FUSE、不动 CC 插件、不靠模型听话；CC 永远
对此无感，只是它每次 session 启动读盘那一刻**真**读到了 daemon 写下的内容
（无论 main 还是 staging），而 daemon 知道那一刻自己写了什么。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from bisect import bisect_right
from collections.abc import Iterator, Sequence, Set
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from operator import itemgetter
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("xskill.ecosystems.install_history")


def fsync_directory(directory: Path) -> None:
    """在 POSIX 上持久化 rename/unlink 元数据；Windows 不支持目录 fsync。"""
    if os.name == "nt":
        return
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


class InstallHistoryCorruptError(RuntimeError):
    """安装历史无法安全解析；调用方必须停止基于它切换目标。"""


class InstallHistoryAppendUncertainError(OSError):
    """追加已开始但持久化结果不确定；物理目标必须保留并等待恢复。"""


class _InstallHistoryCacheReset(RuntimeError):
    """增量读取前文件已 truncate；调用方必须从 offset 0 重建。"""


class InstallDecisionCancelled(RuntimeError):
    """锁内前置条件已变化，当前决策不消费、也不追加成功记录。"""

    def __init__(self, message: str, *, target_changed: bool = False):
        super().__init__(message)
        self.target_changed = target_changed


@dataclass(frozen=True)
class InstallHistoryFileSignature:
    """history 文件一次稳定读取对应的跨平台身份与游标。"""

    device: Optional[int]
    inode: Optional[int]
    size: int
    modified_time_ns: int
    changed_time_ns: int
    cursor: int


@dataclass
class _TimedInstallRun:
    """按 (t, append-position) 排序的不可变 LSM run。"""

    entries: list[tuple[float, int, dict]]
    keys: list[tuple[float, int]]
    position_tree_base: int
    minimum_position_tree: list[int]

    @classmethod
    def from_entries(
        cls,
        entries: list[tuple[float, int, dict]],
    ) -> "_TimedInstallRun":
        keys = [
            (installed_at, position)
            for installed_at, position, _record in entries
        ]
        tree_base = 1
        while tree_base < len(entries):
            tree_base *= 2
        minimum_positions = [2**63 - 1] * (tree_base * 2)
        for entry_index, (_installed_at, position, _record) in enumerate(
            entries
        ):
            minimum_positions[tree_base + entry_index] = position
        for node in range(tree_base - 1, 0, -1):
            minimum_positions[node] = min(
                minimum_positions[node * 2],
                minimum_positions[node * 2 + 1],
            )
        return cls(
            entries=entries,
            keys=keys,
            position_tree_base=tree_base,
            minimum_position_tree=minimum_positions,
        )

    def rightmost_position_at_most(
        self,
        *,
        last_index: int,
        record_limit: int,
    ) -> int | None:
        """在时间前缀内 O(log N) 找到 append position 合法的最右记录。"""
        def search(node: int, node_start: int, node_end: int) -> int | None:
            if (
                node_start > last_index
                or self.minimum_position_tree[node] > record_limit
            ):
                return None
            if node_end - node_start == 1:
                return node_start if node_start < len(self.entries) else None
            midpoint = (node_start + node_end) // 2
            right_result = search(
                node * 2 + 1,
                midpoint,
                node_end,
            )
            if right_result is not None:
                return right_result
            return search(node * 2, node_start, midpoint)

        return search(1, 0, self.position_tree_base)


@dataclass
class _InstallHistoryIndexState:
    """只追加的进程内索引；历史快照用 record_limit 隔离后续追加。"""

    records: list[dict] = field(default_factory=list)
    installs: dict[
        tuple[str, str], list[tuple[int, dict]]
    ] = field(default_factory=dict)
    install_positions: dict[
        tuple[str, str], list[int]
    ] = field(default_factory=dict)
    timed_installs: dict[
        tuple[str, str], list[tuple[float, int, dict]]
    ] = field(default_factory=dict)
    timed_install_keys: dict[
        tuple[str, str], list[tuple[float, int]]
    ] = field(default_factory=dict)
    timed_install_runs: dict[
        tuple[str, str], list[_TimedInstallRun | None]
    ] = field(default_factory=dict)
    timed_merge_input_count: int = 0
    consumed: dict[
        tuple[str, str], dict[str, int]
    ] = field(default_factory=dict)
    assignments: dict[
        tuple[str, str, str], list[tuple[int, dict]]
    ] = field(default_factory=dict)
    decision_sequences: dict[
        tuple[str, str, str], list[tuple[int, int]]
    ] = field(default_factory=dict)
    decision_sequence_positions: dict[
        tuple[str, str, str], list[int]
    ] = field(default_factory=dict)
    record_positions: dict[str, int] = field(default_factory=dict)


class _RecordPrefix(Sequence[dict]):
    """共享只追加列表的不可变前缀视图。"""

    def __init__(self, records: list[dict], limit: int):
        self._records = records
        self._limit = limit

    def __len__(self) -> int:
        return self._limit

    def __iter__(self) -> Iterator[dict]:
        for position in range(self._limit):
            yield self._records[position]

    def __getitem__(self, position):
        if isinstance(position, slice):
            return tuple(self._records[:self._limit])[position]
        normalized_position = (
            position if position >= 0 else self._limit + position
        )
        if normalized_position < 0 or normalized_position >= self._limit:
            raise IndexError(position)
        return self._records[normalized_position]


class _RecordIdPrefix(Set[str]):
    """record_id→追加位置视图；后续追加不会进入旧快照。"""

    def __init__(self, positions: dict[str, int], limit: int):
        self._positions = positions
        self._limit = limit

    def __contains__(self, record_id: object) -> bool:
        if not isinstance(record_id, str):
            return False
        position = self._positions.get(record_id)
        return position is not None and position <= self._limit

    def __iter__(self) -> Iterator[str]:
        for record_id, position in self._positions.items():
            if position <= self._limit:
                yield record_id

    def __len__(self) -> int:
        return sum(
            1
            for position in self._positions.values()
            if position <= self._limit
        )


@dataclass(frozen=True)
class InstallHistoryIndex:
    """一次文件读取构建的批内只读索引。"""

    _state: _InstallHistoryIndexState
    _record_limit: int
    max_append_sequence: int

    @property
    def records(self) -> Sequence[dict]:
        return _RecordPrefix(self._state.records, self._record_limit)

    @property
    def record_ids(self) -> Set[str]:
        return _RecordIdPrefix(
            self._state.record_positions,
            self._record_limit,
        )

    def latest(self, skill: str, target: str) -> Optional[dict]:
        """按追加顺序返回当前安装状态，不依赖可能回拨的墙钟。"""
        key = (skill, target)
        positions = self._state.install_positions.get(key, ())
        insertion_position = bisect_right(positions, self._record_limit)
        if insertion_position == 0:
            return None
        return self._state.installs[key][insertion_position - 1][1]

    def lookup_at(
        self,
        t: float,
        *,
        skill: str,
        target: str,
    ) -> Optional[dict]:
        """仅用墙钟把历史 session 归因到当时已经生效的最后一次安装。"""
        candidate: tuple[float, int, dict] | None = None
        for timed_run in self._state.timed_install_runs.get(
            (skill, target),
            (),
        ):
            if timed_run is None:
                continue
            insertion_position = bisect_right(
                timed_run.keys,
                (float(t), self._record_limit),
            )
            entry_position = timed_run.rightmost_position_at_most(
                last_index=insertion_position - 1,
                record_limit=self._record_limit,
            )
            if entry_position is None:
                continue
            entry = timed_run.entries[entry_position]
            if candidate is None or entry[:2] > candidate[:2]:
                candidate = entry
        return candidate[2] if candidate is not None else None

    def consumed(self, skill: str, target: str) -> Set[str]:
        return _RecordIdPrefix(
            self._state.consumed.get((skill, target), {}),
            self._record_limit,
        )

    def session_assignment(
        self,
        skill: str,
        target: str,
        session_id: str,
    ) -> Optional[dict]:
        for position, record in reversed(
            self._state.assignments.get(
                (skill, target, session_id),
                (),
            )
        ):
            if position <= self._record_limit:
                return record
        return None

    def latest_decision_sequence(
        self,
        skill: str,
        target: str,
        decision_kind: str,
    ) -> Optional[int]:
        key = (skill, target, decision_kind)
        positions = self._state.decision_sequence_positions.get(key, ())
        insertion_position = bisect_right(
            positions,
            self._record_limit,
        )
        if insertion_position == 0:
            return None
        return self._state.decision_sequences[key][
            insertion_position - 1
        ][1]


@dataclass(frozen=True)
class InstallHistorySnapshot:
    """索引及其实际读取文件版本；调用方只能缓存这份签名。"""

    index: InstallHistoryIndex
    signature: Optional[InstallHistoryFileSignature]


@dataclass(frozen=True)
class InstallDecisionContext:
    """决策锁内的一致快照。"""

    index: InstallHistoryIndex
    latest: Optional[dict]
    recovery: Optional[dict]
    current_generation: Optional[str]


@dataclass
class InstallPlan:
    """目标变更计划；物理变更与历史追加由事务统一编排。"""

    side: Optional[str] = None
    sha: str = ""
    generation: str = ""
    records: list[dict] = field(default_factory=list)
    install_decision_ids: tuple[str, ...] = ()
    apply: Optional[Callable[[], None]] = None
    rollback: Optional[Callable[[], None]] = None
    value: Any = None


@dataclass(frozen=True)
class InstallTransactionResult:
    """事务结果；stale 表示 generation 或单调序号拒绝了旧决策。"""

    current: Optional[dict]
    records: tuple[dict, ...] = ()
    pending_decision_ids: tuple[str, ...] = ()
    stale: bool = False
    value: Any = None


@dataclass(frozen=True)
class InstallTransactionRequest:
    """多目标批事务中的一个独立目标请求。"""

    skill: str
    target: str
    decision_ids: tuple[str, ...]
    operation: Callable[
        [InstallDecisionContext, tuple[str, ...]],
        Optional[InstallPlan],
    ]
    decision_kind: Optional[str] = None
    decision_sequence: Optional[int] = None
    expected_generation: Optional[str] = None
    generation_reader: Optional[Callable[[], str]] = None
    invoke_when_consumed: bool = False
    installed_state_reader: Optional[
        Callable[[], tuple[str, str, str]]
    ] = None
    recovery_operation: Optional[Callable[[dict], None]] = None


@dataclass
class _PreparedTransaction:
    request: InstallTransactionRequest
    context: InstallDecisionContext
    pending_decision_ids: tuple[str, ...]
    plan: InstallPlan
    records: list[dict]
    transaction_id: str


@contextmanager
def _exclusive_file_lock(lock_path: Path):
    """跨线程、跨进程独占一个很小的安装决策临界区。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_path_lock(lock_path: Path):
    """供同一生态的多个短命进程串行化一次性扫描。"""
    with _exclusive_file_lock(lock_path):
        yield


class InstallHistory:
    """跨线程/进程追加，并为每批决策只解析一次 JSONL。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._read_count = 0
        self._parsed_line_count = 0
        self._index_cache: InstallHistoryIndex | None = None
        self._index_cache_signature: InstallHistoryFileSignature | None = None
        self._index_cache_boundary_digest: bytes | None = None
        self._index_cache_physical_line_count = 0
        self._last_read_prefix_digest = b""
        self._last_read_boundary_digest = b""
        self._last_read_physical_line_count = 0
        self._index_cache_initialized = False
        self._sequence_validated = False
        self._sequence_floor = 0

    @property
    def _history_lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    @property
    def _sequence_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.sequence")

    @property
    def read_count(self) -> int:
        """本实例实际读取并解析 history 文件的次数（复杂度回归用）。"""
        return self._read_count

    @property
    def parsed_line_count(self) -> int:
        """本实例为索引实际 JSON 解析的非空行数。"""
        return self._parsed_line_count

    def _decision_lock_path(self, skill: str, target: str) -> Path:
        identity = hashlib.sha256(
            f"{skill}\0{target}".encode("utf-8")
        ).hexdigest()
        return self.path.parent / ".install_decisions" / f"{identity}.lock"

    def _recovery_path(self, skill: str, target: str) -> Path:
        identity = hashlib.sha256(
            f"{skill}\0{target}".encode("utf-8")
        ).hexdigest()
        return self.path.parent / ".install_transactions" / f"{identity}.json"

    def has_pending_recovery(self, skill: str, target: str) -> bool:
        """目标是否有尚未确认追加完成的事务日志。"""
        return self._recovery_path(skill, target).is_file()

    @staticmethod
    def _file_signature(
        stat_result: os.stat_result,
        *,
        cursor: int,
    ) -> InstallHistoryFileSignature:
        inode = int(stat_result.st_ino) if stat_result.st_ino else None
        device = int(stat_result.st_dev) if stat_result.st_dev else None
        return InstallHistoryFileSignature(
            device=device,
            inode=inode,
            size=int(stat_result.st_size),
            modified_time_ns=int(stat_result.st_mtime_ns),
            changed_time_ns=int(stat_result.st_ctime_ns),
            cursor=cursor,
        )

    def current_signature(self) -> Optional[InstallHistoryFileSignature]:
        """O(1) 获取当前路径版本；无文件时返回 ``None``。"""
        try:
            with self.path.open("rb") as history_file:
                stat_result = os.fstat(history_file.fileno())
        except FileNotFoundError:
            return None
        return self._file_signature(
            stat_result,
            cursor=int(stat_result.st_size),
        )

    def _read_stable_bytes_locked(
        self,
        *,
        offset: int = 0,
    ) -> tuple[bytes, Optional[InstallHistoryFileSignature]]:
        """读取实际文件描述符并有界拒绝读期间的原地改写。"""
        for _attempt in range(3):
            boundary_start = max(0, offset - 64)
            try:
                with self.path.open("rb") as history_file:
                    before_stat = os.fstat(history_file.fileno())
                    if before_stat.st_size < offset:
                        raise _InstallHistoryCacheReset
                    history_file.seek(boundary_start)
                    read_bytes = history_file.read()
                    cursor = history_file.tell()
                    after_stat = os.fstat(history_file.fileno())
            except FileNotFoundError:
                self._read_count += 1
                self._last_read_prefix_digest = hashlib.sha256(b"").digest()
                self._last_read_boundary_digest = hashlib.sha256(b"").digest()
                self._last_read_physical_line_count = 0
                return b"", None
            except OSError as exc:
                self._read_count += 1
                logger.error(
                    "install history read failed path=%s error_type=%s",
                    self.path,
                    type(exc).__name__,
                )
                raise InstallHistoryCorruptError(
                    f"install history cannot be read: {self.path}"
                ) from exc
            self._read_count += 1
            before_signature = self._file_signature(
                before_stat,
                cursor=int(before_stat.st_size),
            )
            after_signature = self._file_signature(
                after_stat,
                cursor=cursor,
            )
            prefix_length = offset - boundary_start
            prefix_bytes = read_bytes[:prefix_length]
            raw_bytes = read_bytes[prefix_length:]
            if (
                before_signature == after_signature
                and cursor == boundary_start + len(read_bytes)
            ):
                self._last_read_prefix_digest = hashlib.sha256(
                    prefix_bytes
                ).digest()
                self._last_read_boundary_digest = hashlib.sha256(
                    read_bytes[-64:]
                ).digest()
                self._last_read_physical_line_count = (
                    raw_bytes.count(b"\n")
                    + int(bool(raw_bytes) and not raw_bytes.endswith(b"\n"))
                )
                return raw_bytes, after_signature
        logger.error(
            "install history changed repeatedly while reading path=%s",
            self.path,
        )
        raise InstallHistoryCorruptError(
            f"install history changed while reading: {self.path}"
        )

    def _parse_record_bytes(
        self,
        raw_bytes: bytes,
        *,
        previous_sequence: int,
        starting_line_number: int,
    ) -> list[dict]:
        try:
            lines = raw_bytes.decode("utf-8", errors="strict").splitlines()
        except UnicodeError as exc:
            logger.error(
                "install history read failed path=%s error_type=%s",
                self.path,
                type(exc).__name__,
            )
            raise InstallHistoryCorruptError(
                f"install history cannot be decoded: {self.path}"
            ) from exc
        records: list[dict] = []
        for line_number, raw_line in enumerate(
            lines,
            start=starting_line_number,
        ):
            line = raw_line.strip()
            if not line:
                continue
            self._parsed_line_count += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.error(
                    "invalid install history JSON path=%s line=%d error_type=%s",
                    self.path,
                    line_number,
                    type(exc).__name__,
                )
                raise InstallHistoryCorruptError(
                    f"invalid install history JSON at line {line_number}"
                ) from exc
            if not isinstance(record, dict):
                logger.error(
                    "invalid install history schema path=%s line=%d",
                    self.path,
                    line_number,
                )
                raise InstallHistoryCorruptError(
                    f"install history line {line_number} is not an object"
                )
            append_sequence = record.get("append_sequence", line_number)
            if (
                not isinstance(append_sequence, int)
                or append_sequence <= previous_sequence
            ):
                logger.error(
                    "non-monotonic install history sequence path=%s line=%d",
                    self.path,
                    line_number,
                )
                raise InstallHistoryCorruptError(
                    f"non-monotonic install history at line {line_number}"
                )
            record["append_sequence"] = append_sequence
            previous_sequence = append_sequence
            records.append(record)
        return records

    def _parse_records_snapshot_locked(
        self,
    ) -> tuple[list[dict], Optional[InstallHistoryFileSignature]]:
        raw_bytes, signature = self._read_stable_bytes_locked()
        return (
            self._parse_record_bytes(
                raw_bytes,
                previous_sequence=0,
                starting_line_number=1,
            ),
            signature,
        )

    def _parse_records_locked(self) -> list[dict]:
        records, _signature = self._parse_records_snapshot_locked()
        return records

    @staticmethod
    def _extend_index_state(
        state: _InstallHistoryIndexState,
        records: Iterable[dict],
        *,
        maintain_timed_order: bool = True,
    ) -> None:
        """把新增记录一次加入共享索引，不复制既有 history。"""
        pending_timed_entries: dict[
            tuple[str, str], list[tuple[float, int, dict]]
        ] = {}
        for source_record in records:
            record = dict(source_record)
            state.records.append(record)
            position = len(state.records)
            skill = record.get("skill")
            target = record.get("target")
            record_id = record.get("record_id")
            if isinstance(record_id, str):
                state.record_positions.setdefault(record_id, position)
            if not isinstance(skill, str) or not isinstance(target, str):
                continue
            key = (skill, target)
            if record.get("action", "install") == "install":
                state.installs.setdefault(key, []).append((position, record))
                state.install_positions.setdefault(key, []).append(position)
                installed_at = record.get("t")
                if isinstance(installed_at, (int, float)):
                    timed_entry = (float(installed_at), position, record)
                    timed_entries = state.timed_installs.setdefault(key, [])
                    if not maintain_timed_order:
                        timed_entries.append(timed_entry)
                    else:
                        pending_timed_entries.setdefault(
                            key,
                            [],
                        ).append(timed_entry)
            for decision_id in record.get("decision_ids", ()):
                if isinstance(decision_id, str):
                    state.consumed.setdefault(key, {}).setdefault(
                        decision_id,
                        position,
                    )
            session_id = record.get("session_id")
            if record.get("action") == "session_assignment" and isinstance(
                session_id, str
            ):
                state.assignments.setdefault(
                    (skill, target, session_id),
                    [],
                ).append((position, record))
            decision_kind = record.get("decision_kind")
            decision_sequence = record.get("decision_sequence")
            if isinstance(decision_kind, str) and isinstance(
                decision_sequence, int
            ):
                sequence_entries = state.decision_sequences.setdefault(
                    (skill, target, decision_kind),
                    [],
                )
                state.decision_sequence_positions.setdefault(
                    (skill, target, decision_kind),
                    [],
                ).append(position)
                running_maximum = (
                    max(sequence_entries[-1][1], decision_sequence)
                    if sequence_entries
                    else decision_sequence
                )
                sequence_entries.append((position, running_maximum))
        if maintain_timed_order:
            for key, timed_entries in pending_timed_entries.items():
                timed_entries.sort(key=itemgetter(0, 1))
                cls_run = _TimedInstallRun.from_entries(timed_entries)
                InstallHistory._add_timed_install_run(
                    state,
                    key,
                    cls_run,
                )

    @staticmethod
    def _merge_timed_install_runs(
        left: _TimedInstallRun,
        right: _TimedInstallRun,
    ) -> _TimedInstallRun:
        merged_entries: list[tuple[float, int, dict]] = []
        left_position = 0
        right_position = 0
        while (
            left_position < len(left.entries)
            and right_position < len(right.entries)
        ):
            if (
                left.entries[left_position][:2]
                <= right.entries[right_position][:2]
            ):
                merged_entries.append(left.entries[left_position])
                left_position += 1
            else:
                merged_entries.append(right.entries[right_position])
                right_position += 1
        merged_entries.extend(left.entries[left_position:])
        merged_entries.extend(right.entries[right_position:])
        return _TimedInstallRun.from_entries(merged_entries)

    @classmethod
    def _add_timed_install_run(
        cls,
        state: _InstallHistoryIndexState,
        key: tuple[str, str],
        timed_run: _TimedInstallRun,
    ) -> None:
        levels = state.timed_install_runs.setdefault(key, [])
        level = 0
        while level < len(levels) and levels[level] is not None:
            existing_run = levels[level]
            if existing_run is None:
                raise RuntimeError("timed run level changed unexpectedly")
            state.timed_merge_input_count += (
                len(existing_run.entries) + len(timed_run.entries)
            )
            timed_run = cls._merge_timed_install_runs(
                existing_run,
                timed_run,
            )
            levels[level] = None
            level += 1
        if level == len(levels):
            levels.append(timed_run)
        else:
            levels[level] = timed_run

    @classmethod
    def _build_index(cls, records: Iterable[dict]) -> InstallHistoryIndex:
        """从全量记录构建新索引；只在首次读取、truncate 或 replace 时调用。"""
        state = _InstallHistoryIndexState()
        cls._extend_index_state(
            state,
            records,
            maintain_timed_order=False,
        )
        for key, timed_entries in state.timed_installs.items():
            timed_entries.sort(key=itemgetter(0, 1))
            cls._add_timed_install_run(
                state,
                key,
                _TimedInstallRun.from_entries(timed_entries),
            )
        state.timed_installs.clear()
        state.timed_install_keys.clear()
        max_append_sequence = max(
            (
                record.get("append_sequence", position)
                for position, record in enumerate(
                    state.records,
                    start=1,
                )
            ),
            default=0,
        )
        return InstallHistoryIndex(
            _state=state,
            _record_limit=len(state.records),
            max_append_sequence=max_append_sequence,
        )

    def index(self) -> InstallHistoryIndex:
        """读取一次并构建 current/decision/session 三类索引。"""
        return self.snapshot().index

    def snapshot(self) -> InstallHistorySnapshot:
        """锁内读取索引及其文件版本，避免调用方事后 stat 推进游标。"""
        with self._lock, _exclusive_file_lock(self._history_lock_path):
            current_signature = self.current_signature()
            if (
                self._index_cache_initialized
                and current_signature == self._index_cache_signature
            ):
                if self._index_cache is None:
                    raise RuntimeError("history index cache is uninitialized")
                self._sequence_floor = max(
                    self._sequence_floor,
                    self._index_cache.max_append_sequence,
                )
                self._sequence_validated = True
                return InstallHistorySnapshot(
                    index=self._index_cache,
                    signature=self._index_cache_signature,
                )
            cached_signature = self._index_cache_signature
            can_extend_cache = (
                self._index_cache is not None
                and cached_signature is not None
                and current_signature is not None
                and cached_signature.device is not None
                and cached_signature.inode is not None
                and (
                    cached_signature.device,
                    cached_signature.inode,
                ) == (
                    current_signature.device,
                    current_signature.inode,
                )
                and current_signature.size > cached_signature.cursor
            )
            if can_extend_cache:
                read_identity_matches = False
                try:
                    raw_bytes, signature = self._read_stable_bytes_locked(
                        offset=cached_signature.cursor,
                    )
                except _InstallHistoryCacheReset:
                    records, signature = (
                        self._parse_records_snapshot_locked()
                    )
                    index = self._build_index(records)
                    physical_line_count = (
                        self._last_read_physical_line_count
                    )
                    can_extend_cache = False
                else:
                    read_identity_matches = (
                        signature is not None
                        and (
                            signature.device,
                            signature.inode,
                        ) == (
                            cached_signature.device,
                            cached_signature.inode,
                        )
                        and self._last_read_prefix_digest
                        == self._index_cache_boundary_digest
                    )
                if can_extend_cache and read_identity_matches:
                    if raw_bytes and not raw_bytes.endswith(b"\n"):
                        raise InstallHistoryCorruptError(
                            f"incomplete install history tail: {self.path}"
                        )
                    records = self._parse_record_bytes(
                        raw_bytes,
                        previous_sequence=(
                            self._index_cache.max_append_sequence
                        ),
                        starting_line_number=(
                            self._index_cache_physical_line_count + 1
                        ),
                    )
                    self._extend_index_state(
                        self._index_cache._state,
                        records,
                    )
                    max_append_sequence = max(
                        (
                            record.get("append_sequence", 0)
                            for record in records
                        ),
                        default=self._index_cache.max_append_sequence,
                    )
                    index = InstallHistoryIndex(
                        _state=self._index_cache._state,
                        _record_limit=len(
                            self._index_cache._state.records
                        ),
                        max_append_sequence=max_append_sequence,
                    )
                    physical_line_count = (
                        self._index_cache_physical_line_count
                        + self._last_read_physical_line_count
                    )
                elif can_extend_cache:
                    records, signature = (
                        self._parse_records_snapshot_locked()
                    )
                    index = self._build_index(records)
                    physical_line_count = (
                        self._last_read_physical_line_count
                    )
            else:
                records, signature = (
                    self._parse_records_snapshot_locked()
                )
                index = self._build_index(records)
                physical_line_count = self._last_read_physical_line_count
            self._index_cache = index
            self._index_cache_signature = signature
            self._index_cache_boundary_digest = (
                self._last_read_boundary_digest
            )
            self._index_cache_physical_line_count = physical_line_count
            self._index_cache_initialized = True
            self._sequence_floor = max(
                self._sequence_floor,
                index.max_append_sequence,
            )
            self._sequence_validated = True
        return InstallHistorySnapshot(
            index=index,
            signature=signature,
        )

    def _write_sequence_locked(self, sequence: int) -> None:
        """原子持久化 sequence；rename 后同步父目录元数据。"""
        temporary_path = self._sequence_path.with_name(
            f".{self._sequence_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary_path.open(
                "x",
                encoding="ascii",
            ) as sequence_file:
                sequence_file.write(str(sequence))
                sequence_file.flush()
                os.fsync(sequence_file.fileno())
            os.replace(temporary_path, self._sequence_path)
            fsync_directory(self._sequence_path.parent)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError as cleanup_error:
                logger.debug(
                    "install history sequence temporary already removed "
                    "path=%s exception_type=%s",
                    temporary_path,
                    type(cleanup_error).__name__,
                )

    def _next_sequence_locked(self, minimum: int) -> int:
        stored: int | None = None
        sequence_present = self._sequence_path.is_file()
        if sequence_present:
            try:
                sequence_text = self._sequence_path.read_text(
                    encoding="ascii",
                ).strip()
                if not sequence_text:
                    raise ValueError("empty install sequence")
                stored = int(sequence_text)
                if stored < 0:
                    raise ValueError("negative install sequence")
            except (OSError, UnicodeError, ValueError) as exc:
                logger.warning(
                    "install sequence invalid; rebuilding from history "
                    "path=%s error_type=%s",
                    self._sequence_path,
                    type(exc).__name__,
                )
                stored = None
        if (
            not self._sequence_validated
            or (sequence_present and stored is None)
            or (not sequence_present and self._sequence_floor > 0)
        ):
            existing = self._parse_records_locked()
            history_maximum = max(
                (
                    record.get("append_sequence", index)
                    for index, record in enumerate(existing, start=1)
                ),
                default=0,
            )
            stored = max(stored or 0, history_maximum)
            self._sequence_validated = True
        return max(stored or 0, minimum, self._sequence_floor) + 1

    def _append_records(
        self,
        records: Iterable[dict],
        *,
        minimum_sequence: int = 0,
    ) -> tuple[dict, ...]:
        drafts = []
        for source_record in records:
            record = dict(source_record)
            record_id = record.get("record_id")
            if isinstance(record_id, str):
                record = {"record_id": record_id, **record}
            drafts.append(record)
        if not drafts:
            return ()
        with self._lock, _exclusive_file_lock(self._history_lock_path):
            sequence = self._next_sequence_locked(minimum_sequence)
            for offset, record in enumerate(drafts):
                record["append_sequence"] = sequence + offset
            final_sequence = sequence + len(drafts) - 1
            self._write_sequence_locked(final_sequence)
            self._sequence_floor = max(
                self._sequence_floor,
                final_sequence,
            )
            payload = "".join(
                json.dumps(record, ensure_ascii=False) + "\n"
                for record in drafts
            )
            history_existed = self.path.exists()
            try:
                with self.path.open("a", encoding="utf-8") as history_file:
                    history_file.write(payload)
                    history_file.flush()
                    os.fsync(history_file.fileno())
            except OSError as exc:
                logger.error(
                    "install history append uncertain path=%s error_type=%s",
                    self.path,
                    type(exc).__name__,
                )
                raise InstallHistoryAppendUncertainError(
                    f"install history append is uncertain: {self.path}"
                ) from exc
            if not history_existed:
                fsync_directory(self.path.parent)
        return tuple(drafts)

    def _read_recovery(self, skill: str, target: str) -> Optional[dict]:
        recovery_path = self._recovery_path(skill, target)
        if not recovery_path.is_file():
            return None
        try:
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.error(
                "install recovery read failed path=%s error_type=%s",
                recovery_path,
                type(exc).__name__,
            )
            raise InstallHistoryCorruptError(
                f"invalid install recovery: {recovery_path}"
            ) from exc
        if not isinstance(recovery, dict):
            raise InstallHistoryCorruptError(
                f"install recovery is not an object: {recovery_path}"
            )
        expected = recovery.get("expected")
        records = recovery.get("records")
        decision_ids = recovery.get("decision_ids")
        if (
            recovery.get("schema_version") != 1
            or recovery.get("action") != "recover_install_transaction"
            or recovery.get("skill") != skill
            or recovery.get("target") != target
            or recovery.get("state") not in (
                "prepared",
                "applying",
                "applied",
                "cancelled",
            )
            or not isinstance(recovery.get("transaction_id"), str)
            or not isinstance(expected, dict)
            or expected.get("side") not in (None, "main", "staging")
            or not isinstance(expected.get("sha"), str)
            or not isinstance(expected.get("generation"), str)
            or not isinstance(recovery.get("source_generation"), str)
            or not isinstance(decision_ids, list)
            or any(not isinstance(item, str) or not item for item in decision_ids)
            or not isinstance(records, list)
            or not records
            or any(not isinstance(record, dict) for record in records)
        ):
            logger.error("invalid install recovery schema path=%s", recovery_path)
            raise InstallHistoryCorruptError(
                f"invalid install recovery schema: {recovery_path}"
            )
        record_ids = [record.get("record_id") for record in records]
        if (
            any(
                not isinstance(record_id, str) or not record_id
                for record_id in record_ids
            )
            or len(record_ids) != len(set(record_ids))
        ):
            logger.error(
                "invalid install recovery record ids path=%s", recovery_path
            )
            raise InstallHistoryCorruptError(
                f"invalid install recovery record ids: {recovery_path}"
            )
        return recovery

    def _write_recovery(
        self,
        *,
        skill: str,
        target: str,
        transaction_id: str,
        records: list[dict],
        decision_ids: tuple[str, ...],
        side: Optional[str],
        sha: str,
        generation: str,
        source_generation: str,
        state: str,
    ) -> None:
        recovery_path = self._recovery_path(skill, target)
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
        recovery = {
            "schema_version": 1,
            "action": "recover_install_transaction",
            "state": state,
            "transaction_id": transaction_id,
            "skill": skill,
            "target": target,
            "side": side,
            "sha": sha,
            "generation": generation,
            "source_generation": source_generation,
            "decision_ids": list(decision_ids),
            "expected": {
                "side": side,
                "sha": sha,
                "generation": generation,
            },
            "records": records,
        }
        temporary_path = recovery_path.with_suffix(".tmp")
        with temporary_path.open("w", encoding="utf-8") as recovery_file:
            json.dump(recovery, recovery_file, ensure_ascii=False)
            recovery_file.flush()
            os.fsync(recovery_file.fileno())
        os.replace(temporary_path, recovery_path)
        fsync_directory(recovery_path.parent)

    def _clear_recovery(self, skill: str, target: str) -> None:
        recovery_path = self._recovery_path(skill, target)
        if not recovery_path.exists():
            return
        recovery_path.unlink()
        fsync_directory(recovery_path.parent)

    def _repair_incomplete_tail_for_recovery(
        self,
        recoveries: Iterable[tuple[InstallTransactionRequest, dict]],
    ) -> None:
        """仅凭完整恢复日志修最后一条中断写，绝不跳过中间坏行。"""
        if not self.path.is_file():
            return
        with self._lock, _exclusive_file_lock(self._history_lock_path):
            try:
                payload = self.path.read_bytes()
            except OSError as exc:
                raise InstallHistoryCorruptError(
                    f"install history cannot be read: {self.path}"
                ) from exc
            if not payload or payload.endswith(b"\n"):
                return
            line_start = payload.rfind(b"\n") + 1
            tail = payload[line_start:]
            try:
                decoded_tail = tail.decode("utf-8", errors="strict")
                parsed_tail = json.loads(decoded_tail)
                if not isinstance(parsed_tail, dict):
                    raise json.JSONDecodeError(
                        "history tail is not an object", decoded_tail, 0
                    )
            except (UnicodeDecodeError, json.JSONDecodeError):
                try:
                    completed_payload = payload[:line_start].decode(
                        "utf-8",
                        errors="strict",
                    )
                    for completed_line in completed_payload.splitlines():
                        if not completed_line.strip():
                            continue
                        completed_record = json.loads(completed_line)
                        if not isinstance(completed_record, dict):
                            raise json.JSONDecodeError(
                                "history line is not an object",
                                completed_line,
                                0,
                            )
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise InstallHistoryCorruptError(
                        "install history has corruption before recoverable "
                        f"tail: {self.path}"
                    ) from exc
                if not self._matches_recovery_record_prefix(
                    tail,
                    recoveries,
                ):
                    logger.error(
                        "interrupted history tail cannot be matched to one "
                        "recovery record path=%s offset=%d",
                        self.path,
                        line_start,
                    )
                    raise InstallHistoryCorruptError(
                        "install history tail is not safely recoverable: "
                        f"{self.path}"
                    )
                logger.warning(
                    "truncating interrupted install history tail path=%s "
                    "offset=%d; journal will restore exact records",
                    self.path,
                    line_start,
                )
                repaired_payload = payload[:line_start]
            else:
                repaired_payload = payload + b"\n"
            temporary_path = self.path.with_suffix(
                f"{self.path.suffix}.repair.tmp"
            )
            try:
                with temporary_path.open("wb") as repaired_file:
                    repaired_file.write(repaired_payload)
                    repaired_file.flush()
                    os.fsync(repaired_file.fileno())
                os.replace(temporary_path, self.path)
                fsync_directory(self.path.parent)
            except OSError as exc:
                temporary_path.unlink(missing_ok=True)
                raise InstallHistoryCorruptError(
                    f"install history tail repair failed: {self.path}"
                ) from exc

    @staticmethod
    def _matches_recovery_record_prefix(
        tail: bytes,
        recoveries: Iterable[tuple[InstallTransactionRequest, dict]],
    ) -> bool:
        """EOF 残片必须是 journal 中某条期望 JSONL 的合法字节前缀。"""
        append_sequence_marker = b', "append_sequence": '
        for _request, recovery in recoveries:
            for record in recovery["records"]:
                record_id = record.get("record_id")
                serialized_source = (
                    {"record_id": record_id, **record}
                    if isinstance(record_id, str)
                    else record
                )
                serialized_record = json.dumps(
                    serialized_source,
                    ensure_ascii=False,
                ).encode("utf-8")
                record_without_closing_brace = serialized_record[:-1]
                if record_without_closing_brace.startswith(tail):
                    return True
                if not tail.startswith(record_without_closing_brace):
                    continue
                sequence_tail = tail[len(record_without_closing_brace):]
                if append_sequence_marker.startswith(sequence_tail):
                    return True
                if not sequence_tail.startswith(append_sequence_marker):
                    continue
                sequence_fragment = sequence_tail[
                    len(append_sequence_marker):
                ]
                if (
                    not sequence_fragment
                    or sequence_fragment.isdigit()
                    or (
                        sequence_fragment.endswith(b"}")
                        and sequence_fragment[:-1].isdigit()
                    )
                ):
                    return True
        return False

    @staticmethod
    def _recovery_expected_state(recovery: dict) -> tuple[str, str, str]:
        expected = recovery["expected"]
        return (
            expected["side"],
            expected["sha"],
            expected["generation"],
        )

    def _recover_transactions(
        self,
        requests: tuple[InstallTransactionRequest, ...],
    ) -> InstallHistoryIndex:
        recoveries: list[tuple[InstallTransactionRequest, dict]] = []
        for request in requests:
            recovery = self._read_recovery(request.skill, request.target)
            if recovery is not None:
                if recovery["state"] == "cancelled":
                    logger.error(
                        "cancelled install transaction requires diagnosis "
                        "skill=%s target=%s transaction_id=%s",
                        request.skill,
                        request.target,
                        recovery["transaction_id"],
                    )
                    raise InstallHistoryCorruptError(
                        "cancelled install transaction cannot be recovered: "
                        f"{request.skill}/{request.target}"
                    )
                if recovery["state"] == "prepared":
                    logger.warning(
                        "discarding unapplied prepared transaction "
                        "skill=%s target=%s transaction_id=%s",
                        request.skill,
                        request.target,
                        recovery["transaction_id"],
                    )
                    self._clear_recovery(request.skill, request.target)
                    continue
                if request.generation_reader is not None:
                    current_generation = request.generation_reader()
                    allowed_generations = {
                        recovery["source_generation"],
                        recovery["expected"]["generation"],
                    }
                    if current_generation not in allowed_generations:
                        logger.error(
                            "install recovery generation fence rejected "
                            "skill=%s target=%s transaction_id=%s",
                            request.skill,
                            request.target,
                            recovery["transaction_id"],
                        )
                        raise InstallHistoryCorruptError(
                            "install recovery generation changed: "
                            f"{request.skill}/{request.target}"
                        )
                recoveries.append((request, recovery))
        if recoveries:
            self._repair_incomplete_tail_for_recovery(recoveries)
        index = self.index()
        if not recoveries:
            return index

        missing_records: list[dict] = []
        for request, recovery in recoveries:
            expected_side = recovery["expected"]["side"]
            if expected_side is not None:
                if request.installed_state_reader is None:
                    raise InstallHistoryCorruptError(
                        "physical install recovery requires state reader: "
                        f"{request.skill}/{request.target}"
                    )
                expected_state = self._recovery_expected_state(recovery)
                try:
                    actual_state = request.installed_state_reader()
                except Exception:
                    logger.warning(
                        "install recovery could not read current state; "
                        "restoring journal target skill=%s target=%s",
                        request.skill,
                        request.target,
                        exc_info=True,
                    )
                    actual_state = None
                if actual_state != expected_state:
                    if request.recovery_operation is None:
                        raise InstallHistoryCorruptError(
                            "physical install recovery requires operation: "
                            f"{request.skill}/{request.target}"
                        )
                    request.recovery_operation(recovery)
                    actual_state = request.installed_state_reader()
                if actual_state != expected_state:
                    logger.error(
                        "install recovery state mismatch skill=%s target=%s "
                        "expected=%s actual=%s",
                        request.skill,
                        request.target,
                        expected_state,
                        actual_state,
                    )
                    raise InstallHistoryCorruptError(
                        "install recovery could not restore expected state: "
                        f"{request.skill}/{request.target}"
                    )
            for record in recovery["records"]:
                if record["record_id"] not in index.record_ids:
                    missing_records.append(record)

        appended = self._append_records(
            missing_records,
            minimum_sequence=index.max_append_sequence,
        )
        if appended:
            index = self.index()
        for request, _recovery in recoveries:
            self._clear_recovery(request.skill, request.target)
        return index

    def record(
        self,
        *,
        skill: str,
        side: str,
        sha: str = "",
        t: Optional[float] = None,
        target: str | None = None,
        decision_ids: Iterable[str] = (),
    ) -> dict:
        """写一条 install 成功记录。返回写入的完整 record（含 t）。

        语义：``action`` 字段默认是 ``"install"``——本方法**只**写成功
        记录。失败请走 ``record_fail()``，那条记录形态不同（无
        ``side``、含 ``agent`` + ``reason``）。
        """
        if side not in ("main", "staging"):
            raise ValueError(f"side must be 'main' or 'staging', got {side!r}")
        record = {
            "t": t if t is not None else time.time(),
            "action": "install",
            "skill": skill,
            "side": side,
            "sha": sha,
        }
        if target is not None:
            record["target"] = target
        normalized_decision_ids = tuple(dict.fromkeys(decision_ids))
        if normalized_decision_ids:
            record["decision_ids"] = list(normalized_decision_ids)
        return self._append_records((record,))[0]

    def record_fail(
        self,
        *,
        skill: str,
        agent: str,
        reason: str,
        t: Optional[float] = None,
    ) -> dict:
        """写一条 install 失败记录。

        与 ``record()`` 的成功记录共享同一份 jsonl 文件；用 ``action`` 字段
        区分（``"install"`` vs ``"fail"``）。失败记录不带 side / sha——这两
        个字段只对成功 install 有意义；记 ``agent`` (``claude_code`` /
        ``codex`` / ``opencode``) + ``reason`` (异常摘要) 方便运维定位。

        ``lookup()`` / ``count_by_side()`` 内部按 ``action=="install"`` 过滤
        （成功记录默认无 action 字段或 action=="install"），失败记录不影响
        side 反查链路。
        """
        record = {
            "t": t if t is not None else time.time(),
            "action": "fail",
            "skill": skill,
            "agent": agent,
            "reason": reason,
        }
        return self._append_records((record,))[0]

    def all_records(self) -> list[dict]:
        """读全量；坏行会安全记录位置并 fail-loud，绝不静默改写 current。"""
        return list(self.index().records)

    def lookup(
        self,
        t: float,
        *,
        skill: Optional[str] = None,
        target: Optional[str] = None,
    ) -> Optional[dict]:
        """返回 ``t`` 时刻盘上装的是哪条**成功**记录（即 ``record.t ≤ t`` 中最晚的）。

        ``skill`` 指定时仅在该 skill 的记录里查；不指定时返回**全局**最后一
        条 ``record.t ≤ t``。在多 skill 同时灰度的场景应当传 skill。

        失败记录（``action == "fail"``）被过滤掉——本方法用于反查 side，
        失败记录无 side 字段，对反查无意义。

        没有合适记录（t 早于最早一条 install）返回 None。
        """
        if skill is not None and target is not None:
            return self.index().lookup_at(t, skill=skill, target=target)
        recs = self.index().records
        candidate: Optional[dict] = None
        for record in recs:
            if record.get("action", "install") != "install":
                continue
            if skill is not None and record.get("skill") != skill:
                continue
            if target is not None and record.get("target") != target:
                continue
            installed_at = record.get("t")
            if isinstance(installed_at, (int, float)) and installed_at <= t:
                if candidate is None or installed_at >= candidate["t"]:
                    candidate = record
        return candidate

    def count_by_side(
        self,
        *,
        skill: Optional[str] = None,
        target: Optional[str] = None,
    ) -> dict[str, int]:
        """各 side 装了多少次（调试 / 测试看分布用）。

        失败记录不计入。
        """
        counts: dict[str, int] = {"main": 0, "staging": 0}
        for r in self.all_records():
            if r.get("action", "install") != "install":
                continue
            if skill is not None and r.get("skill") != skill:
                continue
            if target is not None and r.get("target") != target:
                continue
            side = r.get("side")
            if side in counts:
                counts[side] += 1
        return counts

    def fail_records(self) -> list[dict]:
        """返回所有失败记录（``action == "fail"``）。运维 / 测试用。"""
        return [r for r in self.all_records() if r.get("action") == "fail"]

    def transact(
        self,
        *,
        skill: str,
        target: str,
        decision_ids: Iterable[str],
        operation: Callable[
            [InstallDecisionContext, tuple[str, ...]],
            Optional[InstallPlan],
        ],
        decision_kind: Optional[str] = None,
        decision_sequence: Optional[int] = None,
        expected_generation: Optional[str] = None,
        generation_reader: Optional[Callable[[], str]] = None,
        invoke_when_consumed: bool = False,
        installed_state_reader: Optional[
            Callable[[], tuple[str, str, str]]
        ] = None,
        recovery_operation: Optional[Callable[[dict], None]] = None,
    ) -> InstallTransactionResult:
        """在单一 ``(skill,target)`` 锁内校验、变更、追加并可补偿。"""
        request = InstallTransactionRequest(
            skill=skill,
            target=target,
            decision_ids=tuple(decision_ids),
            operation=operation,
            decision_kind=decision_kind,
            decision_sequence=decision_sequence,
            expected_generation=expected_generation,
            generation_reader=generation_reader,
            invoke_when_consumed=invoke_when_consumed,
            installed_state_reader=installed_state_reader,
            recovery_operation=recovery_operation,
        )
        return self.transact_many((request,))[0]

    def transact_many(
        self,
        requests: Iterable[InstallTransactionRequest],
    ) -> tuple[InstallTransactionResult, ...]:
        """一次锁多个目标、一次解析 history、一次合并追加全部计划。"""
        request_list = tuple(requests)
        if not request_list:
            return ()
        normalized_requests: list[InstallTransactionRequest] = []
        target_keys: set[tuple[str, str]] = set()
        for request in request_list:
            target_key = (request.skill, request.target)
            if target_key in target_keys:
                raise ValueError(
                    "transact_many requires unique (skill, target) requests"
                )
            target_keys.add(target_key)
            normalized_ids = tuple(dict.fromkeys(request.decision_ids))
            if (
                not normalized_ids
                or any(
                    not isinstance(decision_id, str) or not decision_id
                    for decision_id in normalized_ids
                )
            ):
                raise ValueError(
                    "decision_ids must contain non-empty strings"
                )
            normalized_requests.append(
                InstallTransactionRequest(
                    skill=request.skill,
                    target=request.target,
                    decision_ids=normalized_ids,
                    operation=request.operation,
                    decision_kind=request.decision_kind,
                    decision_sequence=request.decision_sequence,
                    expected_generation=request.expected_generation,
                    generation_reader=request.generation_reader,
                    invoke_when_consumed=request.invoke_when_consumed,
                    installed_state_reader=request.installed_state_reader,
                    recovery_operation=request.recovery_operation,
                )
            )
        normalized_request_tuple = tuple(normalized_requests)
        results: list[Optional[InstallTransactionResult]] = [
            None
        ] * len(normalized_request_tuple)
        prepared: list[_PreparedTransaction] = []

        with ExitStack() as lock_stack:
            for skill, target in sorted(target_keys):
                lock_stack.enter_context(
                    _exclusive_file_lock(
                        self._decision_lock_path(skill, target)
                    )
                )
            index = self._recover_transactions(normalized_request_tuple)
            for request_index, request in enumerate(
                normalized_request_tuple
            ):
                latest = index.latest(request.skill, request.target)
                pending_ids = tuple(
                    decision_id
                    for decision_id in request.decision_ids
                    if decision_id not in index.consumed(
                        request.skill, request.target
                    )
                )
                if not pending_ids and not request.invoke_when_consumed:
                    results[request_index] = InstallTransactionResult(
                        current=latest
                    )
                    continue
                if (
                    request.decision_kind is not None
                    and request.decision_sequence is not None
                ):
                    latest_sequence = index.latest_decision_sequence(
                        request.skill,
                        request.target,
                        request.decision_kind,
                    )
                    if (
                        latest_sequence is not None
                        and request.decision_sequence <= latest_sequence
                    ):
                        results[request_index] = InstallTransactionResult(
                            current=latest,
                            pending_decision_ids=pending_ids,
                            stale=True,
                        )
                        continue
                current_generation = (
                    request.generation_reader()
                    if request.generation_reader is not None
                    else None
                )
                if (
                    request.expected_generation is not None
                    and current_generation != request.expected_generation
                ):
                    results[request_index] = InstallTransactionResult(
                        current=latest,
                        pending_decision_ids=pending_ids,
                        stale=True,
                    )
                    continue
                context = InstallDecisionContext(
                    index=index,
                    latest=latest,
                    recovery=None,
                    current_generation=current_generation,
                )
                plan = request.operation(context, pending_ids)
                if plan is None:
                    results[request_index] = InstallTransactionResult(
                        current=latest,
                        pending_decision_ids=pending_ids,
                    )
                    continue
                drafts = [dict(record) for record in plan.records]
                if plan.side is not None:
                    if plan.side not in ("main", "staging"):
                        raise ValueError(
                            f"invalid install side: {plan.side!r}"
                        )
                    drafts.append({
                        "action": "install",
                        "skill": request.skill,
                        "target": request.target,
                        "side": plan.side,
                        "sha": plan.sha,
                        "generation": plan.generation,
                        "decision_ids": list(
                            plan.install_decision_ids or pending_ids
                        ),
                    })
                transaction_id = uuid.uuid4().hex
                record_time = time.time()
                for record_offset, draft in enumerate(drafts):
                    draft["record_id"] = (
                        f"{transaction_id}:{record_offset}"
                    )
                    draft.setdefault("t", record_time)
                    draft.setdefault("skill", request.skill)
                    draft.setdefault("target", request.target)
                    if request.decision_kind is not None:
                        draft.setdefault(
                            "decision_kind", request.decision_kind
                        )
                    if request.decision_sequence is not None:
                        draft.setdefault(
                            "decision_sequence",
                            request.decision_sequence,
                        )
                if not drafts:
                    results[request_index] = InstallTransactionResult(
                        current=latest,
                        pending_decision_ids=pending_ids,
                        value=plan.value,
                    )
                    continue
                prepared.append(_PreparedTransaction(
                    request=request,
                    context=context,
                    pending_decision_ids=pending_ids,
                    plan=plan,
                    records=drafts,
                    transaction_id=transaction_id,
                ))
                results[request_index] = InstallTransactionResult(
                    current=latest,
                    pending_decision_ids=pending_ids,
                    value=plan.value,
                )

            journaled: list[_PreparedTransaction] = []
            try:
                for transaction in prepared:
                    journaled.append(transaction)
                    self._write_recovery(
                        skill=transaction.request.skill,
                        target=transaction.request.target,
                        transaction_id=transaction.transaction_id,
                        records=transaction.records,
                        decision_ids=transaction.pending_decision_ids,
                        side=transaction.plan.side,
                        sha=transaction.plan.sha,
                        generation=transaction.plan.generation,
                        source_generation=(
                            transaction.context.current_generation or ""
                        ),
                        state="prepared",
                    )
            except Exception:
                for transaction in journaled:
                    self._clear_recovery(
                        transaction.request.skill,
                        transaction.request.target,
                    )
                raise

            applied: list[_PreparedTransaction] = []
            try:
                for transaction in prepared:
                    self._write_recovery(
                        skill=transaction.request.skill,
                        target=transaction.request.target,
                        transaction_id=transaction.transaction_id,
                        records=transaction.records,
                        decision_ids=transaction.pending_decision_ids,
                        side=transaction.plan.side,
                        sha=transaction.plan.sha,
                        generation=transaction.plan.generation,
                        source_generation=(
                            transaction.context.current_generation or ""
                        ),
                        state="applying",
                    )
                    applied.append(transaction)
                    if transaction.plan.apply is not None:
                        transaction.plan.apply()
                    self._write_recovery(
                        skill=transaction.request.skill,
                        target=transaction.request.target,
                        transaction_id=transaction.transaction_id,
                        records=transaction.records,
                        decision_ids=transaction.pending_decision_ids,
                        side=transaction.plan.side,
                        sha=transaction.plan.sha,
                        generation=transaction.plan.generation,
                        source_generation=(
                            transaction.context.current_generation or ""
                        ),
                        state="applied",
                    )
                appended = self._append_records(
                    (
                        record
                        for transaction in prepared
                        for record in transaction.records
                    ),
                    minimum_sequence=index.max_append_sequence,
                )
            except InstallDecisionCancelled as cancellation:
                if not cancellation.target_changed:
                    cancelling_transaction = applied[-1]
                    self._clear_recovery(
                        cancelling_transaction.request.skill,
                        cancelling_transaction.request.target,
                    )
                    rollback_failures = self._rollback_prepared(
                        applied[:-1]
                    )
                    self._clear_unapplied_prepared(prepared, applied)
                    if rollback_failures:
                        raise InstallHistoryCorruptError(
                            "cancelled install batch could not restore prior "
                            "targets"
                        ) from cancellation
                    return tuple(
                        result
                        if result is not None
                        else InstallTransactionResult(current=None)
                        for result in results
                    )
                for transaction in applied:
                    self._write_recovery(
                        skill=transaction.request.skill,
                        target=transaction.request.target,
                        transaction_id=transaction.transaction_id,
                        records=transaction.records,
                        decision_ids=transaction.pending_decision_ids,
                        side=transaction.plan.side,
                        sha=transaction.plan.sha,
                        generation=transaction.plan.generation,
                        source_generation=(
                            transaction.context.current_generation or ""
                        ),
                        state="cancelled",
                    )
                rollback_failures = self._rollback_prepared(applied)
                self._clear_unapplied_prepared(prepared, applied)
                if rollback_failures:
                    raise InstallHistoryCorruptError(
                        "cancelled install transaction could not be rolled "
                        "back"
                    ) from cancellation
                return tuple(
                    result
                    if result is not None
                    else InstallTransactionResult(current=None)
                    for result in results
                )
            except InstallHistoryAppendUncertainError:
                logger.exception(
                    "install history batch outcome uncertain; recovery "
                    "journals retained targets=%d",
                    len(prepared),
                )
                raise
            except Exception:
                self._rollback_prepared(applied)
                self._clear_unapplied_prepared(prepared, applied)
                raise

            appended_by_target: dict[
                tuple[object, object], list[dict]
            ] = {}
            for record in appended:
                record_key = (record.get("skill"), record.get("target"))
                appended_by_target.setdefault(record_key, []).append(record)
            for request_index, request in enumerate(
                normalized_request_tuple
            ):
                result = results[request_index]
                if result is None:
                    raise RuntimeError("transaction result was not initialized")
                target_records = tuple(
                    appended_by_target.get((request.skill, request.target), ())
                )
                if not target_records:
                    continue
                current = result.current
                for record in target_records:
                    if record.get("action", "install") == "install":
                        current = record
                results[request_index] = InstallTransactionResult(
                    current=current,
                    records=target_records,
                    pending_decision_ids=result.pending_decision_ids,
                    stale=result.stale,
                    value=result.value,
                )
            for transaction in prepared:
                self._clear_recovery(
                    transaction.request.skill,
                    transaction.request.target,
                )
        return tuple(
            result
            if result is not None
            else InstallTransactionResult(current=None)
            for result in results
        )

    def _rollback_prepared(
        self,
        applied: Iterable[_PreparedTransaction],
    ) -> tuple[_PreparedTransaction, ...]:
        """逆序补偿已应用计划；不能确认补偿的恢复日志必须保留。"""
        failures: list[_PreparedTransaction] = []
        for transaction in reversed(tuple(applied)):
            clear_recovery = transaction.plan.side is None
            if transaction.plan.rollback is not None:
                try:
                    transaction.plan.rollback()
                    clear_recovery = True
                except Exception:
                    logger.exception(
                        "install rollback failed skill=%s target=%s",
                        transaction.request.skill,
                        transaction.request.target,
                    )
                    failures.append(transaction)
            elif not clear_recovery:
                failures.append(transaction)
            if clear_recovery:
                self._clear_recovery(
                    transaction.request.skill,
                    transaction.request.target,
                )
        return tuple(failures)

    def _clear_unapplied_prepared(
        self,
        prepared: Iterable[_PreparedTransaction],
        applied: Iterable[_PreparedTransaction],
    ) -> None:
        """取消/失败时删除从未进入 applying 的后续计划，禁止 phantom 恢复。"""
        applied_transaction_ids = {
            transaction.transaction_id for transaction in applied
        }
        for transaction in prepared:
            if transaction.transaction_id in applied_transaction_ids:
                continue
            self._clear_recovery(
                transaction.request.skill,
                transaction.request.target,
            )
