# 指定技能蒸馏本地端到端门槛清单

适用于 `b2435f2` 及后续包含该修复的版本。先跑隔离的自动用例，再在独立测试账户或虚拟机做真实 CLI 验收。两类结果分别记录。[本轮验收结果与版本对照](../reports/2026-11-05-targeted-distillation-acceptance.md)

## 1. 准备

- [ ] 在仓库根目录确认待测提交、工作区和 Python 实际加载的源码。需要复现分支提交时，使用该提交的独立检出，避免把本地未提交改动混入结果。
- [ ] 确认 Python 环境已安装项目和 `dev` 测试依赖。下列命令使用 PowerShell，并串行执行测试组。
- [ ] 真实模型验收前，确认实际使用的模型、请求兼容层和依赖版本。用同一隔离环境分别验证对话与向量接口，再开始蒸馏。

```powershell
git rev-parse --short HEAD
git status --short
python -X utf8 -c 'import sys, xskill; print(sys.version); print(xskill.__file__)'
python -X utf8 -m pytest --version
```

## 2. 自动化门槛

现有用例用临时技能库、SQLite、轨迹及安装目录。端到端用例用假模型驱动真实候选、编辑、提交和灰度流程。准备提交前，先跑快速冒烟，再跑两组完整回归。

```powershell
python -X utf8 -m pytest tests/test_distillation_cli.py tests/test_distillation_e2e.py -q -ra --maxfail=1
```

```powershell
$gateDistillationTests = @(rg --files tests -g 'test_distillation*.py')
if (-not $gateDistillationTests) { throw '未找到定向蒸馏测试，请确认当前目录是仓库根目录' }
python -X utf8 -m pytest @gateDistillationTests -q -ra
```

```powershell
$gateDefaultTests = @(
    'tests/test_skill_edit_agent.py', 'tests/test_skill_edit_batch_turns.py',
    'tests/test_skill_edit_mutation_contracts.py', 'tests/test_skill_edit_dirty_queue.py',
    'tests/test_skilledit_parallel.py', 'tests/test_candidates_atom.py',
    'tests/test_candidates_concurrency.py', 'tests/test_agent_tool_context_isolation.py',
    'tests/test_agent_worker.py', 'tests/test_agent_worker_runtime.py',
    'tests/test_pipeline_pool_hot.py', 'tests/test_canary.py',
    'tests/test_canary_rotation.py', 'tests/test_canary_install_atomic.py',
    'tests/test_user_edit_absorb.py', 'tests/test_atom_candidate_pending.py',
    'tests/test_team_cli_serve.py', 'tests/test_server_watcher_startup.py',
    'tests/test_skill_tools_atom.py', 'tests/test_skill_tools_dotgit_guard.py',
    'tests/test_agent_tools_py39_schema.py', 'tests/test_description_opt.py',
    'tests/test_rebuild.py', 'tests/test_cli_serve_guard.py', 'tests/test_status_file.py',
    'tests/test_git_lock_concurrency.py', 'tests/test_traj_read.py',
    'tests/test_multi_atom_store.py'
)
python -X utf8 -m pytest @gateDefaultTests -q -ra
```

Windows 若因权限无法建立普通文件符号链接，定向用例会报告跳过。默认回归中的 `test_move_rejects_symlink_alias_of_source_without_mutation` 若在建立夹具时明确报 WinError 1314，保留整组原始结果。需要单独验证其余用例时，可用下列命令。Windows junction、Git 路径越界和目标替换仍需实际通过。

```powershell
python -X utf8 -m pytest @gateDefaultTests -q -ra -k 'not test_move_rejects_symlink_alias_of_source_without_mutation'
```

以本次执行的退出码、实际收集数量和跳过理由判定。单独复测结果与整组结果分别记录。在 POSIX 环境补验符号链接、目录 `fsync` 和灰度脚本实际落盘的执行位。

| 勾选 | 门槛 | 必须观察到的结果 | 对应自动用例 |
| --- | --- | --- | --- |
| [ ] | 配置与启动 | `configure` 只保存运行。新建撞名、更新缺失或有未证明脏改动均报错，目标文件和原运行状态保留。团队或外部内核拒绝受限模式。 | `cli`、`runs`、`recovery` |
| [ ] | 新建目标 | `create_one` 只创建指定目标。证据达标后从草稿晋升，实际 `main` 提交正文、引用和状态一致。 | `e2e`、`artifacts` |
| [ ] | 更新目标 | `update_only` 使用已有技能的正文和说明。具备编辑条件时，有效变更进入灰度。主版本 SHA 在灰度晋升前保持原值。仅更新引用文件也可交付。 | `e2e`、`checkpoint_fixes` |
| [ ] | 素材归属 | 相关素材进入目标候选。无关素材记录跳过，旧历史可重判，默认全局学习标记保留。目标已有候选可继续处理。 | `materials`、`e2e`、`checkpoint_fixes` |
| [ ] | 其他技能隔离 | 非目标技能的文件、候选、Git 引用、安装副本和后台积压保持原样。第二个 worker 或来自不同状态目录的写入不能接管同一技能库。 | `background`、`install_scope`、`cc_feedback`、`write_boundaries`、`runs` |
| [ ] | Git 越界与部分写入 | 混合合法和非法路径的一批 `checkout` 在写入前整体拒绝。目标未提交内容与非目标文件及 refs 均无差异。合法文件和目录恢复可用。 | `review_fixes`、`policy`、`write_boundaries` |
| [ ] | 提交凭据与恢复 | 伪造的提交字段不能确认批次外素材。运行、来源、Atom 编号和内容版本必须匹配。同名轨迹从绑定的来源读取。提交后中断可重试清理，待处理新版保留。 | `checkpoint_fixes`、`review_fixes` |
| [ ] | 恢复失败保护 | 旧凭据只有编号、候选只有文件记录、恢复扫描超过上限，或灰度发布关系无法证明时，报错并保留现场。替换目标目录和来源不明的未提交内容也阻止恢复。 | `checkpoint_fixes`、`runs`、`recovery` |
| [ ] | 引用与草稿修复 | `./scripts`、嵌套路径和 Windows 路径均被校验。引用在实际提交里缺失时不报成功。候选清空后，坏草稿仍能修复并在通过校验后晋升。 | `artifacts`、`review_fixes`、`checkpoint_fixes` |
| [ ] | 灰度交付 | 灰度副本的内容和文件模式来自实际提交。物化失败、工作区未提交、无有效改动时不得报新的有效候选。 | `review_fixes`、`e2e` |
| [ ] | 默认流程 | `auto` 下候选消费、并发编辑、灰度、轨迹读取及团队入口仍能按原规则运行。 | `$gateDefaultTests` |

表中用例名对应 `tests/test_distillation_<用例名>.py`。路径攻击、提交伪造和中断时序通过自动夹具复现，便于逐项核对前后差异。

- [ ] 用基线版本创建一份临时 Registry SQLite 库，放入代表性旧记录。确认新版只读状态查询显示 `auto` 且不建表。正常连接后确认三张蒸馏表自动建立、旧记录保留，重复打开后数据库仍能通过完整性检查。

## 3. 真实 CLI 验收

在独立的测试账户或虚拟机准备本地 standalone、native 配置、可用的 LLM 和 embedding，以及仅含脱敏测试数据的技能库和轨迹。`serve --home` 只隔离生态扫描及安装位置，`configure` 和 `status` 仍使用当前账户的 XSkill 状态目录。用以下命令确认路径落在测试环境。

```powershell
python -X utf8 -c 'from xskill.config import XSKILL_HOME, get_skill_dir, get_registry_db_path; print(XSKILL_HOME); print(get_skill_dir()); print(get_registry_db_path())'
python -X utf8 -m xskill registry list
python -X utf8 -m xskill distill status --json
```

### M1. 新建与隔离

- [ ] 准备一份已验证的飞书权限排障轨迹和一份明显无关的轨迹。在受限配置生效前，把它们所在的测试目录用 `python -X utf8 -m xskill registry add <轨迹目录>` 注册。准备一个普通目录形式的非目标技能 `gate-unrelated`，含已提交正文和至少一个待处理候选。用下方取证命令保存其文件哈希、候选文件和 Git refs。
- [ ] 确认测试目标 `gate-feishu-debug` 尚不存在，旧 `serve` 和 worker 已退出。执行配置并查询状态。

```powershell
python -X utf8 -m xskill distill configure --mode create_one --skill gate-feishu-debug --description '排查飞书 API 权限及 scope 错误并核验修复'
python -X utf8 -m xskill distill status --json
```

- [ ] 确认模式、目标和 `run_id` 正确，配置阶段未生成目标目录。启动服务，在另一终端重复查询状态。

```powershell
python -X utf8 -m xskill serve --host 127.0.0.1 --port 18080
```

- [ ] 从另一终端确认 `/api/v1/health` 返回 HTTP 200，再开始观察蒸馏状态。

```powershell
(Invoke-WebRequest -Uri 'http://127.0.0.1:18080/api/v1/health').StatusCode
```

- [ ] 核对相关素材已采纳、无关素材已跳过。达到生成门槛后，应出现 `run.artifact_state = main_committed`，实际 Git `main` 含有效正文及引用文件。若停在 `waiting_evidence`，向已注册的测试目录补充有已验证解决过程的相关轨迹。当前代码以候选 `weightscore` 累加至少 10 分触发编辑。[门槛说明](../thresholds.md)
- [ ] 如果已达门槛但未晋升，记录每条评分、候选总分、编辑日志、剩余候选和实际 Git 分支。用每条低于 10 分、总分达到门槛的一批素材覆盖模型误判的情况。
- [ ] 停止测试服务并等待 worker 退出。重新取非目标快照，确认文件内容、候选和 refs 与启动前一致。

### M2. 更新与灰度

- [ ] 在测试配置启用灰度。使用 M1 的产物，或准备一个已提交 `main`、有主版本使用反馈且无正在裁决的灰度的目标。记录 `main` SHA，准备能带来实际改动的相关测试素材。目标未满足反馈或灰度条件时，分别核对 `waiting_main_feedback` 或 `waiting_canary`，补齐前置后再验证交付。
- [ ] 在服务停止后执行更新配置，再按 M1 的命令启动服务。

```powershell
python -X utf8 -m xskill distill configure --mode update_only --skill gate-feishu-debug
```

- [ ] 达到编辑条件后，核对 `candidate_committed` 和 `staging` 实际提交。正常灰度晋升前，原 `main` SHA 保持一致。确认灰度副本包含已提交的引用文件和实际内容，再复查非目标快照。
- [ ] M1 或 M2 产物若承诺可运行的自检，先审阅实际提交的脚本，再在隔离环境按正文命令验证。使用合成凭据和可控的测试接口，覆盖有效输入和无效 token、权限不足等失败输入。只有文件存在或脚本返回 `0` 时，功能验收仍需看输出是否符合预期。
- [ ] 停止服务后重启同一运行，核对 `run_id`、模式和目标一致。已确认的素材不被重复消费。若已按正常灰度流程晋升，则按当前 `main` 核验 SHA 和内容。

### M3. 拒绝错误请求与恢复默认

- [ ] 测试 `serve` 正在运行时配置另一模式，命令应失败，原 `run_id` 和目标保持一致。另一个 `serve` 也应被拒绝。结束测试服务并等待 worker 退出后再执行其余检查。
- [ ] 在服务停止后，以已存在的名称执行 `create_one`，再以不存在的名称执行 `update_only`。每条命令后立即查看 `$LASTEXITCODE`，预期均为 `2`，且未创建、覆盖或改名任何目标。

```powershell
python -X utf8 -m xskill distill configure --mode create_one --skill gate-feishu-debug --description '同名拒绝测试'
$LASTEXITCODE
python -X utf8 -m xskill distill configure --mode update_only --skill gate-missing-target
$LASTEXITCODE
```

- [ ] 切回 `auto` 后查询状态，预期 `mode = auto`、`run = null`。需要继续验证真实自动流转时，在测试环境重新启动 `serve`，确认非目标积压可恢复处理。

```powershell
python -X utf8 -m xskill distill configure --mode auto
python -X utf8 -m xskill distill status --json
```

### 取证命令

在 M1 启动前运行以下命令，保存非目标快照和目标路径。M1 和 M2 停止服务后，在同一取证终端运行差异检查。快照包含候选文件和目录内的 Git 数据。已准备安装副本时，对其普通目录另取一份快照。

```powershell
$gateTestSkillRoot = python -X utf8 -c 'from xskill.config import get_skill_dir; print(get_skill_dir())'
$gateOtherSkill = Join-Path $gateTestSkillRoot 'gate-unrelated'
$gateTargetSkill = Join-Path $gateTestSkillRoot 'gate-feishu-debug'
function Get-GateSkillSnapshot([string]$GateSkillPath) {
    if (-not (Test-Path -LiteralPath $GateSkillPath -PathType Container)) {
        throw "找不到测试技能目录 $GateSkillPath"
    }
    Get-ChildItem -LiteralPath $GateSkillPath -File -Recurse -Force |
        Sort-Object FullName |
        ForEach-Object { '{0} {1}' -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash, $_.FullName }
    if (Test-Path -LiteralPath (Join-Path $GateSkillPath '.git')) {
        git -C $GateSkillPath for-each-ref --format='%(refname) %(objectname)'
        if ($LASTEXITCODE -ne 0) { throw '读取测试技能的 Git refs 失败' }
    }
}
$gateOtherBefore = @(Get-GateSkillSnapshot $gateOtherSkill)
```

```powershell
# 停服后运行。无输出表示快照相同。
Compare-Object $gateOtherBefore @(Get-GateSkillSnapshot $gateOtherSkill)
# M2 更新前后分别记录，灰度晋升前两次输出应一致。
git -C $gateTargetSkill rev-parse --verify refs/heads/main
```

在目标产物已提交时执行。脚本以状态报告的分支和 SHA 核对实际 Git 树，并复用成品校验器。等待状态或失败状态直接记录 `run.artifact_state` 和 `run.error`。

```powershell
$gateRunStatus = python -X utf8 -m xskill distill status --json | ConvertFrom-Json
if ($gateRunStatus.run.artifact_state -notin @('main_committed', 'candidate_committed')) {
    throw '当前运行尚无可验收的提交产物'
}
$gateSkillPath = Join-Path $gateRunStatus.root_key $gateRunStatus.run.target_skill
$gateArtifactBranch = $gateRunStatus.run.artifact_branch
$gateArtifactSha = git -C $gateSkillPath rev-parse --verify "refs/heads/$gateArtifactBranch"
if ($LASTEXITCODE -ne 0 -or $gateArtifactSha -ne $gateRunStatus.run.artifact_sha) {
    throw '状态报告的 SHA 与实际提交不一致'
}
git -C $gateSkillPath ls-tree -r $gateArtifactSha
python -X utf8 -c 'import sys; from pathlib import Path; from xskill.skill.artifact_validation import validate_skill_artifact; print(validate_skill_artifact(Path(sys.argv[1]), ref=sys.argv[2]))' $gateSkillPath $gateArtifactSha
```

## 4. 基线对照

- [ ] 用两个独立检出和状态目录，记录基线与待测提交。固定模型、请求兼容层、同批已筛选素材、候选评分、起始正文和反馈。新建用同名草稿，更新用相同主版本文件树。旧版用原有编辑流程，新版使用对应的受限模式。
- [ ] 每个版本各调度一次真实编辑。记录素材文件哈希、日志、剩余候选和 Git 引用。该对照从编辑入口开始，CLI 从轨迹到交付的结果仍以 M1 至 M3 为准。
- [ ] 如果两版生成的产物不同，可把同一份已知有问题的产物送进提交入口，检查校验能否拦截。分别记录模型是否自然生成问题产物、提交是否成功、灰度副本包含哪些文件，以及经审阅的脚本对无效输入的行为。

## 5. 记录与放行

| 记录项 | 本次结果 |
| --- | --- |
| Commit、Python、系统、实际源码路径 | |
| 定向测试命令与通过、失败、跳过数 | |
| 默认回归命令与通过、失败、跳过、排除数 | |
| M1、M2、M3 结果，运行 ID、目标、实际分支和 SHA | |
| 非目标快照差异与任何失败原因 | |
| 旧库升级、版本对照、脚本功能核验结果 | |
| 未验证的环境或用例及复测安排 | |

- [ ] **自动化放行**要求两组完整回归零失败，新增安全、恢复和端到端门槛全部通过。逐项记录平台跳过和排除理由，未验证的能力留待对应环境复测。
- [ ] **受限行为验收**记录非目标快照、越权拒绝、恢复和切回 `auto` 的结果。
- [ ] **真实产物放行**要求 M1、M2、M3 通过。等待素材、等待反馈和等待灰度是中间状态。最终需要有效提交、状态与实际 SHA 一致、非目标快照无差异，并验证产物承诺的自检行为。
