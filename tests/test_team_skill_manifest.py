from __future__ import annotations

import pickle
import subprocess
from pathlib import Path

import numpy as np

from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.team.server.profile_reco import ClientProfileRecommender
from xskill.team.server.skill_manifest import build_manifest, _ROUTER


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_skill(root: Path, name: str, *, with_staging: bool = False) -> Path:
    d = root / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d)
    _git(["checkout", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@t"], d)
    _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nmetadata:\n  version: 1\n---\n# {name}\n",
        encoding="utf-8",
    )
    _git(["add", "."], d)
    _git(["commit", "-q", "-m", "v1"], d)
    if with_staging:
        _git(["checkout", "-q", "-b", "staging"], d)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d2\nmetadata:\n  version: 2\n---\n# {name} v2\n",
            encoding="utf-8",
        )
        _git(["commit", "-q", "-am", "v2"], d)
        _git(["checkout", "-q", "main"], d)
    return d


def _make_baby_skill(root: Path, name: str) -> Path:
    """造一个还在 baby 分支的 stub skill——cluster 建了但 SkillEditAgent
    还没跑过，没有 main 分支。"""
    d = root / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d)
    _git(["checkout", "-q", "-b", "baby"], d)
    _git(["config", "user.email", "t@t"], d)
    _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: stub\nmetadata:\n  version: 0\n---\n# {name}\n",
        encoding="utf-8",
    )
    _git(["add", "."], d)
    _git(["commit", "-q", "-m", "init baby"], d)
    return d


def test_manifest_skips_baby_skill_without_main(tmp_path):
    """baby-state stub 没有 main 分支 → 不进 manifest，不抛错。"""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    _make_skill(skill_dir, "graduated")
    _make_baby_skill(skill_dir, "still-baby")
    resp = build_manifest(client_id="cid-1", skill_dir=skill_dir,
                          probability=0.2, ranked_slots=80, total_slots=100)
    names = [s.skill_name for s in resp.slots]
    assert names == ["graduated"]            # 只发已 graduate 的
    assert "still-baby" not in names


def test_manifest_caps_total_slots(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    for i in range(150):
        _make_skill(skill_dir, f"skill-{i:03d}")
    resp = build_manifest(client_id="cid-1", skill_dir=skill_dir,
                          probability=0.2, ranked_slots=80, total_slots=100)
    assert len(resp.slots) == 100
    ranked = [s for s in resp.slots if s.bucket == "ranked"]
    recommended = [s for s in resp.slots if s.bucket == "recommended"]
    assert len(ranked) == 80 and len(recommended) == 20


def test_manifest_main_only_skill_has_main_side(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    _make_skill(skill_dir, "no-staging")
    resp = build_manifest(client_id="cid-1", skill_dir=skill_dir,
                          probability=0.2, ranked_slots=80, total_slots=100)
    assert len(resp.slots) == 1
    assert resp.slots[0].side == "main"
    assert resp.slots[0].sha   # 非空


def test_manifest_staging_side_is_deterministic_per_client(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    _make_skill(skill_dir, "graying", with_staging=True)
    s1 = build_manifest(client_id="cid-A", skill_dir=skill_dir,
                        probability=0.2, ranked_slots=80, total_slots=100).slots[0]
    s2 = build_manifest(client_id="cid-A", skill_dir=skill_dir,
                        probability=0.2, ranked_slots=80, total_slots=100).slots[0]
    assert s1.side == s2.side          # 同 client 同 skill 永远同 side
    assert s1.side in ("main", "staging")
    # probability=1.0 → 必 staging；sha 必须是 staging HEAD
    forced = build_manifest(client_id="cid-A", skill_dir=skill_dir,
                            probability=1.0, ranked_slots=80, total_slots=100).slots[0]
    assert forced.side == "staging"


def test_manifest_guarantees_staging_traffic_with_few_clients(tmp_path):
    """3 个 worker + 唯一 staging skill：至少 1 个 worker 拿到 staging。

    这是 board6 灰度饿死 bug 的回归测试——旧的无状态 pick_side 会把 3 个
    client 全哈希到 main，staging 流量为 0。CanaryRouter 把 staging 份额锁进
    总量，保证 ≥1。
    """
    _ROUTER.reset()
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    _make_skill(skill_dir, "only-skill", with_staging=True)
    sides = [
        build_manifest(client_id=f"worker-{i}", skill_dir=skill_dir,
                       probability=0.5, ranked_slots=80, total_slots=100).slots[0].side
        for i in range(3)
    ]
    assert sides.count("staging") >= 1
    _ROUTER.reset()


# ═══════════════════════════════════════════════════════════════════
# SP3 — 画像推荐（profile_reco）
# ═══════════════════════════════════════════════════════════════════
#
# 单测构造一个**确定性**的合成 .skill_index.pkl（避免依赖真实 embed API），
# embedding 用 one-hot 单位向量——这样质心 = 用过的 skill 的均值方向，最近邻
# 可以精确推断。真实数据形态（85 个真实 skill + 真实 atom store 布局）已在
# 开发时单独串测验证过。


def _write_skill_index(skill_dir: Path, names: list[str], dim: int | None = None) -> None:
    """造一个 one-hot embedding 的 .skill_index.pkl。

    第 i 个 skill 的 embedding = e_i（第 i 维为 1 的单位向量）。这样：
    - 任意两个不同 skill 的 cosine = 0；
    - 一组 skill 的质心（归一化后）指向它们 one-hot 维度的平分方向，
      与组内 skill cosine = 1/sqrt(k) > 0、与组外 skill = 0。
    """
    n = len(names)
    d = dim if dim is not None else n
    emb = np.zeros((n, d), dtype=np.float32)
    for i in range(n):
        emb[i, i % d] = 1.0
    with open(skill_dir / ".skill_index.pkl", "wb") as f:
        pickle.dump({"skill_names": list(names), "embeddings": emb,
                     "texts": list(names), "method": "test"}, f)


def _save_atom(traj_root: Path, client_id: str, traj_id: str,
               atom_seq: int, used_skills: list[str]) -> None:
    """在该 client 的桶里落一个 atom，模拟 watcher 拆出的产物。"""
    sessions = traj_root / "clients" / client_id / "sessions"
    atom = AtomTask(
        atom_id=f"atom_{traj_id}_{atom_seq:04d}", traj_id=traj_id,
        offset_start=1, offset_end=5, intent="i", summary="s",
        used_skills=used_skills,
    )
    AtomTaskStore(root=sessions).save(atom)


def test_recommend_picks_centroid_nearest_excluding_used_and_ranked(tmp_path):
    """有 used_skills → recommended 是质心最近邻，且排除已用 / ranked。"""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    # skill-00..skill-09；用 one-hot embedding
    names = [f"skill-{i:02d}" for i in range(10)]
    for nm in names:
        _make_skill(skill_dir, nm)
    _write_skill_index(skill_dir, names)

    traj_root = tmp_path / "traj"
    client_id = "cid-warm"
    # client 用过 skill-00 / skill-01 → 质心 = (e0+e1)/sqrt2
    _save_atom(traj_root, client_id, "traj_a", 1, ["skill-00"])
    _save_atom(traj_root, client_id, "traj_a", 2, ["skill-01"])

    reco = ClientProfileRecommender()
    # 候选 = skill-02..skill-09（模拟已排除 ranked + 已用）
    candidates = [f"skill-{i:02d}" for i in range(2, 10)]
    out = reco.recommend(
        client_id=client_id, traj_root=traj_root,
        skill_index_path=skill_dir / ".skill_index.pkl",
        candidate_names=candidates, limit=3,
    )
    assert out is not None                    # 有画像
    assert len(out) == 3
    assert "skill-00" not in out and "skill-01" not in out   # 排除已用
    # one-hot 下所有候选与质心 cosine 都 = 0，靠稳定排序取前 3 个候选
    assert all(n in candidates for n in out)


def test_recommend_directional_centroid_prefers_topic(tmp_path):
    """质心方向性：用过同一维度的 skill → 推荐偏向同维度的候选。"""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    names = [f"skill-{i:02d}" for i in range(6)]
    for nm in names:
        _make_skill(skill_dir, nm)
    # 自造索引：skill-00/01/02 共享维度 0（同主题），03/04/05 各占独立维度
    emb = np.zeros((6, 4), dtype=np.float32)
    emb[0, 0] = emb[1, 0] = emb[2, 0] = 1.0   # 同主题簇
    emb[3, 1] = emb[4, 2] = emb[5, 3] = 1.0
    with open(skill_dir / ".skill_index.pkl", "wb") as f:
        pickle.dump({"skill_names": names, "embeddings": emb,
                     "texts": names, "method": "test"}, f)

    traj_root = tmp_path / "traj"
    # client 用过 skill-00 → 质心在维度 0
    _save_atom(traj_root, "cid-t", "traj_a", 1, ["skill-00"])

    reco = ClientProfileRecommender()
    out = reco.recommend(
        client_id="cid-t", traj_root=traj_root,
        skill_index_path=skill_dir / ".skill_index.pkl",
        candidate_names=["skill-01", "skill-02", "skill-03", "skill-04"],
        limit=2,
    )
    # skill-01/02 与质心 cosine=1，skill-03/04 cosine=0 → 必先选同主题的
    assert set(out) == {"skill-01", "skill-02"}


def test_recommend_cold_start_returns_none(tmp_path):
    """冷启动：client 无任何带 used_skills 的 atom → recommend 返回 None。"""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    names = [f"skill-{i:02d}" for i in range(5)]
    for nm in names:
        _make_skill(skill_dir, nm)
    _write_skill_index(skill_dir, names)

    traj_root = tmp_path / "traj"
    # 有 atom 但 used_skills 为空 —— 仍属冷启动（无画像）
    _save_atom(traj_root, "cid-cold", "traj_a", 1, [])

    reco = ClientProfileRecommender()
    out = reco.recommend(
        client_id="cid-cold", traj_root=traj_root,
        skill_index_path=skill_dir / ".skill_index.pkl",
        candidate_names=names, limit=3,
    )
    assert out is None      # 无画像 → None，调用方据此退回 ux


def test_recommend_cache_hit_skips_recompute(tmp_path, monkeypatch):
    """缓存命中：指纹不变时不重扫 atom store、不重算质心。"""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    names = [f"skill-{i:02d}" for i in range(5)]
    for nm in names:
        _make_skill(skill_dir, nm)
    _write_skill_index(skill_dir, names)

    traj_root = tmp_path / "traj"
    _save_atom(traj_root, "cid-c", "traj_a", 1, ["skill-00"])

    reco = ClientProfileRecommender()
    kw = dict(client_id="cid-c", traj_root=traj_root,
              skill_index_path=skill_dir / ".skill_index.pkl",
              candidate_names=["skill-01", "skill-02"], limit=2)
    out1 = reco.recommend(**kw)

    # 第二次调用：监视 _collect_used_skills，命中缓存就不该被调
    import xskill.team.server.profile_reco as pr
    calls = {"n": 0}
    orig = pr._collect_used_skills

    def _spy(root):
        calls["n"] += 1
        return orig(root)

    monkeypatch.setattr(pr, "_collect_used_skills", _spy)
    out2 = reco.recommend(**kw)
    assert out1 == out2
    assert calls["n"] == 0      # 指纹不变 → 没有重扫 atom store


def test_recommend_cache_invalidates_on_fingerprint_change(tmp_path, monkeypatch):
    """缓存失效：新增 atom 改变指纹 → 重算质心。"""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    names = [f"skill-{i:02d}" for i in range(5)]
    for nm in names:
        _make_skill(skill_dir, nm)
    _write_skill_index(skill_dir, names)

    traj_root = tmp_path / "traj"
    _save_atom(traj_root, "cid-i", "traj_a", 1, ["skill-00"])

    reco = ClientProfileRecommender()
    kw = dict(client_id="cid-i", traj_root=traj_root,
              skill_index_path=skill_dir / ".skill_index.pkl",
              candidate_names=["skill-01", "skill-02"], limit=2)
    reco.recommend(**kw)

    # 新增一条 traj → 指纹变 → 应重扫
    _save_atom(traj_root, "cid-i", "traj_b", 1, ["skill-01"])

    import xskill.team.server.profile_reco as pr
    calls = {"n": 0}
    orig = pr._collect_used_skills

    def _spy(root):
        calls["n"] += 1
        return orig(root)

    monkeypatch.setattr(pr, "_collect_used_skills", _spy)
    reco.recommend(**kw)
    assert calls["n"] == 1      # 指纹变了 → 重扫了一次


def test_build_manifest_cold_start_recommended_equals_ux(tmp_path):
    """冷启动：build_manifest 的 recommended 退回 ux 排序，与无画像一致。"""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    for i in range(30):
        _make_skill(skill_dir, f"skill-{i:02d}")
    _write_skill_index(skill_dir, [f"skill-{i:02d}" for i in range(30)])

    traj_root = tmp_path / "traj"
    # 一个没画像的 client（无 atom）
    cold = build_manifest(client_id="cid-cold", skill_dir=skill_dir,
                          probability=0.2, ranked_slots=20, total_slots=25,
                          traj_root=traj_root)
    pure_ux = build_manifest(client_id="cid-cold", skill_dir=skill_dir,
                             probability=0.2, ranked_slots=20, total_slots=25,
                             traj_root=None)
    cold_reco = [s.skill_name for s in cold.slots if s.bucket == "recommended"]
    ux_reco = [s.skill_name for s in pure_ux.slots if s.bucket == "recommended"]
    assert len(cold_reco) == 5
    assert cold_reco == ux_reco        # 冷启动 == 纯 ux


def test_build_manifest_warm_recommended_is_profile_driven(tmp_path):
    """暖路径：有画像 → recommended 受画像影响，且排除 ranked / 已用。"""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    names = [f"skill-{i:02d}" for i in range(30)]
    for nm in names:
        _make_skill(skill_dir, nm)
    # 自造索引：让 ux 尾部（ranked 之外）有一个主题簇
    emb = np.zeros((30, 30), dtype=np.float32)
    for i in range(30):
        emb[i, i] = 1.0
    # 把 skill-25 / skill-26 拉到同一维度（主题簇），client 用过 skill-25
    emb[25, :] = 0.0
    emb[26, :] = 0.0
    emb[25, 5] = 1.0
    emb[26, 5] = 1.0
    with open(skill_dir / ".skill_index.pkl", "wb") as f:
        pickle.dump({"skill_names": names, "embeddings": emb,
                     "texts": names, "method": "test"}, f)

    traj_root = tmp_path / "traj"
    # client 用过 skill-25（会落在 ranked 之外，因为所有 skill ux 相同→按名排序，
    # ranked_slots=20 时 skill-00..19 进 ranked，skill-20..29 是候选）
    _save_atom(traj_root, "cid-warm", "traj_a", 1, ["skill-25"])

    resp = build_manifest(client_id="cid-warm", skill_dir=skill_dir,
                          probability=0.2, ranked_slots=20, total_slots=25,
                          traj_root=traj_root)
    ranked = {s.skill_name for s in resp.slots if s.bucket == "ranked"}
    reco = [s.skill_name for s in resp.slots if s.bucket == "recommended"]
    assert len(reco) == 5
    assert all(n not in ranked for n in reco)        # 不在 ranked-80
    assert "skill-25" not in reco                    # 排除已用
    # skill-26 与 client 质心同向（cosine=1），别的候选 cosine=0 → 必排第一
    assert reco[0] == "skill-26"
