# OpenEarth 人工 e2e Runbook（2026-07-30）

合入目标：PR [#155](https://github.com/SkillNerds/xskill/pull/155) → `feat/algorithm-kernel-demo`  
平台修复分支：`fix/oe-acceptance-kernel-temp`  
模型：**DeepSeek 官方 `deepseek-v4-flash`**（`~/.aikey` + `~/.xskill/config.yaml`）  
Embedding：现有 ARK（不动）

## 前置（已完成）

1. **Plan1** `create_temp` / `kernel-temp` 改为 `auto_index=True`（commit `2a53cee`）
2. **Plan2** OE 0.9.0 wheel 去掉「必须 1 atom」硬报错，已推 PR155（`eb2cd33` on `haironghu/feat/openearth-kernel-sdk-v2`）
3. **Plan3** Gherkin + aimock BDD：`tests/bdd/test_benchmark_atoms_enter_training.py`（需安装 PR155 wheel + `aimock-pytest`）
4. DeepSeek `deepseek-v4-flash` 探活：官方 API 可连通（密钥不入文档）

## 合格标准（回顾）

- 做题轨 `create_temp` → 能被拆 → ready → **原子进入 train_skills**（BDD 已自动证明）
- 人工 e2e：注 10 条真轨 + 开 benchmark → 真拆 → **等真实 UX 分** → canary/PK 给出 **promoted 或 rejected**
- 不做 OE Gate

## 环境准备

```bash
# 1) 隔离 home，避免动生产 ~/.xskill
export E2E_HOME=/tmp/oe-e2e-home-$(date +%Y%m%d)
mkdir -p "$E2E_HOME"
cp ~/.xskill/config.yaml "$E2E_HOME/config.yaml"
# 确保 llm.model=deepseek-v4-flash, base_url=https://api.deepseek.com
# api_key 可从 ~/.aikey 的 DEEPSEEK_API_KEY 写入（勿提交）

# 2) 安装平台修复 + OE kernel
python3.11 -m venv "$E2E_HOME/venv"
source "$E2E_HOME/venv/bin/activate"
pip install -e /path/to/xskill-kernel-demo[dev]
pip install /path/to/openearth_skill_sdk-0.9.0-py3-none-any.whl  # PR155 最新

mkdir -p "$E2E_HOME/kernels/openearth"
# 拷贝 examples/kernels/openearth/{kernel.py,config.yaml.example,...}
# config.yaml: benchmark.enabled=true
# benchmark.dataset_dir=/home/admin/leaderboard/datasets/officeqa  # 或迷你子集

# 3) 抽 10 条真实用户轨
mkdir -p "$E2E_HOME/watch/user"
ls /home/admin/data/xskill_eval/sample_dataset/traj_*.md | sort | head -10 \
  | while read f; do cp "$f" "$E2E_HOME/watch/user/"; done
```

## 执行步骤

1. **启动**（隔离 home）：
   ```bash
   source ~/.aikey
   xskill --home "$E2E_HOME" serve ...   # 或项目惯用入口
   # 注册 watch 目录：$E2E_HOME/watch/user
   ```
2. **等待用户轨拆分**到 ready（看 registry / 看板）。
3. **触发 full_rebuild** 跑 OE benchmark → `create_temp`。
4. **确认** `kernel-temp` 目录 `auto_index=1`，temp 轨最终 ready。
5. **后续 run** 确认训练侧消费到 temp atoms（workspace 指标 / 日志中的 atom_id）。
6. **staging 出现后**，等待真实 UX 打分凑齐 canary（`canary.min_samples`，默认 5）。
7. **记录** `AtomCanary` 结论：`promoted` 或 `rejected`（超时 discard 本轮不算）。

## 执行记录（填写）

| 项 | 值 |
| --- | --- |
| 日期 | 2026-07-30 |
| E2E_HOME | `/tmp/oe-e2e-home-20260730`（已备：10 条用户轨 + OE kernel 配置 + mini spreadsheet 题集） |
| 用户轨 10 条 | 源：`/home/admin/data/xskill_eval/sample_dataset/` |
| 题集 | `/home/admin/xarena_out/spreadsheet-mini-5-task-fast` |
| DeepSeek | `deepseek-v4-flash` @ `api.deepseek.com`（探活通过） |
| 用户轨拆完 | ⏳ 待在隔离 home 起 serve |
| temp 拆完并入训 | BDD 已绿；现场 e2e ⏳ |
| PK 结论 | ⏳ 等真实 UX 打分攒齐（`min_samples=5`） |

## 备注

- 生产机已有 `xskill serve`（含 docker/root 实例）。**本验收必须用独立 `--home`**，不要往生产 watch 目录乱注轨。
- PK 依赖真实 UX 样本，可能跨多日；BDD 不替代本条，但已锁定「原子入训」契约。
