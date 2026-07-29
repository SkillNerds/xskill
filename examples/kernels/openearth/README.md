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

桥接层仅提交 `atom_split_status == "ready"` 的轨迹。输入范围严格遵循
`context.invocation`：

- `changed_trajectory_ids` 非空时，只处理其中的 ready 轨迹；
- changed 为空且 `full_rebuild=True` 时，处理全部 ready 轨迹；
- changed 为空且非 full rebuild 时直接结束，不读取 Skill、不调用 SDK。

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

1. **选择输入**：优先处理本轮变化且已经完成 atom 拆分的轨迹；手动全量运行时读取全部
   ready 轨迹；没有变化且不是全量重建时本轮不做任何训练。
2. **同步已有 Skill**：把 XSkill main Skill 的完整只读快照放入 OpenEarth workspace，
   并对没有 `level` 的新增或变化 Skill 做一次批量分类。
3. **构造训练证据**：把每条轨迹展开成 atom，真实数据使用 `atom.ux_score`，临时评测
   数据使用已记录的 OpenEarth oracle score。增量运行通过稳定证据 ID 和内容签名跳过
   历史未变化 atom；全量重建会重新蒸馏历史 atom，但同一批内仍按 `atom_id` 去重，
   同 ID 不同内容或评分会直接报冲突。
4. **划分反思通道**：UX 7–10 的成功 atom 用于提炼可复用 Planning Skill；UX 1–5
   的失败 atom 先检索已有 Planning Skill，找不到时生成临时诊断计划，再反思产生
   Functional 修复候选。
5. **整理候选**：对本轮 Functional 候选做一次全局 curation，可保留、修改、合并或
   丢弃候选，同时校验其 atom 来源。
6. **生成草稿**：候选名称与已有 Skill 完全相同时生成 update draft，并保留原 bundle；
   否则生成 create draft。
7. **交给 XSkill 发布**：SDK 只返回 `SkillDraft`。Kernel 再通过
   `context.publisher.submit(...)` 发布；已有 active staging 的 Skill 会被跳过。

当前到第 6 步即结束 OpenEarth 训练，不执行 OpenEarth candidate rollout 或 Gate。
XSkill 自己的 staging/canary 发布机制不受影响。

全量重建仍以当前 main Skill 作为只读反思上下文，并复用
`name + main_commit_sha` 分类缓存，因此不会重新分类未变化的 Skill。重建只生成并提交
本轮草稿，不会删除没有重新生成的旧 Skill。

## 安装

SDK 源码位于本地 `sdk/`，由本目录的 `.gitignore` 排除，不会提交到 XSkill 远程仓库。
仓库交付的是构建后的 wheel：

```bash
python -m pip install \
  examples/kernels/openearth/wheels/openearth_skill_sdk-0.7.0-py3-none-any.whl
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

每次重建后同步更新 `SHA256SUMS`。wheel 中只应包含运行时代码，不应包含 Gate、
dataset task/evaluator 或 SDK 测试。
