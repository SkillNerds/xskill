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
[![WeChat group](https://img.shields.io/badge/微信-进群-2E8B6F?style=flat-square&logo=wechat&logoColor=white)](#community)

[English](README.en.md) · **简体中文**

<sub>📄 论文:<em>xskill: Team-Level Skill Distillation, Sharing, and Evolution for Coding Agents</em> · <a href="paper/xskill_v4.pdf">PDF(19 页)</a></sub>

<br>

<img src="docs/assets/demo-v5.gif" width="720" alt="一个 coding agent 列出 xskill 从它过往会话中蒸馏出的技能">

</div>

* * *

## ✨ 为什么需要 xskill

你的 coding agent 每次碰到熟悉的问题,都从头再推一遍。你要么重新讲一遍,要么手动维护一个提示词库——而这个库没人盯着就会慢慢烂掉。xskill 让这些活儿消失:

- 🚀 **装起来快**——`pip install xskill`,一个配置文件,搞定。
- 💬 **你只管写代码**——它在后台观察你的真实会话,把"管用的做法"自动蒸馏成 `SKILL.md`,你的 agent 自动加载。零额外操作。
- 🧬 **自我进化,不是自卖自夸**——新版技能只有在真实流量上**确实让用户体验更好**,才会取代旧版。由用户体验驱动,而不是让大模型给自己打分。
- 👥 **团队放大器**——一个人解决,全队复用。团队越大,进化越快、越准。

* * *

## 🔁 一人解决,全队复用

只要团队里有一个人在自己的会话里搞定了某个问题,这个解法就会变成一条技能——其他人的 agent 自动拿到。没人需要专门写文档。

<div align="center">
<img src="docs/assets/xs_multiplier.zh.svg" width="820" alt="一个人解决一次问题,xskill 把它蒸馏成一条技能,瞬间扩散到全队">
</div>

## 🧩 跨越每一个 agent 与设备——同一个技能库

笔记本上用 Claude Code、服务器上用 Codex、IDE 里用 Cursor。xskill 从它们全部收集脱敏后的轨迹,进化出**同一个共享技能库**,再把结果同步回你用的每一个 agent。

<div align="center">
<img src="docs/assets/xs_crosscontext.zh.svg" width="860" alt="多个 agent 和设备汇入同一个轨迹 watcher 和同一个进化技能库,再同步回所有 agent">
</div>

## 🌱 孤岛 → 集体进化

没有一个共享、自我改进的技能库,每个开发者都在孤岛里重复解决同样的问题。xskill 把这些被浪费的、隔离的努力,变成可以复利累积的共享经验。

<div align="center">
<img src="docs/assets/xs_silos_vs_collective.zh.svg" width="860" alt="左:开发者各自孤立地重复解决同一问题。右:开发者连到同一个进化的共享技能库。">
</div>

* * *

## 🏗 架构

<div align="center">
<img src="docs/assets/xs_architecture.zh.svg" width="900" alt="xskill 架构:agent 生态 → 轨迹 watcher → 原子拆分 → 技能路由 → 技能编辑 agent → canary 灰度 A/B → 技能仓库,并支持团队模式">
</div>

几个职责很窄的 LLM agent 在干活:一个把轨迹拆成单一意图的**原子(Atom)**;一个把每个原子**路由**到某条技能;一个在技能攒够素材后**重写** `SKILL.md`;还有一个在真实流量上对新版本做 **A/B 测试**、留下胜出者。每条技能都是它自己的 git 仓库,所以每一次改动都有版本、可回滚。细节见 [`docs/agent.md`](https://github.com/SkillNerds/xskill/blob/main/docs/agent.md)。

* * *

## 🚀 快速开始

### 路径 A —— 单人、本地

```bash
pip install xskill          # Python 3.9+
xskill serve                # 写出 ~/.xskill/config.yaml,然后退出
```

打开 `~/.xskill/config.yaml`,填两个模型端点(一个 LLM,一个 embedding 向量模型):

```yaml
skill_dir: ~/.xskill/skill

llm:
  base_url: https://api.deepseek.com
  model:    deepseek-v4-flash
  api_key:  YOUR_KEY

embedding:
  # DeepSeek 没有 embedding,用 DashScope / OpenAI / Ollama,例如:
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  model:    text-embedding-v4
  api_key:  YOUR_KEY
  dim:      0
```

再跑一次 `xskill serve`——它会自动识别你机器上每一个受支持的 agent 并开始监听。要把旧轨迹归档回填进来:

```bash
xskill registry add /path/to/trajectories
```

### 路径 B —— 团队模式(最有杀伤力的用法)

一台机器当 server,其他人作为轻量 client 加入,大家对着同一个进化中的技能库工作。

```bash
xskill serve --server                          # 打印一个加入 token
xskill connect <host:port> --token <token>     # 在每个同事的机器上运行
```

- **悄悄蒸馏你的高手**——一个人的解法自动到达全队。
- **任何工作流都能接**——Codex、Claude Code、Cursor IDE 随便选,大家加入同一个库,跨工具同步。
- **轨迹保持私有**——会话在上传前已脱敏。
- **A/B 驱动的进化**——一处改动先在每个人身上度量,再决定要不要扩散。人越多,进化越快越准。
- **专家可以手动教**——本地改一条技能,会作为 `user-staging/<client_id>` 拉进 server,喂给下一轮进化。

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
| **任何其他 agent** | 手动 | SDK `xskill.adapters.submit_trajectory` | 拷贝/软链 `SKILL.md` 目录 |

## 📖 概念

| 术语 | 含义 |
| ---- | ---- |
| **Trajectory(轨迹)** | 一次 agent 运行——一段会话的完整记录(`traj_*.md`)。 |
| **Atom(原子)** | 轨迹里最小的、单一意图的切片。路由在这一层发生。 |
| **Skill(技能)** | 一个 `SKILL.md` 加可选脚本,各自在独立的 git 目录里带版本。 |
| **Canary(灰度)** | 当前技能与新候选版本在真实流量上的 A/B 对比测试。 |
| **UX score(体验分)** | 某条技能在某个原子上服务用户的好坏,由交互本身打 1–10 分。灰度保留分更高的那个版本。 |

* * *

## 🗺 路线图

- 更多 agent 适配——Goose、OpenHands、Aider
- 更成熟的用户画像与推荐
- 原生 MCP server 接口(把技能作为工具暴露)
- 浏览技能库、查看 canary 数据的 Web UI
- 技能市场——导入/导出可移植的技能包
- 多租户技能库(按团队的 `skill_dir`)

## 📰 动态

- **2026-05-29** —— 新增 Trae IDE / Trae Agent 适配。
- **2026-05-23** —— `v0.5.0`:团队模式(client-server)、轨迹脱敏、Python 3.9、运行时不再需要 `git` 二进制。
- **2026-05-20** —— MIT 开源;上线 PyPI:`pip install xskill`。
- **2026-05-12** —— 支持 Claude Code、Codex、OpenCode;接通 OpenClaw 与 Cursor。

* * *

<a name="community"></a>
## 💬 社区

关于 coding-agent 技能的问题、想法、踩坑故事——来一起聊。扫码加微信群:

<div align="center">

<table><tr><td align="center" style="border:2px solid #07C160;border-radius:16px;padding:18px 26px;background:#F2FCF6">
<b style="color:#07C160;font-size:1.05em">💬 微信交流群</b><br><br>
<img src="docs/assets/wechat-qr.jpg" width="200" alt="xskill 微信群二维码"><br>
<sub>扫码进群 · 一起聊 coding agent 技能</sub>
</td></tr></table>

</div>

## 🙏 致谢

xskill 站在更广泛的 trajectory-to-skill 研究方向之上(港大 OpenSpace、阿里 Trace2Skill、华东师范 AutoSkill 等),也建立在它所接入的 agent 生态之上——Claude Code、Codex、OpenCode、Cursor、OpenClaw、Trae。

## 🤝 贡献

欢迎提 Issue 和 PR——尤其是新的 agent 适配。具体见仓库说明。

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
