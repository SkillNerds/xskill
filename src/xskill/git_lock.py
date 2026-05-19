"""
git_lock.py — Skill 仓库 git 操作
════════════════════════════════════
封装 git 命令 + skill 仓库的初始化模板。

v2 (AtomTask 流水线) 引入 baby 分支：ClusterAgent 调 new_skill_folder 时
创建 skill 目录后立刻 ``git init`` + ``checkout -b baby`` + 首次 commit
（含 stub SKILL.md 和 .gitignore），让分支状态成为"该 skill 的 state"
单一事实源——避免 .meta.yml 之类的并行元数据。

后续流转：
- baby 分支 = wip（cluster 创建但 SkillEditAgent 尚未跑过）
- SkillEditAgent 第一次跑 → 调 commit_baby_to_main(message) → baby 重命名为 main
- SkillEditAgent 后续跑 → 调 commit_to_staging(message) → 从 main 切 staging
"""

import os, subprocess, logging
from pathlib import Path

logger = logging.getLogger("git_lock")


# ═══════════════════════════════════════════════════════════════════
# 模板常量
# ═══════════════════════════════════════════════════════════════════

# v2 skill 仓库的 .gitignore 内容（new_skill_folder 时写入）
# - .candidates.yml: cluster 写入的 buffer，不版本化（candidates 不入 git 是
#   核心设计：buffer 是过程产物，commit 历史只追溯 skill 内容演化）
# - .ux_scores.jsonl: 灰度评分记录，运行时数据
# - .canary/: staging 物化目录，运行时数据
# - .lock: 旧 v1 残留
SKILL_GITIGNORE = """# xskill v2 skill 仓库的 ignore 规则
# candidates buffer 不版本化（cluster agent 高频写入，与 skill 内容演化解耦）
.candidates.yml

# 灰度运行时数据
.ux_scores.jsonl
.canary/

# 旧锁文件
.lock
"""


def run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    """Run git in *cwd*; always decode UTF-8 (Windows 默认 GBK 会在 git 输出含非 ASCII 时炸).

    subprocess 在解码失败时可能把 stdout/stderr 置为 None；调用方统一当空串处理。
    """
    r = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def init_skill_repo_on_baby(skill_dir: str, name: str, description: str) -> None:
    """v2: 初始化 skill 仓库到 baby 分支，附带 stub SKILL.md + .gitignore。

    流程：
      1. mkdir -p (如需)
      2. git init
      3. git checkout -b baby
      4. 写 .gitignore + stub SKILL.md (frontmatter 含 name/desc，body 占位)
      5. git add . + commit "init: <name>"

    SKILL.md 故意只写 frontmatter + 占位 body——cluster 阶段没有素材写
    完整内容，等 SkillEditAgent 拿 candidates 来时再补充。但 frontmatter
    完整让 ``install_to_claude_code`` 即时可读，CC 看到 stub 也能正常加载。
    """
    p = Path(skill_dir)
    p.mkdir(parents=True, exist_ok=True)
    run_git(["init"], cwd=skill_dir)
    run_git(["checkout", "-b", "baby"], cwd=skill_dir)
    run_git(["config", "user.email", "xskill@local"], cwd=skill_dir)
    run_git(["config", "user.name", "xskill"], cwd=skill_dir)

    (p / ".gitignore").write_text(SKILL_GITIGNORE, encoding="utf-8")

    from datetime import date as _date
    today = _date.today().isoformat()
    stub_md = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"metadata:\n"
        f"  version: 0\n"
        f"  state: baby\n"
        f"  created: \"{today}\"\n"
        f"  last_updated: \"{today}\"\n"
        f"  source_atoms: []\n"
        f"---\n"
        f"\n"
        f"# {name}\n"
        f"\n"
        f"(placeholder — SkillEditAgent 在 candidates 攒满阈值后会用真实 atom 内容填充正文)\n"
    )
    (p / "SKILL.md").write_text(stub_md, encoding="utf-8")

    (p / "scripts").mkdir(exist_ok=True)
    (p / "references").mkdir(exist_ok=True)
    (p / "scripts" / ".gitkeep").write_text("", encoding="utf-8")
    (p / "references" / ".gitkeep").write_text("", encoding="utf-8")

    run_git(["add", "."], cwd=skill_dir)
    run_git(["commit", "-m", f"init({name}): baby branch with stub SKILL.md"],
            cwd=skill_dir)
    logger.info(f"🌱 init skill on baby branch: {skill_dir}")


def commit_baby_to_main_branch(skill_dir: str, message: str) -> bool:
    """SkillEditAgent 调用：将 baby 分支提升为 main。

    流程：
      1. git add . + commit -m <message>  （在当前 baby 上）
      2. git branch -m baby main           （重命名 baby → main）

    返回 True 表示成功 graduate。
    """
    cur = current_branch(skill_dir)
    if cur != "baby":
        logger.warning(f"commit_baby_to_main_branch 拒绝：当前不在 baby (在 {cur})")
        return False
    run_git(["add", "-A"], cwd=skill_dir)
    code, _, err = run_git(["commit", "-m", message], cwd=skill_dir)
    if code != 0 and "nothing to commit" not in err:
        logger.warning(f"baby commit 失败: {err}")
        return False
    # 即便 nothing to commit，仍然 graduate 分支名（baby → main）
    code, _, err = run_git(["branch", "-m", "baby", "main"], cwd=skill_dir)
    if code != 0:
        logger.warning(f"branch rename baby → main 失败: {err}")
        return False
    logger.info(f"🎓 baby → main graduated: {Path(skill_dir).name}: {message}")
    return True


def commit_to_staging_branch(skill_dir: str, message: str) -> bool:
    """SkillEditAgent 调用：从 main 切 staging 分支并提交灰度候选。

    流程：
      1. 校验当前在 main 且不存在 staging
      2. git checkout -b staging (从当前 main HEAD)
      3. git add . + commit -m <message>
      4. 物化 staging SKILL.md 到 ``<skill_dir>/../.canary/<name>/`` 让
         install_to_claude_code(side='staging') 能装得到

    返回 True 表示成功创建 staging 候选。
    """
    p = Path(skill_dir)
    cur = current_branch(skill_dir)
    if cur != "main":
        logger.warning(f"commit_to_staging_branch 拒绝：当前不在 main (在 {cur})")
        return False
    code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=skill_dir)
    if code == 0:
        logger.warning(f"commit_to_staging_branch 拒绝：staging 已存在（灰度中）")
        return False
    code, _, err = run_git(["checkout", "-b", "staging"], cwd=skill_dir)
    if code != 0:
        logger.warning(f"checkout -b staging 失败: {err}")
        return False
    run_git(["add", "-A"], cwd=skill_dir)
    code, _, err = run_git(["commit", "-m", message], cwd=skill_dir)
    if code != 0 and "nothing to commit" not in err:
        logger.warning(f"staging commit 失败: {err}")
        run_git(["checkout", "main"], cwd=skill_dir)
        run_git(["branch", "-D", "staging"], cwd=skill_dir)
        return False
    # 物化 staging 内容到 .canary/<name>/，给 install_to_claude_code(side='staging')
    # 用。在 staging 分支上直接拷 SKILL.md 等文件——比从 git tree 抽更可靠
    # 也不需要 worktree。
    from xskill.canary import materialize_staging
    canary_root = p.parent / ".canary"
    try:
        materialize_staging(p, canary_root)
    except Exception:
        logger.exception(f"materialize_staging 失败: {Path(skill_dir).name}")
    # 回到 main，让后续 cluster / SkillEdit 默认在 main 上工作
    run_git(["checkout", "main"], cwd=skill_dir)
    logger.info(f"🚦 staging candidate committed: {Path(skill_dir).name}: {message}")
    return True


def ensure_repo(skill_dir: str):
    """确保 skill_dir 是一个 git 仓库，在 main 分支上"""
    p = Path(skill_dir)
    p.mkdir(parents=True, exist_ok=True)
    if not (p / ".git").exists():
        run_git(["init"], cwd=skill_dir)
        run_git(["checkout", "-b", "main"], cwd=skill_dir)
        run_git(["config", "user.email", "xskill@local"], cwd=skill_dir)
        run_git(["config", "user.name", "xskill"], cwd=skill_dir)
        (p / ".gitkeep").touch()
        (p / ".gitignore").write_text(
            "# canary runtime data — NOT versioned\n.ux_scores.jsonl\n.lock\n",
            encoding="utf-8",
        )
        run_git(["add", "."], cwd=skill_dir)
        run_git(["commit", "-m", "init skill repo"], cwd=skill_dir)
        logger.info(f"初始化 skill git 仓库: {skill_dir}")
    else:
        # 确保在 main 上
        _, cur, _ = run_git(["branch", "--show-current"], cwd=skill_dir)
        if cur != "main":
            run_git(["checkout", "--", "."], cwd=skill_dir)
            run_git(["clean", "-fd"], cwd=skill_dir)
            code, _, _ = run_git(["checkout", "main"], cwd=skill_dir)
            if code != 0:
                run_git(["checkout", "-b", "main"], cwd=skill_dir)
                (p / ".gitkeep").touch()
                run_git(["add", "."], cwd=skill_dir)
                run_git(["commit", "--allow-empty", "-m", "init"], cwd=skill_dir)
            if cur:
                run_git(["branch", "-D", cur], cwd=skill_dir)
            logger.info(f"🧹 修复: 回到 main，清理残留分支 {cur}")


def has_changes(skill_dir: str) -> bool:
    code, out, _ = run_git(["status", "--porcelain"], cwd=skill_dir)
    return bool(out)


def commit_changes(skill_dir: str, message: str) -> bool:
    run_git(["add", "-A"], cwd=skill_dir)
    code, out, _ = run_git(["diff", "--cached", "--name-only"], cwd=skill_dir)
    if not out and not has_changes(skill_dir):
        return False
    code, _, err = run_git(["commit", "-m", message], cwd=skill_dir)
    if code == 0:
        logger.info(f"📝 commit: {message}")
        return True
    logger.warning(f"commit 失败: {err}")
    return False


def current_branch(skill_dir: str) -> str:
    _, out, _ = run_git(["branch", "--show-current"], cwd=skill_dir)
    return out


def is_on_main(skill_dir: str) -> bool:
    return current_branch(skill_dir) == "main"


