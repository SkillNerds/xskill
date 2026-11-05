# 指定技能蒸馏验证记录

日期：2026-11-05。环境：Windows、本地 Python 3.12、仓库 editable 安装。

## 范围

新增功能通过本地 native 管线实现 `create_one` 和 `update_only`，默认仍为 `auto`。验证使用临时技能库、SQLite、安装目录和假模型；没有启动个人真实蒸馏，没有改写个人历史，没有安装到个人工具目录。

重点检查固定目标权限、直接框架写入、候选批次、Git/checkpoint、灰度物化、安装副本、后台积压、运行锁、恢复、素材跳过和成品状态。新建和更新端到端用例沿用现有候选、编辑与提交流程。

## 验证结果

以下为首次实现提交 `a7f6da5` 的结果。后续独立审查发现了未被这些用例覆盖的边界缺陷，修复与重新验证见[审查问题修复记录](2026-11-05-targeted-distillation-review-fixes.md)。这些历史通过结果不代表边界限制已经完整。

定向测试最终串行运行：**151 passed, 2 skipped**，耗时 277.73 秒。两项跳过涉及当前 Windows 普通文件符号链接权限；junction、目录前缀替换、安装复制/清理竞争与进程锁用例实际执行。

```powershell
$distillationTestFiles = @(rg --files tests -g 'test_distillation*.py')
python -X utf8 -m pytest @distillationTestFiles -q --disable-warnings --maxfail=1
```

默认流程相关回归最终串行运行：**406 passed, 1 skipped, 1 deselected**，耗时 157.03 秒。此前并行验证中的并发编辑耗时断言，在本次串行回归中通过；没有放宽原断言或产品限制。

```powershell
python -X utf8 -m pytest tests/test_skill_edit_agent.py tests/test_skill_edit_batch_turns.py tests/test_skill_edit_mutation_contracts.py tests/test_skill_edit_dirty_queue.py tests/test_skilledit_parallel.py tests/test_candidates_atom.py tests/test_candidates_concurrency.py tests/test_agent_tool_context_isolation.py tests/test_agent_worker.py tests/test_agent_worker_runtime.py tests/test_pipeline_pool_hot.py tests/test_canary.py tests/test_canary_rotation.py tests/test_canary_install_atomic.py tests/test_user_edit_absorb.py tests/test_atom_candidate_pending.py tests/test_team_cli_serve.py tests/test_server_watcher_startup.py tests/test_skill_tools_atom.py tests/test_skill_tools_dotgit_guard.py tests/test_agent_tools_py39_schema.py tests/test_description_opt.py tests/test_rebuild.py tests/test_cli_serve_guard.py tests/test_status_file.py -q --disable-warnings --maxfail=1 -k "not test_move_rejects_symlink_alias_of_source_without_mutation"
```

收集过滤的一项旧用例 `test_move_rejects_symlink_alias_of_source_without_mutation` 在建立夹具符号链接时触发本机 Windows 权限错误，尚未进入产品代码；它没有被当作通过。新增 junction、硬链接、路径别名和实际写入竞争用例仍执行。默认生态安装、ledger 和 description 的另一组相关检查为 93 passed、15 skipped、4 deselected，跳过/过滤主要涉及 Windows 符号链接场景。

静态检查：新增核心模块、接入工具、runner、锁及定向测试通过 Ruff `F` 检查；155 个源码文件通过 Python 3.9 语法解析。语法解析不等于在所有 Python/平台组合运行通过。

既有 GPT-5 nano 参数兼容相关测试单独运行，27 项通过。以下三个文件的 SHA-256 与实施前一致，保留在工作区，不纳入本功能提交：

| 文件 | SHA-256 |
| --- | --- |
| `src/xskill/utils/llm.py` | `543B85C20D87C5C8E97DAA5126537362013A89F01066960B3638D448C1AA83B3` |
| `src/xskill/agents/agno_factory.py` | `8BDF67CB7AAAD552DE8D4FC83230E12F052C5D618967D5B34F4ECBA2E0EBE711` |
| `tests/test_gpt5_nano_parameters.py` | `39FBE047DA63F1051404D46749A3C1258210CB3785CCBA9D6DEF79EE12385B55` |

## 限制与未执行项

- 仅支持本地 native；团队和外部内核在受限模式下拒绝启动。
- 无法证明来源的更新工作区脏改动拒绝自动恢复，保留现场；不会覆盖人工内容。
- 不同来源相同 Atom ID 的证据会记录冲突并拒绝混入；有界 checkpoint 恢复超出 200 个提交且仍有未证明素材时停止并报错。
- 产物验证检查实际提交树、静态引用和安装内容，不执行生成的脚本，也不能证明方法对真实任务有效。
- 未运行真实模型效果实验、README 中的公开基准或跨平台 CI 全矩阵。功能回归通过不能证明历史公开成绩不变，也不代表 GPT-5 nano 配置复现那些成绩。

默认自由演化模式与指定模式应分别做真实效果评测，保持数据划分和模型配置可比较；测试答案和评测轨迹不得回流到蒸馏。

使用说明见 [指定技能蒸馏](../targeted-distillation.md)，设计见[最终设计与开发方案](../superpowers/plans/2026-11-05-targeted-distillation-design-plan.md)。
