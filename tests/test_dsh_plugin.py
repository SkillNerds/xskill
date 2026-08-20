"""dsh-xskill bundle: manifest contract + Node helper tests.

The bundle lives in ``plugins/dsh-xskill`` so ``dsh plugin add`` can install
it (root ``package.json`` also declares ``dsh.bundle`` for
``github:SkillNerds/xskill``). These tests do not boot dsh; they pin the
files dsh will read and the scanner the plugin uses.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "dsh-xskill"
ROOT_PACKAGE = REPO_ROOT / "package.json"
PLUGIN_PACKAGE = PLUGIN_DIR / "package.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_root_and_plugin_package_declare_dsh_bundle():
    root = _load_json(ROOT_PACKAGE)
    plugin = _load_json(PLUGIN_PACKAGE)
    assert root["name"] == "dsh-xskill"
    assert plugin["name"] == "dsh-xskill"
    assert root["dsh"]["bundle"]["patch"] == "./plugins/dsh-xskill/cordis.patch.yml"
    assert plugin["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    assert (REPO_ROOT / root["dsh"]["bundle"]["patch"].lstrip("./")).is_file()
    assert (PLUGIN_DIR / "cordis.patch.yml").is_file()
    assert root["main"] == "plugins/dsh-xskill/index.js"
    assert plugin["main"] == "index.js"


def test_cordis_patch_inserts_dsh_xskill_row():
    patch = yaml.safe_load((PLUGIN_DIR / "cordis.patch.yml").read_text(encoding="utf-8"))
    assert isinstance(patch, list)
    insert = next(item["insert"] for item in patch if "insert" in item)
    row = next(item for item in insert if item.get("id") == "dsh-xskill")
    assert row["name"] == "dsh-xskill"
    assert row["config"]["rank"] == 350
    assert row["config"]["registerGuide"] is True


def test_plugin_entry_exports_cordis_contract():
    text = (PLUGIN_DIR / "index.js").read_text(encoding="utf-8")
    assert "export const name = 'dsh-xskill'" in text or 'export const name = "dsh-xskill"' in text
    assert "export const inject" in text
    assert "export function apply" in text
    assert "xskill_status" in text
    assert "xskill_list" in text
    assert "xskill_search" in text
    assert "registerProvider" in text


def test_bundled_guide_skill_is_valid_kebab_name():
    raw = (PLUGIN_DIR / "using-xskill.md").read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    assert "name: using-xskill" in raw
    assert "description:" in raw
    assert "\n---\n" in raw


def test_readme_documents_dsh_plugin_add():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "dsh plugin --profile web add github:SkillNerds/xskill" in readme
    plugin_readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    assert "dsh plugin" in plugin_readme
    assert "xskill_search" in plugin_readme


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for plugin helper tests")
def test_plugin_lib_node_tests():
    result = subprocess.run(
        ["node", "--test", "lib.test.js"],
        cwd=PLUGIN_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
