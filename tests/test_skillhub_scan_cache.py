"""test_skillhub_scan_cache.py —— skillhub 三层扫描缓存（L1 TTL 快照 / L2 剪枝 / L3 备忘录）。

覆盖：TTL 内不重扫；内容不变零重读；改内容/删文件被反映；点目录不遍历；
force_refresh 绕过 TTL；single-flight 并发只扫一次；目录缺失 raise 且补齐后恢复；
以及 dashboard tag_cloud 的 TTL 缓存命中。
"""
from __future__ import annotations

import logging
import os
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

import xskill.recommend.skillhub as skillhub_module
from xskill.recommend.skillhub import SkillHub


def _write_hub_skill(hub_dir: Path, rel_path: str, description: str) -> Path:
    skill_dir = hub_dir / rel_path
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_dir.name}\ndescription: {description}\n---\n# {skill_dir.name}\n",
        encoding="utf-8")
    return skill_dir


def _make_hub(hub_dir: Path, *, scan_ttl_seconds: float = 5.0) -> SkillHub:
    return SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None,
                    scan_ttl_seconds=scan_ttl_seconds)


def _count_walks(monkeypatch, *, delay: float = 0.0) -> list[int]:
    walk_calls: list[int] = []
    real_walk = os.walk

    def counting_walk(top, *args, **kwargs):
        walk_calls.append(1)
        if delay:
            time.sleep(delay)
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(skillhub_module.os, "walk", counting_walk)
    return walk_calls


def test_ttl_snapshot_scans_disk_once(tmp_path, monkeypatch):
    hub_dir = tmp_path / "hub"
    _write_hub_skill(hub_dir, "alpha", "django migration helper")
    hub = _make_hub(hub_dir)
    walk_calls = _count_walks(monkeypatch)

    hub.entry("alpha")
    hub.fingerprint()
    hub.entry("alpha")

    assert len(walk_calls) == 1


def test_unchanged_files_are_not_reread(tmp_path, monkeypatch):
    hub_dir = tmp_path / "hub"
    _write_hub_skill(hub_dir, "alpha", "one")
    _write_hub_skill(hub_dir, "nested/beta", "two")
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)  # 每次调用都真扫，检验 L3 备忘录

    read_calls: list[str] = []
    real_read_bytes = pathlib.Path.read_bytes

    def counting_read_bytes(self):
        read_calls.append(str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counting_read_bytes)

    hub.fingerprint()
    assert len(read_calls) == 2  # 首轮每个 SKILL.md 读一次
    read_calls.clear()

    hub.fingerprint()
    assert read_calls == []  # mtime/size 未变 → 零重读


def test_changed_content_is_reflected(tmp_path):
    hub_dir = tmp_path / "hub"
    skill_dir = _write_hub_skill(hub_dir, "alpha", "old description")
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)

    first = hub.entry("alpha")
    old_sha = first["content_sha"]

    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: brand new much longer description\n---\n# alpha\n",
        encoding="utf-8")
    future = time.time() + 5
    os.utime(skill_dir / "SKILL.md", (future, future))

    updated = hub.entry("alpha")
    assert updated["content_sha"] != old_sha
    assert updated["description"] == "brand new much longer description"


def test_deleted_skill_disappears(tmp_path):
    hub_dir = tmp_path / "hub"
    skill_dir = _write_hub_skill(hub_dir, "alpha", "one")
    _write_hub_skill(hub_dir, "beta", "two")
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)

    assert hub.entry("alpha") is not None
    import shutil
    shutil.rmtree(skill_dir)

    assert hub.entry("alpha") is None
    assert (skill_dir / "SKILL.md") not in hub._file_memo


def test_dot_directories_are_not_traversed(tmp_path, monkeypatch):
    hub_dir = tmp_path / "hub"
    _write_hub_skill(hub_dir, "alpha", "visible")
    deep_git_skill = hub_dir / ".git" / "objects" / "hidden"
    deep_git_skill.mkdir(parents=True)
    (deep_git_skill / "SKILL.md").write_text(
        "---\nname: hidden\ndescription: buried in git\n---\n", encoding="utf-8")
    hub = _make_hub(hub_dir)

    visited_dirs: list[str] = []
    real_walk = os.walk

    def recording_walk(top, *args, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
            visited_dirs.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(skillhub_module.os, "walk", recording_walk)

    entries = hub._entries(include_vec=False, require_description=False)
    names = {entry["display_name"] for entry in entries}
    assert names == {"alpha"}
    assert not any(".git" in visited for visited in visited_dirs)
    assert (deep_git_skill / "SKILL.md") not in hub._file_memo


def test_force_refresh_bypasses_ttl(tmp_path):
    hub_dir = tmp_path / "hub"
    _write_hub_skill(hub_dir, "alpha", "one")
    hub = _make_hub(hub_dir, scan_ttl_seconds=5.0)

    assert hub.entry("alpha") is not None
    _write_hub_skill(hub_dir, "beta", "two")

    assert hub.entry("beta") is None  # TTL 内旧快照看不到新 skill
    assert hub.entry("beta", force_refresh=True) is not None


def test_force_refresh_bypasses_same_size_same_mtime_file_memo(tmp_path):
    hub_dir = tmp_path / "hub"
    skill_dir = _write_hub_skill(hub_dir, "alpha", "old")
    hub = _make_hub(hub_dir, scan_ttl_seconds=60.0)
    first = hub.entry("alpha")
    skill_md = skill_dir / "SKILL.md"
    original_stat = skill_md.stat()
    replacement = skill_md.read_bytes().replace(b"old", b"new")
    assert len(replacement) == original_stat.st_size
    skill_md.write_bytes(replacement)
    os.utime(skill_md, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    refreshed = hub.entry("alpha", force_refresh=True)
    assert refreshed["description"] == "new"
    assert refreshed["content_sha"] != first["content_sha"]


def test_single_flight_concurrent_entry(tmp_path, monkeypatch):
    hub_dir = tmp_path / "hub"
    for index in range(5):
        _write_hub_skill(hub_dir, f"skill_{index}", f"desc {index}")
    hub = _make_hub(hub_dir)
    walk_calls = _count_walks(monkeypatch, delay=0.05)

    barrier = threading.Barrier(8)

    def call_entry():
        barrier.wait()
        return hub.entry("skill_0")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [future.result() for future in
                   [pool.submit(call_entry) for _ in range(8)]]

    assert all(result is not None for result in results)
    assert len(walk_calls) == 1


def test_invalid_skills_are_isolated_and_log_safe_locations(
    tmp_path, monkeypatch, caplog,
):
    hub_dir = tmp_path / "hub"
    _write_hub_skill(hub_dir, "valid/alpha", "alpha searchable helper")

    encoding_path = hub_dir / "broken/encoding/SKILL.md"
    encoding_path.parent.mkdir(parents=True)
    encoding_bytes = (
        b"---\nname: encoding\ndescription: invalid \xa1\xaa marker\n---\nbody\n"
    )
    encoding_path.write_bytes(encoding_bytes)

    eof_path = hub_dir / "broken/eof/SKILL.md"
    eof_path.parent.mkdir(parents=True)
    eof_bytes = b"---\nname: eof\ndescription: invalid at eof\n---\nbody\n\xff"
    eof_path.write_bytes(eof_bytes)

    schema_path = hub_dir / "broken/schema/SKILL.md"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text(
        "---\nname: schema\ndescription:\n  - invalid\n---\nbody\n",
        encoding="utf-8",
    )

    io_path = hub_dir / "broken/io/SKILL.md"
    io_path.parent.mkdir(parents=True)
    io_path.write_text(
        "---\nname: io\ndescription: unreadable\n---\nbody\n",
        encoding="utf-8",
    )

    real_read_bytes = pathlib.Path.read_bytes

    def controlled_read_bytes(self):
        if self == io_path:
            raise PermissionError("absolute path and secret must not enter logs")
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", controlled_read_bytes)
    caplog.set_level(logging.WARNING, logger="xskill.skillhub")

    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)
    results = hub.search("alpha", limit=5)

    assert [result["display_name"] for result in results] == ["alpha"]
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    invalid_offset = encoding_bytes.index(b"\xa1")
    assert (
        "path=broken/encoding/SKILL.md "
        f"offset={invalid_offset} bytes=a1aa206d"
    ) in log_text
    eof_offset = eof_bytes.index(b"\xff")
    assert (
        "path=broken/eof/SKILL.md "
        f"offset={eof_offset} bytes=ff"
    ) in log_text
    assert (
        "path=broken/schema/SKILL.md "
        "error_type=SkillFileSchemaError"
    ) in log_text
    assert "path=broken/io/SKILL.md error_type=PermissionError" in log_text
    assert str(hub_dir) not in log_text
    assert "secret" not in log_text
    assert "marker" not in log_text


def test_unchanged_invalid_skill_is_not_reread_or_relogged(
    tmp_path, monkeypatch, caplog,
):
    hub_dir = tmp_path / "hub"
    invalid_path = hub_dir / "broken/SKILL.md"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_bytes(b"\xa1\xaa")
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)
    monotonic = Mock(return_value=100.0)

    read_calls: list[Path] = []
    real_read_bytes = pathlib.Path.read_bytes

    def counting_read_bytes(self):
        read_calls.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counting_read_bytes)
    monkeypatch.setattr(skillhub_module.time, "monotonic", monotonic)
    caplog.set_level(logging.WARNING, logger="xskill.skillhub")

    assert hub.fingerprint() == ()
    assert hub.fingerprint() == ()
    monotonic.return_value += (
        skillhub_module.INVALID_SKILL_RECHECK_COOLDOWN_SECONDS + 1
    )
    assert hub.fingerprint() == ()

    warnings = [
        record for record in caplog.records
        if "invalid UTF-8" in record.getMessage()
    ]
    assert read_calls == [invalid_path, invalid_path]
    assert len(warnings) == 1


def test_force_refresh_retries_unchanged_invalid_skill(
    tmp_path, monkeypatch, caplog,
):
    hub_dir = tmp_path / "hub"
    invalid_path = hub_dir / "broken/SKILL.md"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_bytes(b"\xa1\xaa")
    hub = _make_hub(hub_dir, scan_ttl_seconds=60.0)

    read_calls: list[Path] = []
    real_read_bytes = pathlib.Path.read_bytes

    def counting_read_bytes(self):
        read_calls.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counting_read_bytes)
    caplog.set_level(logging.WARNING, logger="xskill.skillhub")

    assert hub.entry("broken") is None
    assert hub.entry("broken", force_refresh=True) is None

    warnings = [
        record for record in caplog.records
        if "invalid UTF-8" in record.getMessage()
    ]
    assert read_calls == [invalid_path, invalid_path]
    assert len(warnings) == 2


def test_modified_invalid_skill_recovers_without_force_refresh(tmp_path):
    hub_dir = tmp_path / "hub"
    invalid_path = hub_dir / "repaired/SKILL.md"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_bytes(b"\xa1\xaa")
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)

    assert hub.entry("repaired") is None
    invalid_path.write_text(
        "---\nname: repaired\ndescription: repaired searchable skill\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    future = time.time() + 5
    os.utime(invalid_path, (future, future))

    repaired = hub.entry("repaired")
    assert repaired is not None
    assert repaired["description"] == "repaired searchable skill"
    assert hub._file_memo[invalid_path][3] == "repaired"


def test_same_metadata_invalid_skill_is_rechecked_and_recovers(
    tmp_path, monkeypatch,
):
    hub_dir = tmp_path / "hub"
    skill_path = hub_dir / "fixed/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    valid_bytes = (
        b"---\nname: fixed\ndescription: same metadata\n---\nbody\n"
    )
    invalid_bytes = valid_bytes.replace(b"same", b"\xffame", 1)
    skill_path.write_bytes(invalid_bytes)
    original_stat = skill_path.stat()

    monotonic = Mock(return_value=100.0)
    monkeypatch.setattr(skillhub_module.time, "monotonic", monotonic)
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)
    assert hub.entry("fixed") is None

    skill_path.write_bytes(valid_bytes)
    os.utime(
        skill_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert skill_path.stat().st_size == original_stat.st_size
    assert skill_path.stat().st_mtime_ns == original_stat.st_mtime_ns

    monotonic.return_value += (
        skillhub_module.INVALID_SKILL_RECHECK_COOLDOWN_SECONDS - 1
    )
    assert hub.entry("fixed") is None
    monotonic.return_value += 2

    repaired = hub.entry("fixed")
    assert repaired is not None
    assert repaired["description"] == "same metadata"


def test_periodic_invalid_rechecks_are_bounded_per_scan(
    tmp_path, monkeypatch, caplog,
):
    hub_dir = tmp_path / "hub"
    invalid_paths = []
    for index in range(5):
        skill_path = hub_dir / f"broken-{index}" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_bytes(b"\xa1\xaa")
        invalid_paths.append(skill_path)

    read_calls: list[Path] = []
    real_read_bytes = pathlib.Path.read_bytes

    def counting_read_bytes(self):
        if self in invalid_paths:
            read_calls.append(self)
        return real_read_bytes(self)

    monotonic = Mock(return_value=100.0)
    monkeypatch.setattr(pathlib.Path, "read_bytes", counting_read_bytes)
    monkeypatch.setattr(skillhub_module.time, "monotonic", monotonic)
    monkeypatch.setattr(skillhub_module, "INVALID_SKILL_RECHECK_LIMIT", 2)
    caplog.set_level(logging.WARNING, logger="xskill.skillhub")
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)

    assert hub.fingerprint() == ()
    assert len(read_calls) == 5
    monotonic.return_value += (
        skillhub_module.INVALID_SKILL_RECHECK_COOLDOWN_SECONDS + 1
    )

    assert hub.fingerprint() == ()
    assert len(read_calls) == 7
    assert hub.fingerprint() == ()
    assert len(read_calls) == 9
    assert hub.fingerprint() == ()
    assert len(read_calls) == 10
    assert all(read_calls.count(path) == 2 for path in invalid_paths)

    monotonic.return_value += (
        skillhub_module.INVALID_SKILL_RECHECK_COOLDOWN_SECONDS + 1
    )
    assert hub.fingerprint() == ()
    assert hub.fingerprint() == ()
    assert hub.fingerprint() == ()
    assert all(read_calls.count(path) == 3 for path in invalid_paths)
    assert len(hub._invalid_recheck_queue) == len(invalid_paths)
    assert len(hub._invalid_recheck_paths) == len(invalid_paths)
    warnings = [
        record for record in caplog.records
        if "invalid UTF-8" in record.getMessage()
    ]
    assert len(warnings) == 5


def test_transient_read_error_is_throttled_then_recovers(
    tmp_path, monkeypatch, caplog,
):
    hub_dir = tmp_path / "hub"
    skill_dir = _write_hub_skill(hub_dir, "readable", "readable helper")
    skill_path = skill_dir / "SKILL.md"
    monotonic = Mock(return_value=100.0)
    read_fails = [True]
    read_calls: list[int] = []
    real_read_bytes = pathlib.Path.read_bytes

    def controlled_read_bytes(self):
        if self == skill_path:
            read_calls.append(1)
            if read_fails[0]:
                raise PermissionError("transient read failure")
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", controlled_read_bytes)
    monkeypatch.setattr(skillhub_module.time, "monotonic", monotonic)
    caplog.set_level(logging.WARNING, logger="xskill.skillhub")
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)

    assert hub.entry("readable") is None
    assert hub.entry("readable") is None
    assert read_calls == [1]

    read_fails[0] = False
    monotonic.return_value += (
        skillhub_module.SKILL_FILE_IO_RETRY_COOLDOWN_SECONDS + 1
    )
    recovered = hub.entry("readable")

    assert recovered is not None
    assert read_calls == [1, 1]
    assert skill_path not in hub._file_io_retry_after
    warnings = [
        record for record in caplog.records
        if "error_type=PermissionError" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_transient_stat_error_is_throttled_then_recovers(
    tmp_path, monkeypatch, caplog,
):
    hub_dir = tmp_path / "hub"
    skill_dir = _write_hub_skill(hub_dir, "statable", "statable helper")
    skill_path = skill_dir / "SKILL.md"
    monotonic = Mock(return_value=100.0)
    stat_fails = [True]
    stat_calls: list[int] = []
    real_stat = pathlib.Path.stat

    def controlled_stat(self, *args, **kwargs):
        if self == skill_path:
            stat_calls.append(1)
            if stat_fails[0]:
                raise PermissionError("transient stat failure")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", controlled_stat)
    monkeypatch.setattr(skillhub_module.time, "monotonic", monotonic)
    caplog.set_level(logging.WARNING, logger="xskill.skillhub")
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)

    assert hub.entry("statable") is None
    assert hub.entry("statable") is None
    assert stat_calls == [1]

    stat_fails[0] = False
    monotonic.return_value += (
        skillhub_module.SKILL_FILE_IO_RETRY_COOLDOWN_SECONDS + 1
    )
    recovered = hub.entry("statable")

    assert recovered is not None
    assert stat_calls == [1, 1]
    assert skill_path not in hub._file_io_retry_after
    warnings = [
        record for record in caplog.records
        if "error_type=PermissionError" in record.getMessage()
    ]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "exception_type", [RuntimeError, AttributeError, ValueError],
)
def test_unknown_frontmatter_exception_propagates(
    tmp_path, monkeypatch, caplog, exception_type,
):
    hub_dir = tmp_path / "hub"
    skill_dir = _write_hub_skill(hub_dir, "unknown", "unknown helper")
    skill_path = skill_dir / "SKILL.md"

    def failing_parse(_text):
        raise exception_type("unexpected parser defect")

    monkeypatch.setattr(skillhub_module, "fm_parse", failing_parse)
    caplog.set_level(logging.WARNING, logger="xskill.skillhub")
    hub = _make_hub(hub_dir)

    with pytest.raises(exception_type, match="unexpected parser defect"):
        hub.fingerprint()

    assert skill_path not in hub._file_memo
    assert not any(
        "path=unknown/SKILL.md" in record.getMessage()
        for record in caplog.records
    )


def test_deleted_invalid_skill_clears_failed_file_memo(tmp_path):
    hub_dir = tmp_path / "hub"
    invalid_path = hub_dir / "deleted/SKILL.md"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_bytes(b"\xa1\xaa")
    hub = _make_hub(hub_dir, scan_ttl_seconds=0.0)

    assert hub.fingerprint() == ()
    assert invalid_path in hub._file_memo
    invalid_path.unlink()

    assert hub.fingerprint() == ()
    assert invalid_path not in hub._file_memo
    assert invalid_path not in hub._invalid_recheck_paths
    assert invalid_path not in hub._invalid_recheck_queue


def test_concurrent_search_reads_and_logs_invalid_skill_once(
    tmp_path, monkeypatch, caplog,
):
    hub_dir = tmp_path / "hub"
    _write_hub_skill(hub_dir, "alpha", "alpha helper")
    invalid_path = hub_dir / "broken/SKILL.md"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_bytes(b"\xa1\xaa")
    hub = _make_hub(hub_dir)

    invalid_read_calls: list[int] = []
    calls_lock = threading.Lock()
    real_read_bytes = pathlib.Path.read_bytes

    def counting_read_bytes(self):
        if self == invalid_path:
            with calls_lock:
                invalid_read_calls.append(1)
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counting_read_bytes)
    caplog.set_level(logging.WARNING, logger="xskill.skillhub")
    barrier = threading.Barrier(8)

    def search():
        barrier.wait()
        return hub.search("alpha", limit=5)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [
            future.result(timeout=10)
            for future in [pool.submit(search) for _ in range(8)]
        ]

    warnings = [
        record for record in caplog.records
        if "invalid UTF-8" in record.getMessage()
    ]
    assert all(rows[0]["display_name"] == "alpha" for rows in results)
    assert len(invalid_read_calls) == 1
    assert len(warnings) == 1


def test_missing_dir_raises_then_recovers(tmp_path):
    hub_dir = tmp_path / "hub"
    hub = _make_hub(hub_dir)

    with pytest.raises(FileNotFoundError):
        hub.entry("alpha")
    with pytest.raises(FileNotFoundError):
        hub.fingerprint()

    _write_hub_skill(hub_dir, "alpha", "now here")
    assert hub.entry("alpha") is not None


def test_tag_cloud_ttl_cache_hit(tmp_path, monkeypatch):
    from xskill.pipeline.atom import AtomTask, AtomTaskStore
    from xskill.pipeline.registry import get_connection
    from xskill.dashboard.metrics import DashboardMetrics
    import xskill.dashboard.metrics as dashboard_metrics

    watch_dir = tmp_path / "wd"
    watch_dir.mkdir()
    store = AtomTaskStore(root=watch_dir)
    store.save(AtomTask(
        atom_id="atom_t_0000", traj_id="t", offset_start=1, offset_end=2,
        intent="i", summary="s", tags=["django", "nginx"], used_skills=[], ux_score=7,
        pre_atom_id=None, post_atom_id=None, context_prefix="", raw_segment=""))
    db_path = tmp_path / "tg.db"
    conn = get_connection(db_path)
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES(?,?,?)",
                 (str(watch_dir), "w", "claude_code"))
    conn.commit()
    conn.close()

    dashboard_metrics._tag_cloud_cache.clear()
    traversal_calls: list[int] = []
    # tag_cloud 现只取 tags 字段，走 iter_tags（免构建完整 AtomTask）——TTL 缓存
    # 命中仍应只遍历一次。
    real_iter_tags = AtomTaskStore.iter_tags

    def counting_iter_tags(self):
        traversal_calls.append(1)
        yield from real_iter_tags(self)

    monkeypatch.setattr(AtomTaskStore, "iter_tags", counting_iter_tags)

    metrics = DashboardMetrics(db_path=db_path)
    first = {row["tag"]: row["count"] for row in metrics.tag_cloud()}
    second = {row["tag"]: row["count"] for row in metrics.tag_cloud()}

    assert first == second == {"django": 1, "nginx": 1}
    assert len(traversal_calls) == 1


def test_tag_cloud_concurrent_calls_walk_atoms_once(tmp_path, monkeypatch):
    """tag_cloud 的 TTL 缓存到期瞬间不许惊群：并发只走一次全量原子。"""
    from xskill.pipeline.atom import AtomTask, AtomTaskStore
    from xskill.pipeline.registry import get_connection
    from xskill.dashboard.metrics import DashboardMetrics
    import xskill.dashboard.metrics as dashboard_metrics

    watch_dir = tmp_path / "wd"
    watch_dir.mkdir()
    store = AtomTaskStore(root=watch_dir)
    store.save(AtomTask(
        atom_id="atom_t_0000", traj_id="t", offset_start=1, offset_end=2,
        intent="i", summary="s", tags=["django"], used_skills=[], ux_score=7,
        pre_atom_id=None, post_atom_id=None, context_prefix="", raw_segment=""))
    db_path = tmp_path / "tg.db"
    conn = get_connection(db_path)
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES(?,?,?)",
                 (str(watch_dir), "w", "claude_code"))
    conn.commit()
    conn.close()

    dashboard_metrics._tag_cloud_cache.clear()
    traversal_calls: list[int] = []
    calls_lock = threading.Lock()
    real_iter_tags = AtomTaskStore.iter_tags

    def counting_iter_tags(self):
        with calls_lock:
            traversal_calls.append(1)
        yield from real_iter_tags(self)

    monkeypatch.setattr(AtomTaskStore, "iter_tags", counting_iter_tags)
    metrics = DashboardMetrics(db_path=db_path)
    barrier = threading.Barrier(16)

    def load():
        barrier.wait()
        return metrics.tag_cloud()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = [future.result(timeout=10)
                   for future in [pool.submit(load) for _ in range(16)]]

    assert len(traversal_calls) == 1
    assert all(rows == [{"tag": "django", "count": 1, "users": []}]
               for rows in results)


def test_tag_cloud_returns_independent_copies(tmp_path):
    """缓存里的行不许被调用方改写（users 是可变 list）。"""
    from xskill.pipeline.atom import AtomTask, AtomTaskStore
    from xskill.pipeline.registry import get_connection
    from xskill.dashboard.metrics import DashboardMetrics
    import xskill.dashboard.metrics as dashboard_metrics

    watch_dir = tmp_path / "wd"
    watch_dir.mkdir()
    AtomTaskStore(root=watch_dir).save(AtomTask(
        atom_id="atom_t_0000", traj_id="t", offset_start=1, offset_end=2,
        intent="i", summary="s", tags=["django"], used_skills=[], ux_score=7,
        pre_atom_id=None, post_atom_id=None, context_prefix="", raw_segment=""))
    db_path = tmp_path / "tg.db"
    conn = get_connection(db_path)
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES(?,?,?)",
                 (str(watch_dir), "boss", "team_client"))
    conn.commit()
    conn.close()
    dashboard_metrics._tag_cloud_cache.clear()

    metrics = DashboardMetrics(db_path=db_path)
    first = metrics.tag_cloud()
    first[0]["count"] = 999
    first[0]["users"].append("intruder")
    first.append({"tag": "injected"})

    assert metrics.tag_cloud() == [{"tag": "django", "count": 1,
                                    "users": ["boss"]}]
