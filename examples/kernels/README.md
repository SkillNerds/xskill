# XSkill 算法内核开发指南

这份文档面向第一次接触 XSkill 的算法开发者。你会学会：bridge 写在哪里、一次
`run()` 到底代表什么、怎样批量读取轨迹、怎样安全修改已有 Skill、怎样在本机离线评测，
以及算法包如何测试和发版。

架构职责边界见[算法内核抽象层](../../docs/algorithm-kernels.md)。本目录提供三个例子：

- [starter](starter/kernel.py)：无 API、可直接跑的协议烟测内核；
- [skillopt](skillopt/kernel.py)：直接调用真实 SkillOpt SDK 的集成示例；
- [openearth](openearth/kernel.py)：第三方 SDK bridge 的通用形状示例。

## 先建立一个正确心智模型

算法开发者提供一个很薄的 Python bridge：

```text
你的算法 package / SDK
         ↑ 普通 Python import
~/.xskill/kernels/<kernel-id>/kernel.py
         ↑ XSkill 发现 KERNEL_CLASS，并调用一次 run(context)
         │
         ├─ context.trajectories  轨迹对象、轨迹目录（只读合同）
         ├─ context.skills        Skill 文件、版本和 UX（只读）
         ├─ context.publisher     新建或提交 staging 候选
         ├─ context.workspace     算法自己的可写持久空间
         └─ context.config_path   算法自己的私有配置
```

XSkill 不要求算法源码搬进本仓库，也不解析算法私有配置。运行 `xskill` 的 Python 环境只要
能 `import` 算法 package 即可。

V2 是可信的进程内插件协议：它是清晰的 API 边界，但不是安全沙箱。平台传出的轨迹路径按
合同只读；后续容器/RPC runner 才能用只读 mount 和资源限额做操作系统级强制。

## 十分钟跑通：不部署服务，直接离线评测

### 1. 安装开发版

```bash
cd /path/to/xskill-kernel-demo
python -m pip install -e '.[dev]'
python -c "import sys, xskill; print(sys.executable); print(xskill.__file__)"
```

### 2. 安装 starter bridge

```bash
mkdir -p ~/.xskill/kernels
cp -R examples/kernels/starter ~/.xskill/kernels/starter
```

目录约定如下：

```text
~/.xskill/kernels/starter/
├── kernel.py              # bridge，必须导出 KERNEL_CLASS
├── config.yaml.example    # 可提交的配置样例
├── config.yaml            # 私有真实配置，由内核维护
└── workspace/             # cursor/cache/中间 DB/算法产物，由内核维护
```

### 3. 跑标准微型数据集

不需要 `xskill serve`，也不需要配置平台 LLM/Embedding key：

```bash
xskill eval starter examples/kernels/datasets/micro-trajectories \
  --sample 1/4 \
  --plugin-dir ~/.xskill/kernels
```

`--sample 1/4` 使用 `seed + item id` 做确定性抽样，并把被选文件及 sidecar 的内容 hash
写入 selection manifest。四条输入固定选择一条；同一 seed 可复现，内容变化会生成新的
dataset ID。

运行时会显示 `tqdm` 阶段进度，结束后输出：

```text
KERNEL   VERSION  DATASET              SELECTED  PROCESSED  SKILLS  STATUS   DURATION  QUALITY  ARTIFACTS
starter  0.1.0    micro-...@<hash>     1         1          1       success  0.18s     n/a*     ~/.xskill/evaluations/...
```

结果默认落在 `~/.xskill/evaluations/<run-id>/`：

```text
run.json                 # 状态、kernel/dataset 身份、selection hash
events.jsonl             # 阶段进度事件
result.json              # 紧凑运行结果（provider metrics 会递归脱敏）
input/selection.json     # 精确输入清单及内容 hash
input/watch_dirs/        # 本次只读输入快照
kernel/workspace/        # 本次算法工作区
registry.db              # 本次隔离 Registry
skills/                  # 本次隔离 Skill Git repo
kernel_runs.db           # 本次隔离运行记录
```

离线运行不会修改生产 `registry.db`、正式 Skill、线上 kernel workspace，也不会更改
`kernel.active`。算法真实私有配置可以原地读取，但不会复制进 artifacts，避免 API key 被
带入评测产物。`--json` 关闭进度并只输出一个稳定 JSON 对象，适合 CI。

## 真实 SkillOpt 集成示例

[skillopt/kernel.py](skillopt/kernel.py)不是伪造 starter：它直接 import 并调用：

```python
from skillopt.config import load_config, flatten_config
from skillopt.engine.trainer import ReflACTTrainer
from skillopt.envs.spreadsheetbench.adapter import SpreadsheetBenchAdapter
```

安装和配置：

```bash
python -m pip install -e /path/to/SkillOpt
cp -R examples/kernels/skillopt ~/.xskill/kernels/skillopt
cp ~/.xskill/kernels/skillopt/config.yaml.example \
   ~/.xskill/kernels/skillopt/config.yaml
```

然后在 `config.yaml` 里填写 SkillOpt 自己的 `skillopt_config`、SpreadsheetBench split 和
data root。XSkill 不解释其中字段；`load_config()`、`_base_`、模型 endpoint、训练轮数和
配置迁移都归 SkillOpt 自己维护。

这个 bridge 会把 `context.workspace/<run-id>` 作为 SkillOpt `out_root`，调用
`ReflACTTrainer.train()`，读取真实 `best_skill.md`，再通过 XSkill Publisher 发布完整
Skill。它只把白名单汇总指标返回 XSkill，不把可能含密钥的 SkillOpt summary/config 原样
写入标准评测报告。

当前边界要说清楚：SkillOpt 的 SpreadsheetBench adapter 消费 benchmark task/workbook，
并不消费 XSkill 用户轨迹。因此该示例 manifest 明确标记为 `evaluation`/`manual`，且
`online_parity=false`；它是“真实 SDK 如何接进来”的例子，不能冒充已经具备生产轨迹等价
行为。要作为线上替换内核，提供方还需实现 trajectory → SkillOpt EnvAdapter。

## 我的 bridge 写在哪里

XSkill 扫描：

```text
<plugin_dir>/<kernel-id>/kernel.py
```

目录名与 `KernelManifest.id` 必须相同，只能使用小写字母、数字、`_`、`-`，最长 64 字符。
最小骨架：

```python
from xskill.kernels import (
    BaseKernel,
    KernelContext,
    KernelManifest,
    KernelRunResult,
)


class AcmeKernel(BaseKernel):
    manifest = KernelManifest(
        id="acme-distiller",
        name="Acme Distiller",
        version="1.0.0",       # 算法 package 版本，不是 API 版本
        description="Generate Skills with the Acme SDK.",
        triggers=("scheduled", "manual", "evaluation"),
        api_version=2,
    )

    def run(self, context: KernelContext) -> KernelRunResult:
        from acme_distiller import distill

        output = distill(
            trajectory_roots=[item.path for item in context.trajectories.directories()],
            config_path=context.config_path,
            workspace=context.workspace,
        )
        # 把 output 转成 Publisher 提交，见下文。
        return KernelRunResult(metrics={"candidates": len(output)})


KERNEL_CLASS = AcmeKernel
```

bridge import 失败时，Dashboard 会把该内核显示为“不可用”并展示异常，不影响其他内核。

## 一次 `run(context)` 到底是什么

一次 invocation 是平台要求内核完成的一次**有界、同步、可审计**工作：

1. 平台创建唯一 `run_id` 并确定 trigger 与输入作用域；
2. 用该作用域构造 `KernelContext`；
3. 只调用一次 `kernel.run(context)`；
4. 内核在返回前完成这批工作并提交候选；
5. 平台记录结果或异常。

V1 文档曾把 `request` 和 `context` 作为两个参数，但 provider 经常根本用不到 request，语义
也不清楚。V2 已将 invocation 收进 context，入口只有 `run(context)`。

通常只需直接 import `BaseKernel`、`KernelContext`、`KernelManifest` 和 `KernelRunResult`。
轨迹、Skill、Publisher 的对象从 context 获得，不要自行构造；也不要依赖 `_workers`、
`kernels.runtime`、Registry 表结构等内部实现。

运行身份在 `context.invocation`：

| 字段 | 含义 |
| --- | --- |
| `run_id` / `context.run_id` | 本次唯一 ID，可作为日志、幂等和工作目录键。 |
| `trigger` | `scheduled`、`trajectory_changed`、`manual` 或 `evaluation`。 |
| `dataset_id` | `live` 或带内容 hash 的离线数据集身份。 |
| `changed_trajectory_ids` | 事件触发时的变化提示；不要把它当唯一输入发现方式。 |
| `full_rebuild` | 调用方是否要求全量重算。 |

`manifest.triggers` 不包含本次 trigger 时，XSkill 会在调用前拒绝运行。

返回值 `KernelRunResult` 的定义：

| 字段 | 含义 |
| --- | --- |
| `processed_trajectory_ids` | 本次确实完成处理的 Registry-qualified ID；决定审计中的 processed 数。 |
| `submitted_skills` | 本次提交的 Skill 名称；Publisher 实际记录会自动去重合并。 |
| `metrics` | provider 自报的 JSON 指标；用于诊断，不能冒充平台质量分。 |
| `notes` | 短备注，不要放轨迹正文、用户隐私或密钥。 |

内核需要自行保证重试幂等。cursor、中间索引、SQLite、模型 cache 等都放
`context.workspace`，不要修改轨迹文件来标记状态。

## Context 只提供五组核心能力

| 属性 | 权限和用途 |
| --- | --- |
| `invocation` / `run_id` | 本次运行身份和输入作用域。 |
| `config_path` | 内核自己的私有配置路径；格式、默认值、迁移和密钥都归内核。 |
| `workspace` | 内核可写持久空间；可自由建 cursor、数据库、cache 和算法产物。 |
| `trajectories` | 逐条读取或直接取得本次允许访问的轨迹目录。 |
| `skills` / `publisher` | 读取完整 Skill、checkout 编辑、版本级 UX 和托管发布。 |

不暴露可写 Registry connection、TrajDB、SkillDB 或正式 Skill 根目录。

## 怎样读取大量用户轨迹

### 逐条 Python 读取

```python
for trajectory in context.trajectories.iter(statuses={"done", "indexed"}):
    print(trajectory.id, trajectory.ecosystem, trajectory.status)
    text = trajectory.read_text()
    raw = trajectory.read_raw_json()  # 无同名 .json 时返回 {}
    metadata = dict(trajectory.metadata)
```

`iter()` 是生成器，适合大量轨迹；`list()` 是便利接口；`get(id)` 精确读取一条。
`trajectory.id` 形如 `3:traj_abc.md`，跨目录稳定；`trajectory.path` 是标准 Markdown 路径，
`watch_dir` 是来源根目录，二者都按合同只读。

### 直接对目录使用 `rg`、`find` 或自己的批处理引擎

Python 对象不是唯一入口。内核可以取得所有注册且属于本次 invocation 的目录：

```python
import subprocess

for source in context.trajectories.directories():
    completed = subprocess.run(
        ["rg", "--json", "docker", str(source.path)],
        check=True,
        capture_output=True,
        text=True,
    )
    consume_rg_json(completed.stdout)
```

也可以使用 `find`、DuckDB、自己的 mmap/indexer 或 agent 原生文件工具。
`TrajectoryDirectoryResource` 包含 `id/path/label/ecosystem/trajectory_count/indexed_count`。
即使目录暂时为空，它仍然能通过 `directories()` 被看到。

离线 `xskill eval` 只会在隔离 Registry 中登记本次 selection 的快照目录，内核看不到生产
Registry，避免抽样后又误扫到全量输入。

## 怎样读取、修改和版本化已有 Skill

### 读取完整 bundle 与版本级 UX

```python
skill = context.skills.get("docker-recovery", days=90)
print(skill.name, skill.main_commit_sha, skill.staging_commit_sha)
print(skill.list_files())
print(skill.read_text("SKILL.md"))
print(skill.read_text("references/checklist.md"))

for version in skill.versions:
    print(
        version.side,
        version.commit_sha,
        version.ux_average,
        version.ux_samples,
        version.first_scored_at,
        version.last_scored_at,
    )
```

Skill 的稳定身份是 `name`；具体发布版本是 Git `commit_sha`。UX 记录绑定 commit，而不是只
绑定名字，因此 main/staging 的评分不会混在一起。

`skill.path` 是正式 main bundle 的只读路径，便于工具读取，但绝不能直接编辑。需要修改时
使用托管 checkout。

### Checkout → 用任意 SDK/Agent 编辑 → 提交

```python
draft = context.skills.checkout("docker-recovery")

# draft.path 位于当前 kernel workspace，可以安全交给自己的 agent/SDK/shell。
my_optimizer.edit_directory(draft.path)

published = context.publisher.submit_checkout(
    draft,
    message="improve recovery verification",
    source_trajectory_ids=tuple(consumed_ids),
)
print(published.action, published.previous_commit_sha, published.commit_sha)
```

checkout 会复制 main 的完整可分发 bundle，包括 `SKILL.md`、`scripts/`、`references/`，但
不复制 `.git`、UX、Canary 和锁文件。提交把 checkout 当作**精确目标快照**：新增、修改和
删除文件都会进入候选。

`draft.base_commit_sha` 是乐观并发令牌。若 checkout 后 main 已变化，Publisher 拒绝 stale
提交，要求重新 checkout；它不会静默覆盖别人的版本。

同名更新的生命周期：

```text
name 相同 + base main commit A
            │ submit checkout bundle B
            ▼
staging commit B（main A 继续分发）
            │ 真实流量获得分别绑定 A/B 的 UX
            ├─ B 更好 → Canary promote B 为 main
            └─ B 更差/超时 → reject，Git 历史仍保留
```

已有活跃 staging 时，新候选会被拒绝，避免替换正在灰度的版本。Canary 物化的是完整
staging bundle，不再只有 `SKILL.md`。

### 新建 Skill

新名称尚无 base version，可直接提交文本 bundle：

```python
from xskill.kernels import SkillSubmission

published = context.publisher.submit(SkillSubmission(
    name="repair-docker-compose",
    skill_md="""---
name: repair-docker-compose
description: Diagnose and recover a failed Docker Compose deployment.
metadata: {}
---

# Repair Docker Compose

Reliable workflow here.
""",
    files={
        "scripts/diagnose.py": "print('diagnose')\n",
        "references/checklist.md": "# Checklist\n",
    },
    source_trajectory_ids=tuple(consumed_ids),
    message=f"create in run {context.run_id}",
))
```

新 Skill 原子创建独立 Git repo 并进入 main。Publisher 校验名称/frontmatter、路径穿越、
隐藏平台文件、UTF-8 文本和 2 MiB bundle 上限，并写入稳定的
`metadata.kernel.id/version`。run ID 只进 `kernel_runs.db`，不会污染相同内容的 hash。

## 评测、打榜与线上 UX：不要混成一个分数

### 已实现：本地 contract eval

`xskill eval <kernel> <trajectory-dataset>` 真实经过 `KernelCatalog`、`KernelRuntime`、
Context 和 Publisher，回答：能否加载、是否幂等、处理多少输入、产出多少 Skill、耗时和
错误是什么。它提供隔离、进度、表格和标准 artifacts，可直接放进算法项目 CI。

provider 的 `metrics` 是自报指标，平台不会把其中的 `benchmark_score` 当可信质量分；本地
结果会明确显示 `QUALITY n/a`。

### 设计基准：Xarena Spreadsheet 官方 top 1/4

现有 Xarena Board 6 的固定口径是 train/val/test = 20/10/70、seed 42。历史代表结果为
SkillOpt 58/70、XSkill 57/70、no-skill 39/70，但旧 SkillOpt 与当前 target harness 曾不
一致，所以只有 protocol fingerprint 完全一致的运行才可比较 baseline delta。

完整 Spreadsheet 数据约 26 MB，不适合随 wheel 打包，也需要遵守上游数据条款。因此本
仓只内置 API-free micro dataset；官方数据由外部 data root 和固定 manifest 提供，不能把
smoke 结果标成官方榜分。

### 尚未伪装成“已完成”的部分

要复现线上行为，需要独立 server、多 client rollout、固定模型 endpoint、算法容器与
held-out evaluator 隔离，最终分别报告 hard pass、soft score 和模拟 UX。进程内 trusted
plugin 无法阻止算法偷读 test/golden；严格榜单必须用 Xarena/Kubernetes 的独立容器。

因此 V2 先交付隔离 local backend；Xarena backend 是下一层 `BenchmarkDriver`，不能用
`KernelRunResult.metrics` 冒充。上线后真实用户满意度仍来自 main/staging 的版本级 UX 和
Canary，而不是离线自报分。

## 怎样切到生产内核

通过配置：

```yaml
kernel:
  active: acme-distiller
  plugin_dir: ~/.xskill/kernels
```

或在 Dashboard「算法内核」页切换。面板只保存 `kernel.active`，只展示私有配置路径，不
读取或覆盖算法配置。切换从下一轮 sweep 生效，不中断正在运行的任务。

生产运行会记录到 `~/.xskill/kernel_runs.db`；Dashboard 汇总成功率、耗时、处理量、产出
量以及该 kernel 当前 main Skills 的后续 UX。至少同时看 UX 样本数、Canary 晋升率、覆盖
率、失败率、耗时和成本，不能只看平均分。

## 配置与 workspace 到底归谁

XSkill 只维护选择器：

```yaml
kernel:
  active: acme-distiller
  plugin_dir: ~/.xskill/kernels
```

算法内核自己维护：

```text
~/.xskill/kernels/acme-distiller/config.yaml
~/.xskill/kernels/acme-distiller/workspace/
```

- 配置 schema、默认值、迁移、endpoint 和密钥都由算法包负责；
- XSkill 将 `config_path` 作为 opaque path，不解析、不改写；
- 真实配置、workspace 和 API key 不进 Git，只提交 `config.yaml.example`；
- cursor、中间分析、缓存、SQLite 都可以放 workspace；
- `xskill eval` 的 workspace/Registry/Skills 独立，私有配置不会复制到 artifacts。

## 测试与发布

推荐四层检查：

1. import/manifest：算法 package 可导入，目录 ID 与 manifest ID 一致；
2. `run()` 单测：空输入、重复运行、配置错误、SDK 异常、cursor 提交顺序；
3. `xskill eval`：固定 selection，检查表格、artifacts、输出 bundle 和无生产污染；
4. XSkill contract/Canary：完整 checkout、stale base、staging、版本 UX 和回退。

本仓测试命令：

```bash
PYTHONPATH=$PWD/src python -m pytest \
  tests/test_kernel_abstraction.py \
  tests/test_kernel_evaluation.py \
  tests/test_canary.py -q
```

建议算法项目结构：

```text
acme-distiller/
├── pyproject.toml
├── src/acme_distiller/
└── integrations/xskill/
    ├── kernel.py
    └── config.yaml.example
```

发布时先跑算法单测和固定 dataset eval，再构建 wheel；让 `KernelManifest.version` 直接取
算法 package 版本。`KERNEL_API_VERSION` 当前是 2，代表 XSkill bridge 协议；
`KernelManifest.version` 代表你的算法版本，二者不是一回事。

## 上线前检查表

- [ ] `manifest.id`、目录名、`kernel.active` 完全一致。
- [ ] 算法 package 能被运行 XSkill 的解释器导入。
- [ ] 入口是 `run(context)`，manifest 固定 `api_version=2`。
- [ ] 轨迹逐条读取和目录批处理都只读。
- [ ] 中间状态只写 `context.workspace`。
- [ ] 修改 Skill 使用 checkout + `submit_checkout()`，不编辑正式 main 路径。
- [ ] 新 Skill 只经 Publisher 提交，来源使用 Registry-qualified trajectory ID。
- [ ] `metrics/notes/artifacts` 不包含隐私和密钥。
- [ ] 私有配置与 workspace 不进入版本库。
- [ ] 先通过固定数据集 eval，再切生产并观察版本级 UX/Canary。

## 继续阅读

- [算法内核架构与职责边界](../../docs/algorithm-kernels.md)
- [真实 SkillOpt bridge](skillopt/kernel.py)
- [可运行 starter bridge](starter/kernel.py)
- [公共 Python API](../../src/xskill/kernels/__init__.py)
- [契约测试](../../tests/test_kernel_abstraction.py)
- [评测测试](../../tests/test_kernel_evaluation.py)
