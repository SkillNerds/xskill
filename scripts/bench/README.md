# 轨迹拆分 benchmark(trajectory-splitting bench)

度量 traj2skill 把一条原始轨迹切成多少个「原子任务(atom)」、边界切得准不准。
这是开发拆分器(splitter)时的**传感器**:任何新拆分机制都对着这套 bench 跑,
指标必须单调改善才算收敛。

**全程不调用任何 LLM、不访问网络。** 数据要么是手工合成的,要么是本机已落盘的产物。

## 目录结构

```
scripts/bench/
├── README.md                 本文件
├── evaluate.py               评测器(边界 P/R/F1 + exact + EOF + Pk + WindowDiff)
├── run_baseline.py           跑第一遍基线(现行窗机制,无 LLM)
├── baseline_window.md        基线报告(合成/真实两套并排)
├── baseline_window.json      基线机器可读结果(run_baseline.py 产出)
├── algorithm_replay/         拆分 + 路由的版本化离线质量回放（普通 CI）
├── formation_effect_replay/  Session、Atom、Task-Grounded 与 Gold Task 的方法效果配对回放
├── synthetic/
│   ├── gen_cases_v2.py       合成集生成器(纯数据,无 LLM/无网络)
│   ├── ground_truth.json     合成集真值 {case: {scenario,total_lines,boundaries,...}}
│   ├── v2_window.json        现行窗机制对合成集的离线预测(基线输入)
│   └── data/                 15 条 .md 轨迹 + 同名 .json sidecar(model 标记)
└── real/
    └── annotations.json      真实集标注(只存行号/边界/场景,绝不含原文)
```

## 两套数据集

### 1) 合成代表性集(15 条,`synthetic/`)

手工精心设计的 15 条对抗性轨迹,每条针对一个极端 / 失败模式:隐晦追问、
伪装新意图、超长单意图、超长前言、对抗性假 `## User` 标记、单 atom、快速调试
循环、撤销反悔、无 user 长段、语音转写噪声,外加若干正常基线。行号由生成器
`TrajBuilder` 精确返回,不靠手算。

- 原文(`data/*.md`)是合成的、可入库。
- 真值在 `ground_truth.json`:`boundaries == atom_starts[1:]`(去掉首个被迫起点);
  追问/澄清/纠错/撤销算同一意图,不进 boundaries;对抗性假 `## User` 也不进。

**重新生成**(确定性,产出与现状逐字节一致):

```bash
python3.11 scripts/bench/synthetic/gen_cases_v2.py
```

### 2) 真实集标注(24 条,`real/annotations.json`)

从本机真实轨迹人工标注而来。**真实轨迹原文绝不入库(隐私约束)** —— 仓库里
只存每条轨迹的**行号 / 边界 / 场景 / 置信度**等元数据。真实评测在运行时从本机
`~/.xskill` 读原文,仓库不持有任何对话内容。

`annotations.json` 每条字段:

| 字段 | 含义 |
| --- | --- |
| `lines` | 轨迹总行数 |
| `boundaries` | 人工认定的内部新意图边界(1-based 行号) |
| `atom_count` | 人工认定的理想 atom 数 |
| `scenarios` | 该轨迹涉及的失败模式标签 |
| `confidence` | `high` / `medium` / `low`;**low 不进主指标,单列** |
| `current_atoms` | 现行窗机制当前实际切出的 atom 数(漏拆对照) |

24 条里 21 条直采进主指标,3 条 `confidence=low` 的退化样本(序列化丢内容 /
人机轮次不可分)隔离单列。

## 评测器 `evaluate.py`

输出两类指标:

- **边界精确**:把边界当内部切分点,按行号匹配(可带容差 `--tol N`,近失算命中)。
  汇报 micro precision/recall/F1 + exact_match + eof_coverage + 分场景。
- **分段标准**:`Pk`(Beeferman 1999)与 `WindowDiff`(Pevzner & Hearst 2002),
  滑窗错配率,**越低越好**;对近失宽容、对漏整段严惩,补 F1 的盲区。窗宽 k 取
  参考平均段长的一半。

自测(独立小例子手算校验 Pk/WindowDiff,并设防"预测==真值硬编码骗分"的闸门):

```bash
python3.11 scripts/bench/evaluate.py selftest
```

对任意预测打分:

```bash
python3.11 scripts/bench/evaluate.py <preds.json> [--tol 5] \
    [--gt scripts/bench/synthetic/ground_truth.json]
```

`preds.json` 形如 `{case_id: {"boundaries":[...], "covered_eof":bool, "error":null}}`。

## 跑基线 `run_baseline.py`

复算现行窗机制在两套数据上的第一遍基线(无 LLM):

```bash
python3.11 scripts/bench/run_baseline.py --json scripts/bench/baseline_window.json
```

- **合成集**:直接用 `synthetic/v2_window.json`(窗机制离线预测)对真值算。
- **真实集**:窗机制的产物 = `~/.xskill/<sub>/<traj_id>/tasks/atom_*.json`。
  atom 的 `offset_start/offset_end` 是**字符偏移**,而标注 `boundaries` 是**行号**,
  所以运行时读源 `.md` 把字符偏移换算成 1-based 行号后再比对:
  `boundaries = sorted(line_of(offset_start))[1:]`,
  `covered_eof = max(line_of(offset_end)) >= total_lines`。

结论见 `baseline_window.md`。

## 隐私约束(硬性)

- 真实轨迹原文、对话内容**一律不入库**;仓库只持有 `annotations.json` 里的行号/边界/场景。
- 真实集评测必须在持有 `~/.xskill` 真实数据的本机运行;CI / 公共环境只能跑合成集。
- 切勿把 `~/.xskill` 下任何 `.md` / `atom_*.json` 原文复制进仓库。
