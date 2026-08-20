<div align="center">

<img src="docs/assets/header.png" width="820" alt="xskill — 一人解决,全队复用">

<h3>让你的 coding agent 的技能,从每一次真实会话里自我进化——你只管写代码。</h3>

<p><em>跨会话、跨 agent、跨设备、跨同事。经验持续累积,技能不断生长。</em></p>

[![PyPI](https://img.shields.io/pypi/v/xskill.svg?style=flat-square&color=E07A5F&label=PyPI)](https://pypi.org/project/xskill/)
[![Python](https://img.shields.io/pypi/pyversions/xskill.svg?style=flat-square&color=4A90B8)](https://pypi.org/project/xskill/)
[![License](https://img.shields.io/badge/license-MIT-5B8C5A?style=flat-square)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/SkillNerds/xskill?style=flat-square&color=F4805E)](https://github.com/SkillNerds/xskill/stargazers)
<br>
[![GitHub](https://img.shields.io/badge/code-SkillNerds%2Fxskill-243B45?style=flat-square&logo=github)](https://github.com/SkillNerds/xskill)
[![Paper](https://img.shields.io/badge/paper-PDF-8E44AD?style=flat-square&logo=readthedocs&logoColor=white)](paper/xskill_v4.pdf)
[![Live demo](https://img.shields.io/badge/demo-xskill.wiki-0E7C86?style=flat-square)](https://xskill.wiki/story/)
[![LINUX DO](https://img.shields.io/badge/LINUX%20DO-社区-FFB003?style=flat-square)](https://linux.do)

[English](README.en.md) · **简体中文**

<sub>📄 论文:<em>xskill: Team-Level Skill Distillation, Sharing, and Evolution for Coding Agents</em> · <a href="paper/xskill_v4.pdf">PDF(19 页)</a></sub>

<br>


</div>

* * *

## 为什么需要 xskill

```
“同事的coding agent已经做过的事情，你为什么不能直接拿来用？”
```

xskill是企业级的团队skill演进方案，支持轨迹自动蒸馏Skill，基于轨迹画像推送Skill，支持导入团队skillhub进行推荐，支持skill评价。

- **高手经验自动传递**： 一个人的解法自动到达全组，让最短路径不再壮志难酬。
- **跨Harness和设备共享进化**： Codex、Claude Code、Cursor IDE都会加入光荣的进化,齐心,协力。
- **支持专家修改Skill**： 觉得skill不完美？直接修改本地的skill，改动会被云端自动学习。
- **轨迹保持私有**： 会话在上传前已脱敏，秘钥密码和相关隐私不会被别人看到。
- **不让skill烂掉**： 自动评价skill，支持分析用户实际使用轨迹给出评价分并绘制不同**skill版本的得分趋势折线图**，大数据显微镜。

* * *

## 一人解决,全队复用

只要团队里有一个人在自己的会话里搞定了某个问题,这个解法就会变成一条技能——其他人的 agent 自动拿到。没人需要专门写文档。

<div align="center">
<img src="docs/assets/xs_multiplier.zh.svg" width="820" alt="一个人解决一次问题,xskill 把它蒸馏成一条技能,瞬间扩散到全队">
</div>

## 跨越每一个 agent 与设备——同一个技能库

笔记本上用 Claude Code、服务器上用 Codex、IDE 里用 Cursor。xskill 从它们全部收集脱敏后的轨迹,进化出**同一个共享技能库**,再把结果同步回你用的每一个 agent。

<div align="center">
<img src="docs/assets/xs_crosscontext.zh.svg" width="860" alt="多个 agent 和设备汇入同一个轨迹 watcher 和同一个进化技能库,再同步回所有 agent">
</div>

## 孤岛 → 集体进化

没有一个共享、自我改进的技能库,每个开发者都在孤岛里重复解决同样的问题。xskill 把这些被浪费的、隔离的努力,变成可以复利累积的共享经验。

<div align="center">
<img src="docs/assets/xs_silos_vs_collective.zh.svg" width="860" alt="左:开发者各自孤立地重复解决同一问题。右:开发者连到同一个进化的共享技能库。">
</div>

* * *

## 架构

<div align="center">
<img src="docs/assets/xs_architecture.zh.svg" width="900" alt="xskill 架构:agent 生态 → 轨迹 watcher → 原子拆分 → 技能路由 → 技能编辑 agent → canary 灰度 A/B → 技能仓库,并支持团队模式">
</div>

> [!NOTE]
> xskill全流程都是Agentic-Centric的管线。首先将轨迹按照内部意图拆分为子轨迹（轨迹原子），然后对轨迹原子进行聚类并分配到对应的skill，原子积攒足够后就会触发skill编辑产出新的skill版本。
不同的skill版本会在真实的用户流量上进行测试，用户体验分高的胜出作为主版本继续迭代，每一次改动都有版本、可回滚。细节见 [`docs/agent.md`](https://github.com/SkillNerds/xskill/blob/main/docs/agent.md)。

## 精度表现
xskill是一套无监督的skill蒸馏方案，其不需要构建数据集就可以完成进化。

目前版本的算法管线精度表现如下：

<p align="center">
  <sub><strong>Setup:</strong> <code>DeepSeek-V4-Flash</code> · <code>Claude Code</code> · <code>single</code> mode<br>
  <strong>Pipeline:</strong> SkillOpt built-in evaluation pipeline · Official data split</sub>
</p>

<table align="center">
  <thead>
    <tr>
      <th></th>
      <th align="right">Spreadsheet</th>
      <th align="right">ALFWorld</th>
      <th align="right">OfficeQA</th>
      <th align="right">Mean¹</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th align="left">XSkill</th>
      <td align="right"><strong>88.57</strong></td>
      <td align="right"><strong>84.33</strong></td>
      <td align="right"><strong>60.47</strong></td>
      <td align="right"><strong>77.79</strong></td>
    </tr>
    <tr>
      <th align="left">SkillOpt</th>
      <td align="right">87.86</td>
      <td align="right">77.61</td>
      <td align="right">51.16</td>
      <td align="right">72.21</td>
    </tr>
    <tr>
      <th align="left">Delta</th>
      <td align="right"><strong>+0.71</strong></td>
      <td align="right"><strong>+6.72</strong></td>
      <td align="right"><strong>+9.30</strong></td>
      <td align="right"><strong>+5.58</strong></td>
    </tr>
  </tbody>
</table>

<p align="center"><sub>¹ 表内数值为测试集通过率(%)，Mean 为三项基准的算术平均。Spreadsheet 与 ALFWorld 使用官方全量测试集，OfficeQA 使用官方划分的 1/4 子集。</sub></p>


在目前已完成的三项评测中，xskill 均高于 SkillOpt，平均分领先 5.58。

### Evaluation Setup

为了尽量还原真实的团队使用场景，评测环境通过 namespace 隔离部署了：

* 3 个独立的 `xskill-client`
* 1 个共享的 `xskill-server`
* 多个由 LLM 模拟的用户，负责与 Agent 进行 QA 交互

> [!IMPORTANT]
> XSkill 无需显式监督信号即可持续进化。
> SkillOpt 强依赖 ValSet 提供进化所需的监督信号。
> 在 ALFWorld 的 Epoch 2、3、4 中，ValSet 均出现精度溢出，导致进化失败，是其算法缺陷。

未来会支持嵌入不同的算法内核，敬请期待。

* * *

## 🚀 快速开始

### 单人模式（尝鲜体验）

```bash
pip install xskill          # Python 3.9+
xskill serve                # 第一次启动只初始化配置文件 ~/.xskill/config.yaml
```

打开 `~/.xskill/config.yaml`,填两个模型端点(一个 LLM,一个 embedding 向量模型):

```yaml
skill_dir: ~/.xskill/skill

llm:
  base_url: https://api.deepseek.com
  model:    deepseek-v4-flash
  api_key:  YOUR_KEY

embedding:
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  model:    text-embedding-v4
  api_key:  YOUR_KEY
  dim:      0
```

再跑一次 `xskill serve`, 它会自动识别你机器上每一个受支持的 agent 并开始运行，收集agent的轨迹并将skill推送到对应的harness下。

> [!NOTE]
>单人模式下，你将会丢失很多精心设计的特性，因此只建议在以下情况进行使用。
> 1.希望能够自动将之前高频工作流串联成skill
> 2.每天有高强度的Agent运行需求

### 团队模式(推荐)


服务器管理员在服务器配置config.yaml后运行中心化进程：
```bash
xskill serve --server  # 会打印connect join命令，复制给组内同事便可。
```

普通用户执行：
```bash
xskill connect <host:port> --token <token>  --name <工号/姓名>   # 在每个同事的机器上运行
```

#### 额外功能：管控面板

在config.yaml中可以填写管理员身份:
```yaml
dashboard:
  enabled: true
  public: true
  password: ""
  admins:
    - admin_name_1  # 管理员<工号/姓名>
    - admin_name_2
  admin_password: admin_passwd_123
```
然后再在任意一台pc上输入：
```bash
xskill dashboard
```

就可以打开并登录管控面面板(身份自动识别），管理员可以进行如下操作：
- 全局pin某个skill
- 暂停某个用户的轨迹上传（异常行为用户）
- 全局下线停推某个skill
- 查看skill的版本血缘，得分趋势
  
普通用户可以：
- 为自己pin某个喜欢的skill，防止推荐流变化
- 下线某个自己不喜欢的skill
- 查看自己的轨迹贡献给了哪些用户，谁用了自己的skill
- 查看skill的版本血缘，得分趋势

#### 额外功能：即时生成或改写 Skill

`generate` 不是凭空创作 Skill：它会从 team server 上已有且有权访问的轨迹中提取经验，自然语言指令只用来描述想生成或改写什么。连接到支持 `generate` 的 team server 后，可以这样创建 Skill：

```bash
xskill generate "创建一个排查 Python 内存泄漏的 Skill，包含常用诊断命令"
```

同一个命令也能基于轨迹改写已有 Skill；在指令中写明 Skill 名称和修改目标即可：

```bash
xskill generate "改写现有的 python-memory-debug Skill，补充 Windows 排查步骤"
```

需要代理优先参考指定用户的历史轨迹时，使用 `--name`；多个工号或用户 ID 以逗号分隔。省略该参数时，代理可以在 server 授权的全部轨迹范围内检索：

```bash
xskill generate --name alice,bob "根据这些用户的成功案例生成数据库迁移 Skill"
```

任务可能会先等待 SkillEdit 池的空闲席位。CLI 会持续输出排队和运行日志；完成后，生成或改写的 Skill 会直接提交到主干，并 pin 到发起人的推荐列表。如果 CLI 提示 server 版本过旧，请联系管理员升级 team server。

#### 额外功能：按需搜索

除了 server 按画像推送的Skill,client 还可以主动搜索或下载 server 中的技能:
```bash
xskill search <KEYWORDS>       
xskill search <KEYWORDS> --download     # 检索的同时下载到本地(老的搜索结果会被刷新掉，建议在临时使用场景触发）
xskill download <skill-id>          # 交互多选安装 harness, 会持久化到本地, 会随云端更新
xskill download <skill-id> --agent claude-code --agent codex -y   # 非交互式安装到指定harness
xskill upload ./my-skill            # 打包上传一个 skill 目录(含 SKILL.md),全队立即可搜到
```

#### 额外功能：SkillHub支持
 Xskill支持导入skillhub并将海量skill纳入**推荐**和**评价**，
 服务器管理员只需要配置~/.xskill/config.yaml:
 ```yaml
skillhub:
  enabled: true
  dir: /root/.xskill/skillhub_skills
 ```
然后将公司内网skillhub随意放置到该目录下（支持多个skillhub），xskill就会自动探测skill并纳入推荐，将相关skill自动推送给指定的用户.


* * *

## 🔌 与你的 agent 协同

| Agent | 状态 | 轨迹采集 | 技能安装 |
| ----- | ---- | -------- | -------- |
| **Claude Code** | ✅ 已验证 | `~/.claude/projects/` | 软链 → `~/.claude/skills/<name>/` |
| **Codex CLI** | ✅ 已验证 | `~/.codex/sessions/` | 软链 → `~/.agents/skills/<name>/` |
| **OpenCode** | ✅ 已验证 | SQLite `~/.local/share/opencode/opencode.db` | 软链 → `~/.agents/skills/<name>/` |
| **OpenClaw** | 🟡 已实现 | `~/.openclaw/agents/` | 拷贝 → `~/.agents/skills/<name>/` |
| **Cursor** | 🟡 已实现 | `~/.cursor/projects/*/agent-transcripts/` | 软链 → `~/.cursor/skills/<name>/` |
| **Trae** | 🟡 已实现 | IDE `state.vscdb` / CLI `trajectory_*.json` | 软链 → `~/.trae-cn/skills/`、`~/.trae/skills/` |
| **DeepSeek Harness (dsh)** | 🟡 已实现 | `~/.dsh/sessions/`（明文与默认 zstd 会话均可） | 软链 → `~/.dsh/skills/<name>/` |
| **任何其他 agent** | 手动 | SDK `xskill.adapters.submit_trajectory` | 拷贝/软链 `SKILL.md` 目录 |

## 📖 概念

| 术语 | 含义 |
| ---- | ---- |
| **Trajectory(轨迹)** | 一次 agent 运行会话的完整记录，Xskill维护了一套庞大的解析生态层。 |
| **TrajectoryAtom(轨迹原子)** | 轨迹里最小的、单一意图的切片，是生成skill的原料。 |
| **Skill(技能)** | 一个 `SKILL.md` 加可选脚本,各自在独立的 git 目录里带版本。 |
| **Canary(灰度)** | 当前技能与新候选版本在真实流量上的 A/B 对比测试。 |
| **UX score(体验分)** | 某条技能在某个原子上服务用户的好坏,由交互本身打 1–10 分。灰度保留分更高的那个版本。 |

* * *

## 🗺 路线图

- 动态并发：根据内网vllm负载动态调整并发以获取更高吞吐
- 多租户：支持一台实例，外放给多个组/部门用
- 工业级推荐引擎：更精细的画像和推荐算法
- 更多 agent 适配：Goose、OpenHands、Aider
- 轨迹湖：支持管理海量轨迹，服务器端安全预警，Time-Travel功能


## 📰 动态
- **2026-08-14** `v0.6.31`：`xskill rebuild --force` 不再因 `.git/objects` 非空中断；全量 rebuild 会留下 `xskill import` 纳入的技能，只清蒸馏产物。
- **2026-08-14** `v0.6.30`：team `xskill import` 后钉到发起人推荐列表；技能库登录后可点空心星 pin 进自己的推荐流，并标出推给我、已钉状态；import 后技能立即出现在技能库清单。
- **2026-08-14** `v0.6.30a3`：`xskill generate` 排队和执行时 CLI 及时打出状态，不再干等；旧安装账本混入倒退序号时仍能 import 并装回 harness。
- **2026-08-14** `v0.6.30a2`：修复 team `xskill generate` 上下文压缩后代理忘掉已执行工作。
- **2026-08-14** `v0.6.30a1`：team 模式新增 `xskill generate` 按指令写技能到主干；新增 `xskill import` 纳入已有技能。
- **2026-08-07** `v0.6.29`：修复推荐回填问题。
- **2026-08-03** `v0.6.29a6`：milvus变更为可选VDB；支持 DashBoard选择skill推送个数，支持Dashboard查询Skill的用户灰度情况；
- **2026-08-03** `v0.6.29a5`：引入 Milvus Lite 向量索引。
- **2026-07-30** `v0.6.29a4`：Dashboard 新增「我的」页，普通用户默认进入，贡献去向与世界消息支持分页浏览；支持回收client端推荐变更导致产生的孤儿skill。
- **2026-07-30** `v0.6.29a3`：推荐引擎优化。
- **2026-07-20** —— `v0.6.25`：SkillHub 支持非 UTF-8 `SKILL.md`；Dashboard 支持暂停/恢复指定用户的轨迹入库；
- **2026-07-07** —— `v0.6.2a2`：修复 Windows `connect` 无需管理员权限即可后台常驻，windows下常驻进程迭代至可用；
- **2026-07-07** —— `v0.6.2`: 引入用户画像和skill 推荐引擎;支持使用实名制工号连接服务；支持导入第三方 skillhub 纳入检索池;为Windows平台添加后台常驻(`xskill start/stop/status`), 支持CLI管理进程；
- **2026-05-29** —— 新增 Trae IDE / Trae Agent 适配。
- **2026-05-23** —— `v0.5.0`:引入团队模式(client-server)、引入轨迹脱敏功能守护隐私、支持Python 3.9、移除`git` 依赖。
- **2026-05-20** —— MIT 开源;上线 PyPI:`pip install xskill`。
- **2026-05-12** —— 支持 Claude Code、Codex、OpenCode;接通 OpenClaw 与 Cursor。

* * *

## 🙏 致谢

港大 OpenSpace、阿里 Trace2Skill、华东师范 AutoSkill、微软SkillOpt

## 🤝 贡献

欢迎提 Issue 和 PR

## 📝 引用

```bibtex
@misc{xskill2026,
  title        = {xskill: Team-Level Skill Distillation, Sharing, and Evolution for Coding Agents},
  author       = {SkillNerds},
  year         = {2026},
  howpublished = {\url{https://github.com/SkillNerds/xskill}}
}
```

## 📄 许可证

MIT © [370025263](https://github.com/370025263)。见 [LICENSE](LICENSE)。
