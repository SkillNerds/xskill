# Design Doc — 用 ToolContext 取代 _ctx/_ctx_v2 全局上下文

- 日期：2026-06-25
- 状态：设计稿（待 review）→ 实现计划：`docs/superpowers/plans/2026-06-25-toolcontext-refactor.md`（待写）
- 分支：拟新建 `refactor/toolcontext`（基于 `feat/anti-trajjam-rebuild`；本重构是 Mode B（尤其 B5 map-reduce 并行 leaf）的前置清理）
- 关联：`src/xskill/agents/skill_tools.py`、`agno_factory.py`、`pipeline/runner.py`、`core.py`、`api/app.py`、`team/server/api.py`、`skill/repo.py`

## 0. 问题：两套半全局上下文 = 未做完的迁移（熵增）

`skill_tools.py` 用**模块级可变全局**给 agent 工具喂上下文，且分裂成两套半：

- `_ctx`（v1，`skill_tools.py:26`）：`skill_dir / data_dir / llm_client / embed_client / config`，由 `init_context(...)` 填。
- `_ctx_v2`（`skill_tools.py:37`）：`skill_dir / store / embed_client / traj_root`，由 `init_context_v2(...)` 填。其注释自陈："**Kept separate from `_ctx` so legacy callers don't accidentally read stale fields during Task 3/4 transition**"——一次**没做完的迁移**，v2 后贴、v1 没删。
- `team/server/api.py` 还有第三条 `init_team_context` 往同一类模块级单例注入。

工具（`read_file`/`write_file`/`skill_read`/`atom_task_read`/…）**不带参数说自己要操作哪个 skill，而是低头读这些全局**（如 `read_file` 的可读根 = `_ctx["skill_dir"].parent`，`skill_tools.py:191`；`list_files` 在 `_ctx_v2` 与 `_ctx` 间回退，`:762`）。后果：

1. **隐形耦合 + 静默错**：工具行为取决于"上次谁 init 过"这个看不见的全局。Mode A 实证：测试把 `init_context_v2(skill_dir=sd)` 指错根，`commit_update_main` **不报错、静默空转**，main 没动，终审才挖出。
2. **不可并发**：一个全局 → 两个 agent / 两个 map-reduce leaf 不能同时跑（会互相覆盖 ctx），逼着串行。这正是 Mode B §B5 那条"leaf 串行"限制的根。
3. **v1/v2 双份要同步**：两份都存 `skill_dir`，调用方得同时 init 两套并保持一致（runner 确实两个都调，`runner.py:340/346`）——违背 CLAUDE.md「source 唯一、不熵增」。
4. **测试脆**：测试得摆全局态，且跨测试泄漏（`tests/test_description_opt_e2e.py:23` 明确注释"会污染模块级 `_ctx`"）。

## 1. 目标与范围

**行为保持的纯架构重构**：把三条全局上下文（`_ctx`/`_ctx_v2`/`init_team_context`）替换为**一个绑定式 `ToolContext`**，工具改为**闭包捕获各自的 ctx**，彻底去全局。

**目标：**
- 单一 `ToolContext`（不可变 dataclass），字段 = 三套并集。
- `build_skill_tools(ctx) -> list[callable]` 工厂：返回**闭包**捕获 `ctx` 的工具，每个 agent 构造自己的 ctx + 工具集 → **parallel-safe**。
- 删除 `_ctx`、`_ctx_v2`、`init_context`、`init_context_v2`、`init_team_context`——**不留 shim**（CLAUDE.md：手动迁移 + 新代码，source 唯一）。

**非目标：**
- 不改任何**对外行为/输出**（蒸馏结果、skill 内容、API 响应一字不变）——纯内部重构，由全量回归守门。
- 不改 agno model 包装链（`build_chat_model` / rate_limit / retry / trace 不动）。
- 不在本重构里实现 Mode B 的并行 leaf——本重构只**解锁**并行能力；是否并行由 Mode B §B5 决定。
- 不动 `skill/git.py` 的 commit 原语（它们不读全局）。

## 2. 设计

### 2.1 ToolContext（不可变 dataclass）
```python
@dataclass(frozen=True)
class ToolContext:
    skill_dir: Path | None = None      # skill 仓根
    store: Any = None                  # AtomTaskStore（v2 工具用）
    traj_root: Path | None = None      # 轨迹根（atom_task_read/read_traj 用）
    embed_client: Any = None           # 向量检索/索引
    llm_client: Any = None             # description 优化等用
    config: dict = field(default_factory=dict)
    data_dir: Path | None = None       # 旧 v1 检索路径（search_similar_trajs 等）
```
字段是三套全局的并集；不同工具用各自需要的子集；缺失字段为 `None`，工具用到却为 `None` 时**抛错**（fail-loud，不兜底）。

### 2.2 工具改闭包工厂
现状：~15 个模块级工具函数读全局；agent 构造时传 `tools=[ST.read_file, ST.skill_read, …]`（纯函数引用，`skill_edit_agent.py` / `task_cluster_agent.py`），由 `make_default_factory` 的 `factory(*, instructions, tools)` 包进 agno `Agent`。

目标：
```python
def build_skill_tools(ctx: ToolContext) -> list:
    """返回绑定 ctx 的工具闭包列表。agno 看到的仍是普通 callable。"""
    def read_file(path: str) -> str:
        root = ctx.skill_dir.parent
        ...
    def skill_read(skill_name: str) -> str:
        ...
    # … 其余工具同构，全部 close over ctx，不读任何全局
    return [read_file, skill_read, atom_task_read, read_traj, write_file,
            list_files, commit_baby_to_main, commit_to_staging,
            commit_update_main, …]   # 由调用方按场景挑子集
```
- agno 工具是普通 callable（`agno_factory.py:324` `Agent(tools=tools)`），闭包完全兼容，**不依赖 agno 内部 state/dependency 机制**（与本仓"不耦合 agno 版本"一致）。
- 每个 agent 在构造前 `ctx = ToolContext(skill_dir=…, store=…, …)` → `tools = build_skill_tools(ctx)`（或按场景挑子集）→ `factory(instructions=…, tools=tools)`。**两个 agent / 两个 leaf 各持各的 ctx 闭包，天然并发安全。**

### 2.3 非 agent 的直接调用者
`rebuild_skill_index` / `search_skills` 等被 `api/app.py`、`SkillRepo` **直接调用**（非经 agent）。这些改为**显式接收 ctx 或其字段**（`SkillRepo.rebuild_index` 已有"显式 kwarg、绕开 init_context"的先例，`skill/repo.py:79`）——统一成显式传参，删掉对全局的回退。

### 2.4 删除项
`_ctx`、`_ctx_v2`、`init_context`、`init_context_v2`、`init_team_context` 全删；team server 的注入改为构造 `ToolContext` 传入。

## 3. 影响面（迁移清单）

| 文件 | 改动 |
| --- | --- |
| `agents/skill_tools.py` | 删 3 全局 + 2 init 函数；~15 工具改 `build_skill_tools(ctx)` 闭包；直接调用工具改显式 ctx |
| `agents/skill_edit_agent.py` | `_run` / `_check_*` 构造 `ToolContext` + `build_skill_tools`，删 `init_context*` 调用（含 Mode B 的 leaf/merge 场景天然受益） |
| `agents/task_cluster_agent.py` | 同上（cluster 工具集） |
| `pipeline/runner.py` | 4 处 `init_context*`（`:340/346/1250/1326`）改构造 ctx + build_tools |
| `core.py` / `api/app.py` | `init_context` 调用（daemon 启动 / api_reindex）改显式 ctx |
| `team/server/api.py` | `init_team_context` 改构造 `ToolContext` |
| `skill/repo.py` | 已半显式，统一到 ctx |
| `tests/*`（~10 文件） | 各自构造 `ToolContext` 取代 `init_context*`（消除全局污染注释里点名的脆点） |

## 4. 迁移策略：干净大改，但分阶段保绿

终态**无 shim**；过程按"每步保绿"的 TDD 分解（中间态短暂并存新工厂与旧全局，仅作迁移脚手架，最后一 task 删净）：

1. **引入** `ToolContext` + `build_skill_tools(ctx)`（与旧全局并存），含工厂单测 + **并发安全测试**（两个 ctx 闭包互不串扰）。
2. **迁移 agent 调用点**（skill_edit / task_cluster / runner）到 build_tools，删其 `init_context*`。
3. **迁移直接调用者**（api/app、core、SkillRepo、team）。
4. **迁移测试**到自建 ToolContext。
5. **删除** `_ctx/_ctx_v2/init_context/init_context_v2/init_team_context`（此时已无引用）+ 全量回归。

## 5. 测试

- **工厂单测**：`build_skill_tools(ctx)` 返回的 `read_file/skill_read/...` 真按 `ctx.skill_dir` 等读写；缺字段（None）用到即抛。
- **并发安全测试**（核心收益验证）：构造两个不同 `skill_dir` 的 ctx → 各 build 一套工具 → 交错调用，断言**各读各的、零串扰**（这是旧全局做不到、本重构要证明的）。
- **行为保持**：`make test` 全量回归绿（除已知 canary e2e 环境 flake，见 anti-trajjam ledger）；重点跑 `test_skill_edit_agent / test_skill_tools_atom / test_multi_atom_store / test_description_opt_e2e / test_skill_repo`。
- 删除 task 后再跑一次全量，确认无残留全局引用（`grep -rn "_ctx\b\|init_context" src` 仅剩 ToolContext/build_skill_tools）。

## 6. 任务分解（→ writing-plans 展开为 TDD 步骤）

Task 1 ToolContext + build_skill_tools + 并发安全测试 ／ Task 2 迁移 agent 调用点 ／ Task 3 迁移直接调用者 + team ／ Task 4 迁移测试 ／ Task 5 删全局 + 全量回归。

**风险**：纯机械但面广；唯一语义风险是"某工具漏掉某字段导致 None 抛错"——靠 §5 的工厂单测 + 全量回归兜住。Mode B §B5 的"leaf 串行/别擅自重构全局"护栏在本重构落地后**作废**（leaf 可直接并行，各持 ctx）。
