"""SkillEditAgent —— SKILL.md 自主整理 + git commit（baby→main 或 staging）
========================================================================

何时触发（``maybe_run`` 守门）：
  1. 该 skill 没有 staging 分支（灰度中不再触发新 SkillEdit）
  2. ``.candidates.yml`` 中所有 atom 的 ``weightscore`` 累加 ≥ 10
  3. 触发场景是"create staging"时（即 main 已存在），额外要求
     ``.ux_scores.jsonl`` 至少有 1 条 ``side=main`` 的真实评分——
     避免冷启动后 main 无人用就连开 staging 卡死灰度链路
  4. 调用方（watcher._check_pending_skill_edits）保证不在冷启动期触发

agent 写完 SKILL.md / scripts / references 后**必须**调以下两个 commit
工具之一（依当前分支状态）：
  - baby 分支：调 ``commit_baby_to_main(skill_name, message)`` 完成首版
  - main 分支：调 ``commit_to_staging(skill_name, message)`` 产出灰度候选

落盘成功（SKILL.md mtime 推进 + 非空）→ 清空 candidates buffer + 立即
install_to_claude_code 让 CC 立刻看到新版本。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from xskill.skill import candidates as C

logger = logging.getLogger("xskill.skill_edit_agent")


SYSTEM_PROMPT_TEMPLATE = """你是 SkillEditAgent。某 skill 的 candidates buffer 累计
weightscore ≥ 10，需要你产出/更新它的 SKILL.md。

# 当前场景

{scenario_block}

# 你的目标

读 atom 内容（AtomTaskRead），必要时读 traj 原文（ReadTraj），整理出一份
**完整可执行**的 skill。SKILL.md 是必产物，但你**不限于**只写 SKILL.md——
可以补充任何辅助文件，只要在 skill 目录范围内：

- ``<skill_dir>/SKILL.md`` — 必产物，frontmatter + body
- ``<skill_dir>/scripts/*.py`` / ``*.sh`` — 可机械执行的脚本
- ``<skill_dir>/references/*.md`` — 长参考材料（trace / 配置 / 文档摘录）
- ``<skill_dir>/templates/*`` 等任意子目录

# SKILL.md schema

```
---
name: <英文 slug>
description: <2-5 句中文：干什么、典型触发表述（引号引原文）、需要的工具/权限>
compatibility: <环境/版本/权限 + 至少一条负向硬约束>
metadata:
  version: <自增整数；新建从 1 开始，更新版本号+1>
  created: "<AUTO>"
  last_updated: "<AUTO>"
  source_atoms: ["atom_xxx_0001", ...]
---

# <中文标题>

<开头一段：这个 skill 解决什么，核心原则>

## <阶段名-1>
1. <步骤：精确到命令/文件/函数>
2. <步骤>

   > ⚠️ <警告：引用 atom 证据 "见 atom_xxx_0001" 或 "3/5 atoms 表明..."，
   > 不要凭空猜>
```

# 写完文件**必须 commit**

写完所有文件后**必须**调以下其中一个工具完成版本化——否则改动只是工作目录
脏文件，watcher 会判定本次 SkillEdit 失败重试。

## 当前在 {branch_now} 分支，按场景调对应工具：

- **如果在 baby 分支**：调 ``commit_baby_to_main(skill_name, message)``
  这是该 skill 第一次出版本，graduate 到 main 分支。
- **如果在 main 分支**（已有 main，本次是更新）：调
  ``commit_to_staging(skill_name, message)`` 把更新作为灰度候选放进 staging。
  staging 会被灰度系统 vs main 对比，胜出才升级。

commit message 写明本次基于哪些 atom_id 整理，例如：
``"v2: 合并 atom_traj_x_0001/0003 的 zombie cleanup 步骤；新增 references/pidns_pitfall.md"``

# 可用工具
- AtomTaskRead(atom_id) — 读 atom JSON
- ReadTraj(traj_id, offset_start, offset_end) — 按行号取轨迹原文（offset 即 1-based 行号）
- SkillRead(skill_name) — 读现有 SKILL.md（更新场景用）
- list_files(path) — 列目录文件
- write_file(path, content) — 写任意文件到 skill_dir 下
- commit_baby_to_main(skill_name, message) — 仅 baby 分支可用
- commit_to_staging(skill_name, message) — 仅 main 分支可用

# 隐私守护

source atom / traj 原文来自真实开发轨迹，即便上传时已脱敏，仍可能残留
**敏感信息**——API key、token、密码、私钥、内网地址、个人邮箱/姓名等。
整理 skill 时：

- **绝不**把这些原样抄进 SKILL.md / scripts / references 任何产物。skill 要
  沉淀的是「做法」，不是「某次跑用的具体密钥」。
- 引用命令/配置/代码时，凡凭证位置一律用占位符——``API_KEY=<your-api-key>``、
  ``--token <TOKEN>``、``password: <password>``、``ssh user@<host>``。
- 看到 ``[REDACTED]``（上传脱敏留下的）保持原样，不要试图"还原"或编一个值。
- 拿不准某串是否敏感 → 一律当敏感处理、占位符化。

# 硬禁止
- 不要随便引用没在 atom 中出现过的命令/函数；以 AtomTaskRead 为唯一可信来源
- 不要在描述里发明用户群体或场景
- SKILL.md ≤ 400 行；超过的内容拆到 references/ 或 scripts/
- 不要写 ``## trigger`` / ``## 触发条件`` / ``## pitfalls`` / ``## 陷阱`` 段——
  触发信号在 frontmatter.description，pitfalls 用 ``> ⚠️`` 内联 blockquote
- **不要自己用 write_file 写 ``.git/`` 下文件**或 ``.candidates.yml``——前者会
  破坏 git 状态，后者是 cluster 的 buffer 由系统管理
"""


@dataclass
class SkillEditAgent:
    """每个实例服务**一个**具体 skill 子目录。"""
    skill_dir: Path
    store: Any  # AtomTaskStore (only needed by atom_task_read tool indirectly)
    agno_agent_factory: Callable[..., Any]
    llm_cfg: dict
    traj_root: Path
    threshold: int = C.ATOM_PROMOTION_THRESHOLD

    def maybe_run(self) -> bool:
        """检查所有守门条件 → 触发 agent → 验证落盘 → 清 buffer。

        守门顺序（任一失败即 return False）：
          1. 该 skill 有 staging 分支 → 灰度中，不触发
          2. candidates 累计 weightscore < threshold → 没攒够
          3. 触发场景是 "create staging"（main 已存在）：
             - .ux_scores.jsonl 必须至少有 1 条 side=main → 证明 main 真有人用
             - 否则保留 candidates 等用户用过 main 再触发

        全过 → 跑 agent → 验证 SKILL.md mtime 推进 + 非空 → 清 candidates。
        agent 没落盘 SKILL.md → 保留 candidates 等下轮重试（Bug 2 修复）。
        """
        from xskill.skill.git import current_branch, run_git

        # 守门 1: 灰度中（有 staging）不触发
        code, _, _ = run_git(["rev-parse", "--verify", "staging"],
                             cwd=str(self.skill_dir))
        if code == 0:
            return False
        # 守门 2: 阈值
        data = C.load_candidates(self.skill_dir)
        ready = C.ready_for_promotion_v2(
            data, threshold=self.threshold, skill_dir=self.skill_dir,
        )
        if not ready:
            return False
        ready = self._detect_and_resolve_conflicts(ready)
        if not ready:
            return False
        # 守门 3: 若场景是 "create staging"（即在 main 上）→ 额外要求 main 真有人用过
        cur = current_branch(str(self.skill_dir))
        if cur == "main":
            if not self._main_has_ux_score():
                logger.info(
                    "skip SkillEdit: %s main 还没真实 ux_score，"
                    "保留 candidates 等用户用 main 后再产 staging",
                    self.skill_dir.name,
                )
                return False
        elif cur != "baby":
            logger.warning(
                "skip SkillEdit: %s 在异常分支 %r (期望 baby 或 main)",
                self.skill_dir.name, cur,
            )
            return False

        skill_md = self.skill_dir / "SKILL.md"
        mtime_before = skill_md.stat().st_mtime if skill_md.is_file() else 0.0
        size_before = skill_md.stat().st_size if skill_md.is_file() else 0

        try:
            self._run(ready, current_branch_name=cur)
        except Exception:
            logger.exception("SkillEditAgent _run failed: %s", self.skill_dir.name)

        # 实测落盘：mtime 推进 + 非空 = agent 真写了
        wrote = (
            skill_md.is_file()
            and skill_md.stat().st_size > 0
            and (
                skill_md.stat().st_mtime > mtime_before
                or skill_md.stat().st_size != size_before
            )
        )
        if not wrote:
            logger.warning(
                "SkillEditAgent ran but SKILL.md not written/empty: %s — "
                "保留 candidates 等下轮重试",
                self.skill_dir.name,
            )
            return False

        # commit 工具的成功效应（baby→main 或 main→staging）通过当前分支变化
        # 自然反映，不需要在这里做额外检查
        C.clear_candidates(self.skill_dir)
        logger.info("SkillEditAgent done + candidates cleared: %s",
                    self.skill_dir.name)
        return True

    def _detect_and_resolve_conflicts(self, ready: list[dict]) -> list[dict]:
        """Persist conflicts, auto-resolve obvious ones, and return safe candidates."""
        def _load(atom_id: str):
            if self.store is None:
                raise FileNotFoundError(atom_id)
            return self.store.load(atom_id)

        groups = C.detect_conflicts(
            self.skill_dir,
            ready,
            atom_loader=_load if self.store is not None else None,
        )
        hard = [g for g in groups if g.type == "hard" and not g.resolution]
        if hard:
            C.resolve_conflicts(self.skill_dir, hard)
            if C.has_unresolved_hard_conflicts(self.skill_dir):
                logger.warning(
                    "Skill %s 存在未解决的硬冲突，暂停自动更新",
                    self.skill_dir.name,
                )
                return []
        return C.filter_candidates_by_resolved_conflicts(self.skill_dir, ready)

    def _main_has_ux_score(self) -> bool:
        """检查该 skill 的 .ux_scores.jsonl 是否有至少 1 条 side=main 记录。

        冷启动后没人用过 main 时，避免立刻产生 staging 卡死灰度链路（B 守门）。
        """
        from xskill.canary import load_ux_scores
        try:
            scores = load_ux_scores(self.skill_dir)
        except Exception:
            return False
        return any(s.get("side") == "main" for s in scores)

    def _run(self, ready: list[dict], current_branch_name: str) -> None:
        from xskill.agents import skill_tools as ST
        from xskill.skill.frontmatter import parse as fm_parse

        # 构造 scenario_block + branch_now 给 prompt 用
        skill_md = self.skill_dir / "SKILL.md"
        scenario_lines: list[str] = []
        if current_branch_name == "baby":
            scenario_lines.append(
                "skill_name: " + self.skill_dir.name + "（**baby 分支**——首次出版本）"
            )
            scenario_lines.append(
                "写完 SKILL.md 后调 ``commit_baby_to_main(skill_name, message)`` "
                "graduate 到 main 分支。"
            )
        else:
            scenario_lines.append(
                "skill_name: " + self.skill_dir.name + "（**main 分支** —— 更新现有 skill）"
            )
            scenario_lines.append(
                "写完 SKILL.md 后调 ``commit_to_staging(skill_name, message)`` "
                "把更新作为灰度候选 commit 到 staging。"
            )

        # 现有 SKILL.md 是 stub (baby 时) 或正式版 (main 时)
        if skill_md.is_file():
            try:
                fm, _ = fm_parse(skill_md.read_text(encoding="utf-8"))
                cur_desc = (fm.get("description") or "").strip().replace("\n", " ")
                cur_ver = (fm.get("metadata", {}) or {}).get("version", "?")
                scenario_lines.append("")
                scenario_lines.append(f"现有 SKILL.md description: {cur_desc[:200]}")
                scenario_lines.append(f"现有 SKILL.md version: {cur_ver}")
            except Exception:
                pass
        scenario_lines.append("")
        scenario_lines.append("# 待整理 candidates（按 weightscore 倒序）")
        for c in sorted(
            ready, key=lambda x: x.get("weightscore", 0), reverse=True,
        ):
            note = c.get("note", "")
            ext = f"  note: {note}" if note else ""
            scenario_lines.append(
                f"- atom_id={c['atom_id']}  weightscore={c['weightscore']}{ext}"
            )
        scenario_lines.append("")
        soft_hints = C.soft_conflict_merge_hints(
            self.skill_dir,
            candidate_ids={str(c.get("atom_id", "")) for c in ready},
        )
        if soft_hints:
            import json as _json
            scenario_lines.append("# 冲突合并提示")
            scenario_lines.append(
                "以下 soft conflict 已由系统判定为可共存。写 SKILL.md 时不要二选一，"
                "应合并为条件分支、优先级规则或同一 section 下的不同步骤："
            )
            scenario_lines.append(
                _json.dumps(soft_hints, ensure_ascii=False, indent=2)
            )
            scenario_lines.append("")
        scenario_lines.append(f"目标 skill 目录: {self.skill_dir}")
        scenario_lines.append(f"目标 SKILL.md 路径: {skill_md}")

        scenario_block = "\n".join(scenario_lines)
        sysprompt = SYSTEM_PROMPT_TEMPLATE.format(
            scenario_block=scenario_block,
            branch_now=current_branch_name,
        )
        user_msg = scenario_block  # 同时也作为 user 消息（agno 两端都看）

        agent = self.agno_agent_factory(
            instructions=[sysprompt],
            tools=[
                ST.atom_task_read,
                ST.read_traj,
                ST.skill_read,
                ST.list_files,
                ST.write_file,
                ST.commit_baby_to_main,
                ST.commit_to_staging,
            ],
        )
        agent.run(user_msg)
