# OpenEarth Kernel

这个目录是 OpenEarth 接入 XSkill Kernel API v2 的可交付桥接包。训练只有一个 SDK
入口 `train_skills(...)`，输入始终是 XSkill 轨迹，不再区分“真实数据训练接口”和“数据集
任务训练接口”。

## 数据流

```text
ready TrajectoryResource
  └─ atoms: AtomResource[]
       ├─ user trajectory → atom.ux_score
       └─ temp trajectory → OpenEarth oracle score
                    ↓
              ScoredAtomInput
                    ↓
       reflect / curate → SkillDraft
                    ↓
          context.publisher.submit(...)
```

在启用内部 benchmark 时，full rebuild 会先由 OpenEarth target harness 运行配置的 case，
把每个 rollout 转成一个平台格式 Markdown，记录 oracle score，再调用
`context.trajectories.create_temp(...)`。临时轨迹进入 XSkill 拆分队列；拆成唯一 Atom
并变为 ready 后，在后续 Kernel 调用中进入上面的统一训练流。

桥接层仅提交 `atom_split_status == "ready"` 的轨迹。输入范围严格遵循
`context.invocation`：

- `changed_trajectory_ids` 非空时，只处理其中的 ready 轨迹；
- changed 为空且 `full_rebuild=True` 时，处理全部 ready 轨迹；
- changed 为空且非 full rebuild 时不蒸馏轨迹、不调用 SDK 训练入口，但仍检查待发布队列。

SDK 将每条轨迹展开成 atom，并以 `<trajectory resource id>#<atom id>` 作为稳定证据 ID：

- 真实用户轨迹（`source="user"`）直接读取每个 `atom.ux_score`；
- 评测轨迹（`source="temp"`）忽略 atom 自身分数，读取 OpenEarth 在
  `context.workspace` 中按 `trajectory_id` 保存的 oracle 分数；
- `pending` 和 `updated` 轨迹不会进入本轮训练；
- UX 7–10 为成功，1–5 为失败，6 或未评分暂缓；
- 当前链路没有 Gate 阶段。

已有 XSkill Skill 会作为只读 main 快照传给 SDK。显式声明 `level` 的 Skill 直接进入
对应层；没有 `level` 的 Skill 由 OpenEarth 在一次批量 LLM 调用中分类为
`planning`、`functional` 或 `unclassified`。分类结果按 `name + main_commit_sha`
缓存在 workspace，不会写回原 Skill；低于 0.7 置信度的结果保持未分类。

## OpenEarth 处理流程

一次 Kernel 运行大致分为以下阶段：

1. **可选 benchmark 生产**：启用 benchmark 的 full rebuild 先运行尚未登记的 case，
   计算 oracle score，并创建 pending 临时轨迹；这些轨迹等待 XSkill 拆分，不在当轮训练。
2. **选择输入**：优先处理本轮变化且已经完成 atom 拆分的轨迹；手动全量运行时读取全部
   ready 轨迹；没有变化且不是全量重建时本轮不做任何训练。
3. **同步已有 Skill**：把 XSkill main Skill 的完整只读快照放入 OpenEarth workspace，
   并对没有 `level` 的新增或变化 Skill 做一次批量分类。
4. **构造训练证据**：把每条轨迹展开成 atom，真实数据使用 `atom.ux_score`，临时评测
   数据使用已记录的 OpenEarth oracle score。增量运行通过稳定证据 ID 和内容签名跳过
   历史未变化 atom；全量重建会重新蒸馏历史 atom，但同一批内仍按 `atom_id` 去重，
   同 ID 不同内容或评分会直接报冲突。
5. **划分反思通道**：UX 7–10 的成功 atom 用于提炼可复用 Planning Skill；UX 1–5
   的失败 atom 先检索已有 Planning Skill，找不到时生成临时诊断计划，再反思产生
   Functional 修复候选。
6. **整理候选**：对本轮 Functional 候选做一次全局 curation，可保留、修改、合并或
   丢弃候选，同时校验其 atom 来源。
7. **生成草稿**：候选名称与已有 Skill 完全相同时生成 update draft，并保留原 bundle；
   否则生成 create draft。
8. **交给 XSkill 发布**：SDK 只返回 `SkillDraft`。Kernel 再通过
   `context.publisher.submit(...)` 发布；已有 active staging 的 Skill 会进入 OpenEarth
   待发布队列。

内部 benchmark 只负责产生训练证据，不执行 candidate rollout 或 OpenEarth Gate。
XSkill 自己的 staging/canary 发布机制不受影响。

全量重建仍以当前 main Skill 作为只读反思上下文，并复用
`name + main_commit_sha` 分类缓存，因此不会重新分类未变化的 Skill。重建只生成并提交
本轮草稿，不会删除没有重新生成的旧 Skill。

## staging 发布队列

XSkill 同一时刻只允许同名 Skill 存在一个 active staging。OpenEarth 不再丢弃因此无法
提交的新草稿，而是在 `context.workspace/openearth-publication-queue.json` 中为每个
Skill 保留一个最新 pending draft：

- active staging 仍存在时继续等待；新 draft 覆盖同名旧 pending draft，避免陈旧版本
  无限堆积；
- staging 被拒绝、main 未变化时，pending draft 使用原 base commit 直接提交；
- staging 晋升、main 已变化时，先保留最新 main 的 provider 元数据和 bundle 文件，
  再叠加 pending draft 的 OpenEarth 字段和正文，最后以新 main SHA 提交；
- 每次 scheduled/manual 调用都会先执行一次队列 tick，即使本轮没有 changed trajectory；
- 只有 Publisher 成功接受草稿后才从队列移除，发布竞态则重新读取 main/staging 状态。

队列只管理尚未进入 XSkill staging 的下一版本；已进入 staging 的版本仍完全由 XSkill
灰度、晋升或拒绝。

## 运行日志

Kernel 使用 `xskill.kernel.openearth` logger 输出结构化 INFO 进度，覆盖运行开始、发布
队列整理、benchmark rollout、蒸馏开始、Planning/Functional 反思完成、草稿提交或排队
以及运行完成。每条日志都包含 `run_id` 和 `stage`，例如：

```text
run_id=... stage=distillation_started selected_atoms=12 selected_trajectories=4
run_id=... stage=reflect_planning candidates=2 trajectories=7
run_id=... stage=run_completed generated_drafts=2 processed_atoms=12
```

运行 `xskill serve` 时，这些 INFO 写入 `~/.xskill/logs/xskill.kernel.log`
（`xskill.kernel.*` 组件文件），并冒泡进 `xskill.log`。kernel-host 捕获的 stdout
（SDK print）也追加进同一份 kernel.log；Dashboard 算法内核页实时串流这份文件，
并显示最近一条 `stage=`。

```bash
tail -f ~/.xskill/logs/xskill.kernel.log
tail -f ~/.xskill/logs/xskill.log | grep 'xskill.kernel.openearth'
```

## 安装

SDK 源码位于本地 `sdk/`，由本目录的 `.gitignore` 排除，不会提交到 XSkill 远程仓库。
仓库交付的是构建后的 wheel：

```bash
python -m pip install \
  examples/kernels/openearth/wheels/openearth_skill_sdk-0.10.0-py3-none-any.whl
```

然后复制 Kernel 目录并创建私有配置：

```bash
mkdir -p "$HOME/.xskill/kernels"
cp -R examples/kernels/openearth "$HOME/.xskill/kernels/openearth"
cp "$HOME/.xskill/kernels/openearth/config.yaml.example" \
  "$HOME/.xskill/kernels/openearth/config.yaml"
```

在 `~/.xskill/config.yaml` 中选择：

```yaml
kernel:
  kernels_path: ~/.xskill/kernels
  kernel_id: openearth
```

详细 SDK 和评测接入方式见 [docs/sdk-usage.md](docs/sdk-usage.md)。

## 构建私有 SDK

在本地源码目录执行：

```bash
cd examples/kernels/openearth/sdk
python -m pip wheel \
  --no-deps \
  --no-build-isolation \
  --wheel-dir ../wheels \
  .
```

每次重建后同步更新 `SHA256SUMS`。wheel 包含 benchmark dataset/environment/target
harness 和运行时代码，但不包含 Gate、组合 experiment 或 SDK 测试。
