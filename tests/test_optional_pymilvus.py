"""pymilvus 为 optional extra：未安装时不崩，并节流 warn。"""
from __future__ import annotations

from pathlib import Path

import pytest

from xskill.recommend import skill_vector_store as svs


@pytest.fixture(autouse=True)
def _reset_milvus_gates(monkeypatch):
    monkeypatch.setattr(svs, "_pymilvus_import_ok", None)
    monkeypatch.setattr(svs, "_milvus_last_warn_mono", None)
    yield
    monkeypatch.setattr(svs, "_pymilvus_import_ok", None)
    monkeypatch.setattr(svs, "_milvus_last_warn_mono", None)


def test_open_skill_vector_index_falls_back_without_pymilvus(monkeypatch):
    monkeypatch.setattr(svs, "_pymilvus_import_ok", False)
    warned: list[str] = []

    def _capture(msg, *args, **_kwargs):
        warned.append(msg % args if args else msg)

    monkeypatch.setattr(svs.logger, "warning", _capture)
    index = svs.open_skill_vector_index(memory=False, dim=4)
    assert isinstance(index, svs.MemorySkillVectorIndex)
    assert any("Milvus Lite unavailable" in m for m in warned)


def test_milvus_unavailable_warn_throttled_hourly(monkeypatch):
    monkeypatch.setattr(svs, "_pymilvus_import_ok", False)
    monkeypatch.setattr(svs, "_milvus_last_warn_mono", None)
    warned: list[str] = []

    def _capture(msg, *args, **_kwargs):
        warned.append(msg % args if args else msg)

    monkeypatch.setattr(svs.logger, "warning", _capture)
    svs.warn_milvus_unavailable_hourly("pymilvus not installed")
    svs.warn_milvus_unavailable_hourly("pymilvus not installed")
    assert len(warned) == 1
    # 人为拨回时钟后应再 warn
    monkeypatch.setattr(
        svs, "_milvus_last_warn_mono",
        svs._milvus_last_warn_mono - svs._MILVUS_WARN_INTERVAL_S - 1,
    )
    svs.warn_milvus_unavailable_hourly("again")
    assert len(warned) == 2


def test_milvus_warn_fires_when_monotonic_uptime_under_interval(monkeypatch):
    """开机不足 1h 时，旧哨兵 0.0 会误吞首次 warn；None 哨兵必须放行。"""
    monkeypatch.setattr(svs, "_milvus_last_warn_mono", None)
    monkeypatch.setattr(svs.time, "monotonic", lambda: 120.0)
    warned: list[str] = []
    monkeypatch.setattr(
        svs.logger, "warning",
        lambda msg, *args, **_k: warned.append(msg % args if args else msg),
    )
    svs.warn_milvus_unavailable_hourly("pymilvus not installed")
    assert len(warned) == 1


def test_try_open_milvus_lite_returns_none_without_pymilvus(monkeypatch):
    monkeypatch.setattr(svs, "_pymilvus_import_ok", False)
    assert svs.try_open_milvus_lite_index(dim=4) is None


def test_pyproject_keeps_pymilvus_optional_only():
    """主依赖不得再硬拉 pymilvus（否则阻断 client 自动更新）。"""
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    # 不依赖 tomllib（3.9/3.10 无内置）；用段落切分即可
    deps_start = text.index("dependencies = [")
    extras_start = text.index("[project.optional-dependencies]")
    deps_block = text[deps_start:extras_start]
    assert "pymilvus" not in deps_block
    assert 'milvus = [' in text or 'milvus=[' in text
    assert 'pymilvus[milvus_lite]>=2.4.2' in text[extras_start:]
