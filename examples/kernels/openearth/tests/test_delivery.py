"""Checks for the source-private OpenEarth Kernel delivery bundle."""

from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZipFile


OPENEARTH = Path(__file__).resolve().parents[1]
WHEEL = (
    OPENEARTH / "wheels"
    / "openearth_skill_sdk-0.8.0-py3-none-any.whl"
)


def test_openearth_wheel_checksum_and_public_contents():
    expected = (OPENEARTH / "SHA256SUMS").read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(WHEEL.read_bytes()).hexdigest() == expected

    with ZipFile(WHEEL) as archive:
        names = set(archive.namelist())
        metadata = archive.read(
            "openearth_skill_sdk-0.8.0.dist-info/METADATA"
        ).decode("utf-8")

    assert "Version: 0.8.0" in metadata
    assert "openearth_skill_sdk/xskill.py" in names
    assert "openearth_skill_sdk/drafts.py" in names
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
