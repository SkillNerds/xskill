"""UserEditAbsorbAgent —— 把用户对 ~/.claude/skills/<name>/ 的手改吸回 main
================================================================================

场景：因为 install_to_claude_code 用 symlink，用户改 ``~/.claude/skills/<name>/``
下任何文件实际改的是 xskill 源仓库。watcher 周期性检测：

- 任一文件（SKILL.md / scripts/* / references/* 或新增目录）mtime 超过该
  skill 最近一次 git commit 时间
- 且距 ``now ≥ 3 分钟``（避免用户编辑过程被误触）

满足 → 触发本 agent：读 git diff → LLM 写 commit message → 直接 commit
到 main → 如果存在 staging 一并删除（用户手改优先级压过灰度候选）。

行为上"用户手改"被视为 ground truth：
- 不走灰度（用户已经验证过了才会去手改）
- 不区分 baby/main：手改情况下都强制 commit 到 main（baby 也提前 graduate）
- 清空 candidates buffer（手改可能包含了原本要靠 cluster 攒的内容）
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from xskill.skill import candidates as C

logger = logging.getLogger("xskill.user_edit_absorb_agent")

# 用户停止编辑的最小静默时间——避免编辑过程被中途回流
USER_EDIT_QUIET_SECONDS = 180


SYSTEM_PROMPT = """你是 UserEditAbsorbAgent。某个 skill 的源文件被用户**手动**
修改了（通过 ``~/.claude/skills/<name>/`` 改的，因为 symlink 链接到我们的源仓
库）。你的任务是把用户改动作为 ground truth 吸收回 main 分支。

# 输入
我会给你：
  - skill_name（slug）
  - 该 skill 当前所在 git 分支
  - ``git diff`` 的完整内容（显示用户改了什么）

# 目标
1. 看 diff 判断用户改了什么（增 SKILL.md 内容？新加 scripts？删除某段？）
2. 调 ``absorb_user_edit_to_main(skill_name, message)`` 工具吸收：
   - 工具内部会：git add . + commit -m <message> + (若 baby) rename baby→main
     + (若有 staging) git branch -D staging
   - 你不需要操作 git，只需写一个合理的 commit message

# commit message 格式
``absorb user edit: <一句话总结用户改了什么>``，例：
  - ``absorb user edit: 用户在 ## 验证 阶段加了 lsof 命令``
  - ``absorb user edit: 用户删除了过时的 references/legacy.md``

# 硬禁止
- 不要在 diff 之外推断用户意图——用户改什么就吸什么
- 不要试图"修正"用户的改动——他们是 ground truth
- 不要调任何写文件工具：用户已经写完了，你只需 commit
"""


@dataclass
class UserEditAbsorbAgent:
    """每实例服务一个具体 skill；watcher 检测到手改触发。"""
    skill_dir: Path
    agno_agent_factory: Callable[..., Any]
    llm_cfg: dict

    def run(self) -> bool:
        """跑一次 absorb：读 diff → agent 给 message → commit + 清 candidates。

        返回 True 表示成功 commit（agent 调过 absorb 工具）；False 表示
        diff 为空 / agent 没 commit。
        """
        from xskill.skill.git import run_git

        # 读 diff 让 agent 看
        code, diff_out, _ = run_git(["diff", "HEAD"], cwd=str(self.skill_dir))
        # 也加 untracked 文件 (status 看 untracked 数量)
        _, status_out, _ = run_git(["status", "--porcelain"], cwd=str(self.skill_dir))
        if not diff_out and not status_out:
            return False

        from xskill.agents import skill_tools as ST
        skill_name = self.skill_dir.name

        # 构造 user_msg：含 skill_name + 当前分支 + 完整 diff + 未追踪文件列表
        from xskill.skill.git import current_branch
        cur_branch = current_branch(str(self.skill_dir))
        user_msg_parts = [
            f"skill_name: {skill_name}",
            f"current_branch: {cur_branch}",
            "",
            "# git status (含未追踪):",
            status_out or "(no untracked)",
            "",
            "# git diff HEAD（已追踪文件的改动）:",
            (diff_out[:8000] if diff_out else "(no tracked file changes)"),
        ]
        user_msg = "\n".join(user_msg_parts)

        agent = self.agno_agent_factory(
            instructions=[SYSTEM_PROMPT],
            tools=[ST.absorb_user_edit_to_main],
        )
        try:
            agent.run(user_msg)
        except Exception:
            logger.exception("UserEditAbsorbAgent failed: %s", skill_name)
            return False

        # 检查是否成功 commit（最新 commit message 含 "absorb user edit"）
        _, last_msg, _ = run_git(["log", "-1", "--format=%s"], cwd=str(self.skill_dir))
        if "absorb user edit" not in last_msg.lower():
            logger.warning(
                "UserEditAbsorbAgent ran but no absorb commit landed: %s "
                "(last commit: %r)",
                skill_name, last_msg[:120],
            )
            return False
        # 清 candidates buffer——手改是 ground truth，原本 buffer 里的素材
        # 价值已经被人工版本超越
        C.clear_candidates(self.skill_dir)
        logger.info("UserEditAbsorbAgent absorbed user edit: %s", skill_name)
        return True


def _max_workspace_mtime(skill_dir: Path) -> float:
    """扫该 skill 工作区所有相关文件 + 目录的 max(mtime)。

    跳过 git 内部 / candidates buffer / canary 物化 / ux 评分——这些是
    daemon 自己的运行时产物，不算"用户手改"。无相关文件返回 0.0。
    """
    max_mtime = 0.0
    for p in skill_dir.rglob("*"):
        try:
            rel = p.relative_to(skill_dir)
            parts = rel.parts
            if not parts:
                continue
            if parts[0] == ".git":
                continue
            if parts[0] in (".candidates.yml", ".canary", ".ux_scores.jsonl"):
                continue
            m = p.stat().st_mtime
            if m > max_mtime:
                max_mtime = m
        except (OSError, ValueError):
            continue
    return max_mtime


def has_pending_user_edit(skill_dir: Path) -> bool:
    """该 skill 工作区有未 commit 的用户手改（不管静默多久）。

    = ``detect_user_edits`` 的判据 (a)，去掉静默检查 (b)。

    判据：
    - 取 SKILL.md / scripts/** / references/** 等所有非 .git / 非
      .candidates.yml 文件的 max(mtime)
    - 该 mtime 比最近一次 git commit 时间严格大 ≥1 秒 → 有未 commit 改动

    ``git log --format=%ct`` 返回**整数秒**（Unix ts truncate 掉小数部分），
    ``os.stat().st_mtime`` 返回**浮点秒**。同一秒内"write file → git commit"
    时，file mtime = N.XXX 而 commit_ts = N → 浮点差 0.X 秒，会被误判为
    "用户编辑了文件"。要求 mtime 比 commit_ts 严格大 ≥1 秒才算真的编辑。
    """
    from xskill.skill.git import run_git

    if not (skill_dir / ".git").is_dir():
        return False

    # last commit timestamp
    code, ts_out, _ = run_git(
        ["log", "-1", "--format=%ct", "HEAD"], cwd=str(skill_dir),
    )
    if code != 0 or not ts_out.strip():
        return False
    try:
        last_commit_ts = float(ts_out.strip())
    except ValueError:
        return False

    max_mtime = _max_workspace_mtime(skill_dir)
    # 见 docstring：要求 mtime 比 commit_ts 严格大 ≥1 秒才算真的编辑。
    return max_mtime - last_commit_ts >= 1.0


def detect_user_edits(skill_dir: Path, *, quiet_seconds: int = USER_EDIT_QUIET_SECONDS) -> bool:
    """检测该 skill 是否有用户手改且已稳定 (>=3 分钟没新动作)。

    判据：
    - (a) ``has_pending_user_edit``：有未 commit 改动
    - (b) ``now - max_mtime ≥ quiet_seconds`` → 用户已停止编辑 ≥3 分钟

    两个都过才返回 True（表示该跑 absorb）。
    """
    import time

    if not has_pending_user_edit(skill_dir):
        return False
    max_mtime = _max_workspace_mtime(skill_dir)
    if (time.time() - max_mtime) < quiet_seconds:
        return False  # 改动太新，可能用户还在编辑
    return True


# ──────────────────────────────────────────────────────────────────
# dest → source 回流桥（通用 copy-mode install 回流）
# ──────────────────────────────────────────────────────────────────
#
# 凡是 ``ecosystems._fallback.install_dir`` 走到 copy 路径的生态（dest 是
# 真目录，跟源仓解耦），用户改 dest 都不会被 absorb agent 看到——absorb
# 走的是源仓 mtime。本模块的 ``reverse_sync_copy_dest`` 把 dest 用户改
# 灌回源仓，让 source mtime 看起来"刚被改过"，后续原有 absorb / push-edit
# 链路就能像处理普通源仓改动一样收编。
#
# 历史背景：openclaw 是第一个被迫走 copy 的生态（openclaw discovery 对
# 非 bundled 档做 realpath 检查，symlink 跑出 root 会被拒）；ngagent 在
# Windows non-DevMode 下也撞同样问题（Node.js Dirent 把 junction 当
# symlink 不当目录看，详见 issue #34），所以把这套机制泛化给所有 copy
# 模式的生态用。
#
# install-meta 由 ``_fallback._write_install_meta`` 统一写到
# ``dest.parent / .xskill-install-meta-<dest.name>.json``。本模块也兼容
# 读取 **dest 内部** 的老 ``.xskill-install-meta.json``（openclaw 旧版位置）
# ——新装的 skill 都已是新位置，老路径只为存量 dest 提供平滑过渡，不应
# 有新代码再往 dest 内部写 meta。

# openclaw 旧位置：写在 dest 内部（保留以兼容存量装出去的 dest）
_OPENCLAW_INSTALL_META = ".xskill-install-meta.json"

# 默认 reverse_sync exclude：``.git`` + 兼容 openclaw 老位置 meta（dest 内部）。
# 新位置 meta 在 ``dest.parent`` 旁边，``dest.rglob("*")`` 扫不到，无需 exclude。
# 老位置在 ``dest/.xskill-install-meta.json``——只要 dest 还可能存在老 meta
# （存量 openclaw 装好的 dest 升级前都是老位置），就必须在所有 helper 的默认
# exclude 里把它排掉，否则 install 时重写 meta 会触发误判 "用户改了 dest"。
_DEFAULT_REVERSE_SYNC_EXCLUDE = frozenset({".git", _OPENCLAW_INSTALL_META})


def _new_install_meta_path(dest_dir: Path) -> Path:
    """新位置 meta：``dest.parent / .xskill-install-meta-<dest.name>.json``。

    与 ``ecosystems._fallback._install_meta_path`` 同源；这里复刻一份
    避免 user_edit_absorb_agent ↔ ecosystems 的循环 import。
    """
    return dest_dir.parent / f".xskill-install-meta-{dest_dir.name}.json"


def _read_install_meta_ts(dest_dir: Path) -> Optional[float]:
    """读 dest install-meta 的 installed_at（epoch 秒）；读不到返回 None。

    优先读**新位置**（``dest.parent`` 旁边的 ``.xskill-install-meta-<name>.json``）；
    新位置缺失才退到 openclaw 旧位置（``dest/.xskill-install-meta.json``）
    做兼容——存量 openclaw dest 升级前还是老位置；新装的统一在新位置。
    """
    for meta_path in (_new_install_meta_path(dest_dir),
                      dest_dir / _OPENCLAW_INSTALL_META):
        if not meta_path.is_file():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        ts = data.get("installed_at")
        if isinstance(ts, (int, float)):
            return float(ts)
    return None


def _dest_user_edit_mtime(dest_dir: Path, exclude: frozenset[str]) -> float:
    """扫 dest 下所有用户内容文件的 max mtime，跳过 exclude 第一段。

    .git 跳过——dest 理论上不该有 .git，但万一有（用户自己 git init）也别
    把 git 内部状态当用户改算。
    """
    max_mtime = 0.0
    for p in dest_dir.rglob("*"):
        try:
            rel = p.relative_to(dest_dir)
            parts = rel.parts
            if not parts:
                continue
            if parts[0] in exclude:
                continue
            m = p.stat().st_mtime
            if m > max_mtime:
                max_mtime = m
        except (OSError, ValueError):
            continue
    return max_mtime


def has_pending_dest_edit(
    dest_dir: Path, *,
    quiet_seconds: int = USER_EDIT_QUIET_SECONDS,
    exclude: frozenset[str] = _DEFAULT_REVERSE_SYNC_EXCLUDE,
) -> bool:
    """dest 里是否有用户手改、且静默时间已过 quiet_seconds？

    判据：
    - dest 存在
    - 能读到 install-meta 里的 installed_at
    - dest 某文件 mtime > installed_at + 1 秒（用户改过）
    - now - max_mtime >= quiet_seconds（停手 ≥3 分钟）
    """
    if not dest_dir.is_dir():
        return False
    installed_at = _read_install_meta_ts(dest_dir)
    if installed_at is None:
        return False
    max_mtime = _dest_user_edit_mtime(dest_dir, exclude)
    if max_mtime - installed_at < 1.0:
        return False
    if (time.time() - max_mtime) < quiet_seconds:
        return False
    return True


def reverse_sync_copy_dest(
    dest_dir: Path, source_dir: Path,
    *,
    exclude: frozenset[str] = _DEFAULT_REVERSE_SYNC_EXCLUDE,
    quiet_seconds: int = USER_EDIT_QUIET_SECONDS,
) -> bool:
    """通用 copy-mode 回流：把 dest 用户改灌回 source。

    任何 ``install_dir`` 走到 copy 路径的生态都能用（openclaw / ngagent /
    其它落到 copy fallback 的）。``exclude`` 默认只排 ``.git``；调用方
    可以加更多（如 openclaw 兼容路径加 ``_OPENCLAW_INSTALL_META``）。

    返回 True 表示真有内容被灌回去（这一轮 watcher 下一步应该看到 source
    有 pending edit）；False 表示 dest 没改 / 还在静默期 / 出错跳过。

    流程：
    1. ``has_pending_dest_edit`` 检查（dest 有改且静默 ≥quiet_seconds）
    2. 抢源仓 ``skill_repo_lock``——跟 CC absorb / canary flip 用同一把锁
    3. 遍历 dest 文件（跳 exclude 第一段路径），对每个文件 copy 到 source
       对应路径（覆盖；新文件自动 mkdir）
    4. 留意：**不删** source 里 dest 没有的文件（避免误删源仓里 ``.canary``
       等 xskill 自己产物；用户要删请在源仓直接删）
    5. touch source 里 SKILL.md 的 mtime（让 ``_max_workspace_mtime`` 下一轮
       看到 source 有 pending edit）

    并发：copy 期间 dest 也可能被 install 重新覆盖 / 用户继续改。锁只保护
    source 的一致性。dest 在 copy 中途变了，最坏情况是漏掉这次新改动，
    下一轮 watcher 再扫到再回流。
    """
    # 默认 exclude 已含 openclaw 老位置 meta + .git；调用方传别的就用它们。
    full_exclude = frozenset(exclude)
    if not has_pending_dest_edit(
        dest_dir, quiet_seconds=quiet_seconds, exclude=full_exclude,
    ):
        return False

    from xskill.skill.git import skill_repo_lock

    skill_name = source_dir.name
    logger.info("reverse_sync_copy_dest start: %s (dest=%s → source=%s)",
                skill_name, dest_dir, source_dir)

    with skill_repo_lock(source_dir):
        touched_any = False
        for src_file in dest_dir.rglob("*"):
            try:
                rel = src_file.relative_to(dest_dir)
            except ValueError:
                continue
            parts = rel.parts
            if not parts or parts[0] in full_exclude:
                continue
            if src_file.is_dir():
                continue
            dst_file = source_dir / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)  # copy2 保 mtime
            touched_any = True

        if touched_any:
            # touch source 让 watcher 下一轮看见 (max_mtime > last_commit_ts ≥ 1s)
            (source_dir / "SKILL.md").touch()

    logger.info("reverse_sync_copy_dest done: %s (synced=%s)",
                skill_name, touched_any)
    return touched_any


def reverse_sync_openclaw_dest(
    dest_dir: Path, source_dir: Path,
    *, quiet_seconds: int = USER_EDIT_QUIET_SECONDS,
) -> bool:
    """已弃用别名，新代码用 ``reverse_sync_copy_dest``。

    保留这个名字给外部调用方（team/client/daemon.py、pipeline/runner.py、
    openclaw.py）平滑迁移。语义与默认参数的 ``reverse_sync_copy_dest`` 等价
    （默认 exclude 已含 openclaw 老位置 meta）。
    """
    return reverse_sync_copy_dest(
        dest_dir, source_dir,
        quiet_seconds=quiet_seconds,
    )
