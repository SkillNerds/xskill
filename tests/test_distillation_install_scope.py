"""Installation callbacks use the run's explicit state database in every thread."""
from pathlib import Path

from xskill.pipeline.distillation import DistillationStore
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill.git import init_skill_repo_on_baby, commit_baby_to_main_branch
from xskill.skill.write_policy import library_write_guard


def test_two_watchers_do_not_share_implicit_installation_ledger(tmp_path, monkeypatch):
    from xskill import ecosystems
    from xskill.ecosystems.install_ledger import get_default_ledger

    forbidden_global = tmp_path / "global-installations.sqlite"
    monkeypatch.setenv("XSKILL_INSTALL_LEDGER", str(forbidden_global))
    monkeypatch.setattr(ecosystems, "detect_known_ecosystems", lambda **kwargs: [{"ecosystem": "codex"}])
    observed = []

    def install(source, **kwargs):
        observed.append(get_default_ledger().db_path.resolve())
        return Path(kwargs["target_root"]) / ".agents" / "skills" / source.name / "SKILL.md"

    monkeypatch.setattr(ecosystems, "install_to_codex", install)
    expected = []
    for suffix in ("a", "b"):
        state = tmp_path / suffix
        root = state / "skills"
        root.mkdir(parents=True)
        target = root / "target"
        init_skill_repo_on_baby(str(target), "target", "Diagnose failures")
        (target / "SKILL.md").write_text(
            "---\nname: target\ndescription: Diagnose failures\n---\nInspect the error and verify the repair.\n",
            encoding="utf-8",
        )
        assert commit_baby_to_main_branch(str(target), "ready")
        runs = DistillationStore(state / "registry.db", root)
        runs.configure("update_only", skill="target")
        watcher = DirectoryWatcher(skill_dir=root, db_path=runs.db_path,
                                   home_root=state / "home", distillation_store=runs)
        try:
            with library_write_guard(root, runs.policy(), db_path=runs.db_path):
                watcher._install_skill_to_all_detected(target)
        finally:
            watcher.stop(wait=True)
        expected.append((state / "installations.sqlite").resolve())
    assert observed == expected
    assert not forbidden_global.exists()
