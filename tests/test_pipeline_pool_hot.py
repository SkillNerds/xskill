"""席位、配额比 yaml 热补丁 + admin PATCH。"""
from __future__ import annotations

from xskill.config import patch_agent_worker_pool_yaml


def test_patch_pool_yaml_keeps_comment_and_updates_int():
    raw = (
        "llm:\n  base_url: http://x/v1\n"
        "agent_worker:\n"
        "  pools:\n"
        "    edit:\n"
        "      workers: 4  # default seats\n"
        "      llm_weight: 1\n"
    )
    out = patch_agent_worker_pool_yaml(raw, "edit", workers=8, llm_weight=3)
    assert "workers: 8  # default seats" in out
    assert "llm_weight: 3" in out
    assert "workers: 4" not in out


def test_patch_pool_yaml_rejects_zero():
    raw = "llm:\n  base_url: http://x/v1\nagent_worker:\n  pools:\n    edit:\n      workers: 4\n"
    try:
        patch_agent_worker_pool_yaml(raw, "edit", workers=0)
    except ValueError as exc:
        assert "正整数" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_refresh_pool_config_applies_after_disk_change(tmp_path):
    from tests.pool_helpers import pool_config
    from xskill.pipeline.runner import DirectoryWatcher

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "agent_worker:\n"
        "  pools:\n"
        "    split:\n      workers: 1\n      llm_weight: 1\n"
        "    cluster:\n      workers: 1\n      batch_size: 8\n      llm_weight: 1\n"
        "    edit:\n      workers: 2\n      llm_weight: 1\n"
        "    embed:\n      workers: 1\n",
        encoding="utf-8",
    )
    watcher = DirectoryWatcher(
        pool_config=pool_config(workers=1, edit_workers=2),
        config_path=cfg,
        xskill_home=tmp_path,
        home_root=tmp_path,
    )
    try:
        watcher._refresh_pool_config()
        assert watcher._pools["edit"].workers == 2
        cfg.write_text(
            "agent_worker:\n"
            "  pools:\n"
            "    split:\n      workers: 1\n      llm_weight: 1\n"
            "    cluster:\n      workers: 1\n      batch_size: 8\n      llm_weight: 1\n"
            "    edit:\n      workers: 5\n      llm_weight: 4\n"
            "    embed:\n      workers: 1\n",
            encoding="utf-8",
        )
        watcher._refresh_pool_config()
        assert watcher._pools["edit"].workers == 5
        assert watcher.pool_config["edit"]["llm_weight"] == 4
    finally:
        watcher.stop()

