"""Checks for the source-private OpenEarth Kernel delivery bundle."""

from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZipFile


OPENEARTH = Path(__file__).resolve().parents[1]
WHEEL = (
    OPENEARTH / "wheels"
    / "openearth_skill_sdk-0.9.0-py3-none-any.whl"
)


def test_openearth_wheel_checksum_and_public_contents():
    expected = (OPENEARTH / "SHA256SUMS").read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(WHEEL.read_bytes()).hexdigest() == expected

    with ZipFile(WHEEL) as archive:
        names = set(archive.namelist())
        metadata = archive.read(
            "openearth_skill_sdk-0.9.0.dist-info/METADATA"
        ).decode("utf-8")

    assert "Version: 0.9.0" in metadata
    assert "openearth_skill_sdk/xskill.py" in names
    assert "openearth_skill_sdk/drafts.py" in names
    assert "openearth_skill_sdk/benchmark.py" in names
    assert "openearth_skill_sdk/environments.py" in names
    assert "openearth_skill_sdk/target.py" in names
    assert not any(
        forbidden in name
        for name in names
        for forbidden in ("/gate.py", "/experiment.py", "/tests/")
    )


def test_openearth_delivery_documents_atom_score_sources():
    readme = (OPENEARTH / "README.md").read_text(encoding="utf-8")
    usage = (
        OPENEARTH / "docs" / "sdk-usage.md"
    ).read_text(encoding="utf-8")

    assert "atom.ux_score" in readme
    assert "OpenEarth oracle score" in readme
    assert "record_oracle_score" in usage
    assert "context.trajectories.create_temp" in usage
    assert "openearth-skill-level-classifications.json" in usage
    assert "changed_trajectory_ids" in usage
    assert "full_rebuild" in usage
    assert "atom_id" in usage
    assert "openearth-publication-queue.json" in usage
    assert "latest-wins" in usage
    assert "run_benchmark" in usage
    assert "openearth-benchmark-state.json" in usage


def test_oracle_temp_multi_atom_does_not_raise():
    """Platform may split temp trajectories into N atoms; oracle is case-level."""
    import importlib
    import sys
    import types
    from zipfile import ZipFile

    # Load xskill module from the vendored wheel without installing.
    with ZipFile(WHEEL) as archive:
        source = archive.read("openearth_skill_sdk/xskill.py").decode("utf-8")
    assert "exactly one" not in source
    assert "oracle_multi_atom" in source

    package = types.ModuleType("openearth_skill_sdk")
    package.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openearth_skill_sdk"] = package

    # Minimal stubs for relative imports used at module import time.
    for name in (
        "analyst",
        "backend",
        "config",
        "contracts",
        "service",
    ):
        stub = types.ModuleType(f"openearth_skill_sdk.{name}")
        if name == "contracts":
            class ScoredAtomInput:  # noqa: D401 - test stub
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            stub.ExistingSkillInput = object
            stub.ScoredAtomInput = ScoredAtomInput
            stub.TrainingResult = object
        if name == "config":
            stub.load_config = lambda *a, **k: {}
            stub.role_config = lambda *a, **k: {}
        if name == "analyst":
            stub.Analyst = object
        if name == "backend":
            stub.OpenCodeBackend = object
        if name == "service":
            stub.TrajectorySkillDistiller = object
        sys.modules[f"openearth_skill_sdk.{name}"] = stub

    spec = importlib.util.spec_from_loader(
        "openearth_skill_sdk.xskill",
        loader=None,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["openearth_skill_sdk.xskill"] = module
    exec(compile(source, "xskill.py", "exec"), module.__dict__)

    atom_a = types.SimpleNamespace(
        atom_id="a1",
        content="first",
        ux_score=None,
        intent="",
        summary="",
        used_skills=(),
        offset_start=0,
        offset_end=1,
    )
    atom_b = types.SimpleNamespace(
        atom_id="a2",
        content="second",
        ux_score=None,
        intent="",
        summary="",
        used_skills=(),
        offset_start=1,
        offset_end=2,
    )
    traj = types.SimpleNamespace(
        atom_split_status="ready",
        atoms=(atom_a, atom_b),
        source="temp",
        trajectory_id="traj_temp_case",
        id="wd1:traj_temp_case",
    )
    oracle = {
        "schema": 1,
        "trajectories": {
            "traj_temp_case": {"ux_score": 9, "case_id": "c1"},
        },
    }
    scored = list(module._atom_inputs([traj], oracle_scores=oracle))
    assert len(scored) == 2
    assert {item.atom_id for item in scored} == {"a1", "a2"}
    assert all(item.ux_score == 9 for item in scored)
    assert all(item.score_source == "oracle" for item in scored)
