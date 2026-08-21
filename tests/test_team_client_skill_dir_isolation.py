"""test_team_client_skill_dir_isolation.py — Issue #227 路径隔离与数据防损毁单元测试"""
from pathlib import Path
import shutil
import pytest

from xskill.config import get_skill_dir, get_client_skill_dir
from xskill.team.client.daemon import TeamClient
from xskill.team.client.state import ClientState
from xskill.team.shared.protocol import SyncResponse, SkillSlot



def test_client_and_server_skill_dir_isolation(tmp_path: Path):
    """验证 Client 与 Server 默认工作目录物理隔离且互不相同。"""
    xskill_home = tmp_path / ".xskill"
    server_dir = get_skill_dir({}, xskill_home=xskill_home)
    client_dir = get_client_skill_dir({}, xskill_home=xskill_home)

    assert server_dir == xskill_home / "skill"
    assert client_dir == xskill_home / "client_skill"
    assert server_dir != client_dir


def test_client_skill_dir_config_override(tmp_path: Path):
    """验证 config.yaml 的 client_skill_dir 自定义配置能够生效。"""
    xskill_home = tmp_path / ".xskill"
    custom_cfg = {"client_skill_dir": "my_custom_client_skills"}
    client_dir = get_client_skill_dir(custom_cfg, xskill_home=xskill_home)

    assert client_dir == xskill_home / "my_custom_client_skills"

    # 非法配置应抛出 ValueError
    with pytest.raises(ValueError, match="client_skill_dir 必须是非空字符串路径"):
        get_client_skill_dir({"client_skill_dir": "   "}, xskill_home=xskill_home)


def test_client_cleanup_does_not_touch_server_skill_dir(tmp_path: Path):
    """验证 Client 的 cleanup 机制仅作用于 client_skill，绝不误删 Server 技能库 (Issue #227 核心防护)。"""
    xskill_home = tmp_path / ".xskill"
    server_skill_dir = get_skill_dir({}, xskill_home=xskill_home)
    client_skill_dir = get_client_skill_dir({}, xskill_home=xskill_home)

    server_skill_dir.mkdir(parents=True, exist_ok=True)
    client_skill_dir.mkdir(parents=True, exist_ok=True)

    # 1. 模拟 Server 技能库拥有 5 个权威技能
    server_skills = ["skill-server-1", "skill-server-2", "skill-server-3", "skill-server-4", "skill-server-5"]
    for s in server_skills:
        s_dir = server_skill_dir / s
        s_dir.mkdir()
        (s_dir / "SKILL.md").write_text(f"# {s}", encoding="utf-8")

    # 2. 模拟 Client 目录下拥有 1 个旧技能
    (client_skill_dir / "stale-client-skill").mkdir()
    (client_skill_dir / "stale-client-skill" / "SKILL.md").write_text("# stale", encoding="utf-8")

    # 3. 构造 TeamClient 实例，作用于 client_skill_dir
    state = ClientState(server_url="http://127.0.0.1:8000", client_id="test-client-1", join_token="token-1")
    client = TeamClient(
        state=state,
        http=None,  # cleanup 不发 HTTP
        skill_dir=client_skill_dir,
        cursor_path=tmp_path / "cursor.json",
        history_path=tmp_path / "history.json",
        home_root=tmp_path / "home",
    )


    # 4. Manifest 中只分配了保留空的 slots
    manifest = SyncResponse(slots=[], server_time=1700000000.0)


    # 5. 执行 cleanup
    client.cleanup(manifest)

    # 6. 断言: Client 目录下的过时技能被正常清理
    assert not (client_skill_dir / "stale-client-skill").exists()

    # 7. 断言 (关键): Server 技能库里的 5 个技能完好无损，一个都没被删除！
    for s in server_skills:
        assert (server_skill_dir / s).is_dir()
        assert (server_skill_dir / s / "SKILL.md").is_file()
        assert (server_skill_dir / s / "SKILL.md").read_text(encoding="utf-8") == f"# {s}"


def test_client_legacy_skill_dir_migration_pure_client(tmp_path: Path):
    """验证纯客户端电脑升级时，自动将老 skill/ 完整迁移至 client_skill/，100% 保留用户手改。"""
    xskill_home = tmp_path / ".xskill"
    legacy_skill_dir = xskill_home / "skill"
    client_skill_dir = xskill_home / "client_skill"

    legacy_skill_dir.mkdir(parents=True, exist_ok=True)
    # 模拟老客户端在 skill/ 目录下有已安装的技能与用户手改
    my_skill = legacy_skill_dir / "user-custom-skill"
    my_skill.mkdir()
    (my_skill / "SKILL.md").write_text("# My Custom Prompt", encoding="utf-8")
    (my_skill / "local_edit.txt").write_text("local unpushed work", encoding="utf-8")

    # 构造 TeamClient (非 server 机器，无 team_server.json)
    state = ClientState(server_url="http://127.0.0.1:8000", client_id="test-client-1", join_token="token-1")
    client = TeamClient(
        state=state,
        http=None,
        skill_dir=client_skill_dir,
        cursor_path=tmp_path / "cursor.json",
        history_path=tmp_path / "history.json",
        home_root=tmp_path,
    )

    # 断言 1: client_skill/ 成功继承了全部老数据
    migrated_skill = client_skill_dir / "user-custom-skill"
    assert migrated_skill.is_dir()
    assert (migrated_skill / "SKILL.md").read_text(encoding="utf-8") == "# My Custom Prompt"
    assert (migrated_skill / "local_edit.txt").read_text(encoding="utf-8") == "local unpushed work"

    # 断言 2: 老 skill 目录已被原子移动，无残留孤儿目录
    assert not legacy_skill_dir.exists()


def test_client_legacy_skill_dir_migration_colocated_server(tmp_path: Path):
    """验证同机 Server 场景下，客户端初始化时采用复制而非移动，严格保护 Server 权威母本。"""
    xskill_home = tmp_path / ".xskill"
    server_skill_dir = xskill_home / "skill"
    client_skill_dir = xskill_home / "client_skill"

    server_skill_dir.mkdir(parents=True, exist_ok=True)
    # 标记本机为 Server
    (xskill_home / "team_server.json").write_text("{}", encoding="utf-8")

    # 模拟 Server 权威库中包含 3 个生产技能
    for i in range(1, 4):
        s_dir = server_skill_dir / f"server-prod-skill-{i}"
        s_dir.mkdir()
        (s_dir / "SKILL.md").write_text(f"# Prod {i}", encoding="utf-8")

    # 构造 TeamClient
    state = ClientState(server_url="http://127.0.0.1:8000", client_id="test-client-1", join_token="token-1")
    client = TeamClient(
        state=state,
        http=None,
        skill_dir=client_skill_dir,
        cursor_path=tmp_path / "cursor.json",
        history_path=tmp_path / "history.json",
        home_root=tmp_path,
    )

    # 断言 1: Server 权威库原位保留，文件数与内容绝对不变！
    for i in range(1, 4):
        s_dir = server_skill_dir / f"server-prod-skill-{i}"
        assert s_dir.is_dir()
        assert (s_dir / "SKILL.md").read_text(encoding="utf-8") == f"# Prod {i}"

    # 断言 2: Client 副本成功复制了初始技能，且两者物理路径完全独立
    for i in range(1, 4):
        c_dir = client_skill_dir / f"server-prod-skill-{i}"
        assert c_dir.is_dir()
        assert (c_dir / "SKILL.md").read_text(encoding="utf-8") == f"# Prod {i}"


def test_client_legacy_skill_dir_migration_idempotent(tmp_path: Path):
    """验证当 client_skill/ 已经有数据时，迁移逻辑保持幂等，绝不覆盖已有数据。"""
    from xskill.config import migrate_legacy_client_skill_dir
    xskill_home = tmp_path / ".xskill"
    legacy_dir = xskill_home / "skill"
    client_dir = xskill_home / "client_skill"

    legacy_dir.mkdir(parents=True, exist_ok=True)
    client_dir.mkdir(parents=True, exist_ok=True)

    (legacy_dir / "old-skill").mkdir()
    (client_dir / "current-active-skill").mkdir()

    migrated = migrate_legacy_client_skill_dir(target_client_dir=client_dir, xskill_home=xskill_home)
    assert not migrated
    assert (client_dir / "current-active-skill").exists()
    assert not (client_dir / "old-skill").exists()

