"""Local configuration and DB-only status avoid model initialization."""
import json

from xskill.cli import build_parser


def test_distill_parser_supports_prepare_and_resume():
    parser = build_parser()
    args = parser.parse_args(["distill", "configure", "--mode", "create_one", "--skill", "chosen", "--description", "Useful methods"])
    assert args.distill_action == "configure"
    assert args.mode == "create_one"
    assert parser.parse_args(["distill", "configure", "--resume", "run-id"]).resume == "run-id"
    assert parser.parse_args(["distill", "status", "--json"]).json


def test_distill_configuration_and_status_do_not_create_models(tmp_path, monkeypatch, capsys):
    from xskill.cli import cmd_distill
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)
    monkeypatch.setattr("xskill.config.load_config", lambda: {"skill_dir": str(tmp_path / "skills")})
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    def forbidden(*args, **kwargs):
        raise AssertionError("distill CLI must not create model clients")
    monkeypatch.setattr("xskill.utils.llm.create_llm_client", forbidden)
    args = build_parser().parse_args(["distill", "configure", "--mode", "create_one", "--skill", "chosen", "--description", "Useful methods"])
    assert cmd_distill(args) == 0
    assert not (tmp_path / "skills" / "chosen").exists()
    capsys.readouterr()
    assert cmd_distill(build_parser().parse_args(["distill", "status", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "create_one"


def test_team_client_distill_is_rejected_before_loading_config(monkeypatch, capsys):
    from xskill.cli import cmd_distill
    monkeypatch.setattr("xskill.runtime.role", lambda: "client")
    monkeypatch.setattr("xskill.config.load_config", lambda: (_ for _ in ()).throw(AssertionError("unexpected config load")))
    assert cmd_distill(build_parser().parse_args(["distill", "status"])) == 2
    assert "local" in capsys.readouterr().err.lower()


def test_status_without_config_or_registry_is_read_only(tmp_path, monkeypatch, capsys):
    from xskill.cli import cmd_distill
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path / "state")
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    def forbidden(*args, **kwargs):
        raise AssertionError("status attempted credential/client initialization")
    monkeypatch.setattr("xskill.config.load_config", forbidden)
    monkeypatch.setattr("xskill.utils.llm.create_llm_client", forbidden)
    assert cmd_distill(build_parser().parse_args(["distill", "status", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "auto"
    assert not (tmp_path / "state").exists()


def test_direct_management_commands_reject_before_sideeffects(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from xskill import cli
    from xskill.pipeline.distillation import DistillationStore
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)
    DistillationStore(tmp_path / "registry.db", tmp_path / "skill").configure("create_one", "chosen", "Useful methods")
    args = SimpleNamespace()
    # Empty args would fail deep inside these handlers if admission did not
    # reject first; no client, source, network, or model is needed to reject.
    for name in ("cmd_import", "cmd_download", "cmd_repair_baselines", "cmd_init", "cmd_connect", "cmd_start", "cmd_generate"):
        assert getattr(cli, name)(args) == 2, name
    assert cli.cmd_rebuild(args, None) == 2
    assert "targeted distillation" in capsys.readouterr().err


def test_fresh_cli_process_rebuild_cannot_reset_active_run(tmp_path):
    import subprocess
    import sys
    from xskill.pipeline.distillation import DistillationStore
    # The test runner's environment-selected home is overridden before the
    # child imports xskill, using the existing supported XSKILL_HOME env key.
    store = DistillationStore(tmp_path / "registry.db", tmp_path / "skill")
    run = store.configure("create_one", "chosen", "Useful methods")
    script = """
import sys
from pathlib import Path
import xskill.config as config
config.XSKILL_HOME = Path(sys.argv[1])
from xskill.cli import main
sys.argv = ['xskill', 'rebuild', '--force']
raise SystemExit(main())
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stderr
    assert "targeted distillation" in result.stderr
    assert store.run()["run_id"] == run["run_id"]
    assert not (store.skill_dir / "chosen").exists()


def test_another_home_cannot_manage_library_with_restricted_explicit_locator(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from xskill.cli import cmd_import
    from xskill.pipeline.distillation import DistillationStore
    root = tmp_path / "shared-skills"
    DistillationStore(tmp_path / "home-a" / "registry.db", root).configure("create_one", "chosen", "Useful methods")
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    import yaml
    (home_b / "config.yaml").write_text(yaml.safe_dump({"skill_dir": str(root)}), encoding="utf-8")
    monkeypatch.setattr("xskill.config.XSKILL_HOME", home_b)
    assert cmd_import(SimpleNamespace()) == 2
