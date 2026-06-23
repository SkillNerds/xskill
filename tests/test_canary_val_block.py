"""test_canary_val_block.py -- 决策触发式 val 评测（val_block）单测
=====================================================================
覆盖 canary.check_and_decide 的"缺 val 就挂起等"改造：

  ux 样本已够、本应裁决，但 main/staging 任一 sha 在 .val_scores.json 缺分时——
  val_block=True → 不立即用纯 ux 回退，而是写 .val_request.json、返回 waiting_val
  （不动 staging）；等下一轮 val 分齐 → 正常综合分裁决、清掉 .val_request.json。
  val_block=False → 保持旧行为（缺 val 退回纯 ux 裁决）。

用例链（核心）：
  1. ux 够但缺 val + val_block=True → waiting_val、写了 .val_request.json、没翻牌。
  2. 补上两个 sha 的 val 分 → 再调一次 → 正常综合分裁决、.val_request.json 被清。
  3. val_block=False → 缺 val 时退回纯 ux 裁决（旧行为不变）。
  4. val 齐全时不写 request、直接裁决。
另含：兜底超时（等太久仍缺 val → 退回纯 ux）、gitignore 含 .val_request.json。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xskill import canary
from xskill.canary import CanaryConfig
from xskill.skill.git import run_git


# ──────────────────────────────────────────────────────
# Helpers（与 test_canary_val_score.py 同风格）
# ──────────────────────────────────────────────────────

def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], cwd=str(path))
    run_git(["checkout", "-b", "main"], cwd=str(path))
    run_git(["config", "user.email", "test@t"], cwd=str(path))
    run_git(["config", "user.name", "test"], cwd=str(path))
    (path / "SKILL.md").write_text("v1", encoding="utf-8")
    run_git(["add", "-A"], cwd=str(path))
    run_git(["commit", "-m", "init"], cwd=str(path))
    return path


def _commit(path: Path, content: str, msg: str):
    (path / "SKILL.md").write_text(content, encoding="utf-8")
    run_git(["add", "-A"], cwd=str(path))
    run_git(["commit", "-m", msg], cwd=str(path))


def _setup_staged(tmp_path) -> Path:
    sd = _init_repo(tmp_path / "skill")
    initial = canary.main_sha(sd)
    _commit(sd, "v2", "update")
    canary.route_main_history_to_staging(sd, initial)
    return sd


def _fill_ux(sd: Path, *, main_score: float, staging_score: float, n: int):
    m_sha = canary.main_sha(sd)
    s_sha = canary.staging_sha(sd)
    for i in range(n):
        canary.append_ux_score(sd, traj_id=f"m{i}", skill_name="s", side="main",
                               commit_sha=m_sha, score=main_score, reasons="")
        canary.append_ux_score(sd, traj_id=f"s{i}", skill_name="s", side="staging",
                               commit_sha=s_sha, score=staging_score, reasons="")


def _write_val(sd: Path, *, main_acc: float, staging_acc: float, n: int = 10):
    m_sha = canary.main_sha(sd)
    s_sha = canary.staging_sha(sd)
    p = sd / canary.VAL_SCORES_FILENAME
    p.write_text(json.dumps({
        m_sha: {"acc": main_acc, "n": n},
        s_sha: {"acc": staging_acc, "n": n},
    }), encoding="utf-8")


# ──────────────────────────────────────────────────────
# 1) 缺 val → 挂起 → 补分 → 裁决（核心链）
# ──────────────────────────────────────────────────────

class TestWaitThenDecide:
    def test_missing_val_blocks_and_writes_request(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=True)
        # ux 够（各 3 条），但不写 .val_scores.json → 缺 val
        _fill_ux(sd, main_score=6, staging_score=9, n=3)

        res = canary.check_and_decide(sd, cfg)

        # (1) 挂起：不 promote 不 discard
        assert res["action"] == "waiting_val", res
        assert canary.has_staging(sd)  # staging 没动
        # (2) 写了 .val_request.json，记下两个 sha
        req = canary.load_val_request(sd)
        assert req is not None
        assert req["main_sha"] == canary.main_sha(sd)
        assert req["staging_sha"] == canary.staging_sha(sd)
        assert "requested_ts" in req

    def test_request_not_refreshed_for_same_sha_pair(self, tmp_path):
        """同一对 sha 重复挂起不刷新 requested_ts（兜底超时不被无限续期）。"""
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=True)
        _fill_ux(sd, main_score=6, staging_score=9, n=3)
        canary.check_and_decide(sd, cfg)
        ts1 = canary.load_val_request(sd)["requested_ts"]
        canary.check_and_decide(sd, cfg)  # 再来一轮，仍缺 val
        ts2 = canary.load_val_request(sd)["requested_ts"]
        assert ts1 == ts2

    def test_val_filled_then_decides_and_clears_request(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=True)
        _fill_ux(sd, main_score=6, staging_score=9, n=3)

        # 第一轮：缺 val → 挂起
        first = canary.check_and_decide(sd, cfg)
        assert first["action"] == "waiting_val"
        assert canary.load_val_request(sd) is not None

        # 外部（algo 后台 loop）补上两个 sha 的 val 分：
        #   main ux=6 val=0.8 → 0.5*6 + 0.5*8 = 7.0
        #   staging ux=9 val=0.9 → 0.5*9 + 0.5*9 = 9.0 → staging 胜 → promoted
        _write_val(sd, main_acc=0.8, staging_acc=0.9)

        second = canary.check_and_decide(sd, cfg)
        assert second["action"] == "promoted", second
        assert abs(second["main_avg"] - 7.0) < 1e-6
        assert abs(second["staging_avg"] - 9.0) < 1e-6
        # .val_request.json 被清
        assert canary.load_val_request(sd) is None

    def test_val_filled_can_reject_via_composite(self, tmp_path):
        """补分后综合分把 ux 高但 val 低的 staging 拉下马 → rejected + 清 request。"""
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=True)
        _fill_ux(sd, main_score=6, staging_score=9, n=3)
        assert canary.check_and_decide(sd, cfg)["action"] == "waiting_val"
        # main 综合 = 0.5*6+0.5*8=7.0 ; staging = 0.5*9+0.5*2=5.5 < 7.0 → rejected
        _write_val(sd, main_acc=0.8, staging_acc=0.2)
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "rejected", res
        assert not canary.has_staging(sd)
        assert canary.load_val_request(sd) is None


# ──────────────────────────────────────────────────────
# 2) val_block=False → 旧行为（缺 val 退回纯 ux）
# ──────────────────────────────────────────────────────

class TestValBlockOff:
    def test_block_off_falls_back_to_pure_ux(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=False)
        _fill_ux(sd, main_score=3, staging_score=9, n=3)
        # 不写 val，val_block=False → 直接纯 ux 裁决（不挂起、不写 request）
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "promoted"
        assert res["main_avg"] == 3.0
        assert res["staging_avg"] == 9.0
        assert canary.load_val_request(sd) is None

    def test_val_weight_zero_does_not_block(self, tmp_path):
        """val_weight=0 时即便 val_block=True 也不挂起（综合分本就纯 ux）。"""
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.0, val_block=True)
        _fill_ux(sd, main_score=3, staging_score=9, n=3)
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "promoted"
        assert canary.load_val_request(sd) is None


# ──────────────────────────────────────────────────────
# 3) val 齐全 → 直接裁决，不写 request
# ──────────────────────────────────────────────────────

class TestValPresentNoRequest:
    def test_val_present_decides_without_request(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=True)
        _fill_ux(sd, main_score=5, staging_score=7, n=3)
        _write_val(sd, main_acc=0.6, staging_acc=0.9)  # 两 sha 都有
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "promoted"  # 0.5*7+0.5*9=8 >= 0.5*5+0.5*6=5.5
        assert canary.load_val_request(sd) is None


# ──────────────────────────────────────────────────────
# 4) 兜底超时：等太久仍缺 val → 退回纯 ux
# ──────────────────────────────────────────────────────

class TestTimeoutFallback:
    def test_stale_request_times_out_to_pure_ux(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=True,
                           val_block_timeout=60.0)
        _fill_ux(sd, main_score=3, staging_score=9, n=3)
        # 手写一个"很久以前"的 request（超过 60s 兜底窗口），sha 对齐当前两侧
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)
                  ).isoformat(timespec="seconds")
        canary.write_val_request(
            sd, main_sha_=canary.main_sha(sd),
            staging_sha_=canary.staging_sha(sd), requested_ts=old_ts)
        # 仍无 val 分 → 超时 → 放弃等待、退回纯 ux 裁决
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "promoted", res
        assert res["main_avg"] == 3.0
        assert res["staging_avg"] == 9.0
        assert canary.load_val_request(sd) is None  # 超时后清掉

    def test_timeout_zero_waits_forever(self, tmp_path):
        """val_block_timeout=0 → 永不超时，一直挂起等 val。"""
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=True,
                           val_block_timeout=0.0)
        _fill_ux(sd, main_score=3, staging_score=9, n=3)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)
                  ).isoformat(timespec="seconds")
        canary.write_val_request(
            sd, main_sha_=canary.main_sha(sd),
            staging_sha_=canary.staging_sha(sd), requested_ts=old_ts)
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "waiting_val", res


# ──────────────────────────────────────────────────────
# 5) config + gitignore
# ──────────────────────────────────────────────────────

class TestConfigAndGitignore:
    def test_from_dict_defaults_and_overrides(self):
        c = CanaryConfig.from_dict({})
        assert c.val_block is True
        assert c.val_block_timeout == 3600.0
        c2 = CanaryConfig.from_dict({"val_block": False, "val_block_timeout": 120})
        assert c2.val_block is False
        assert c2.val_block_timeout == 120.0

    def test_gitignore_includes_val_request(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        canary.ensure_gitignore(sd)
        gi = (sd / ".gitignore").read_text()
        assert ".val_request.json" in gi

    def test_gitignore_appends_val_request_when_missing(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        (sd / ".gitignore").write_text(
            ".ux_scores.jsonl\n.val_scores.json\n.lock\n", encoding="utf-8")
        canary.ensure_gitignore(sd)
        gi = (sd / ".gitignore").read_text()
        assert ".val_request.json" in gi
