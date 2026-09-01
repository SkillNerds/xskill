# 启用 OpenEarth 算法内核（第一期用户说明书）

任务要求（约一百字）：对照主干设置页与 #155、#205 现有契约，按已确认目录补第一期用户说明书。写清两份配置怎么填、设置页怎么切核、400 与成功与日志长什么样。#155 README 里的内核页和 SSE 不写进第一期。

对应 [issue #363](https://github.com/SkillNerds/xskill/issues/363)、[合入设计](2026-08-27-openearth-landing-on-main.md)、[PR #364](https://github.com/SkillNerds/xskill/pull/364)。样例按主干设置页现有按钮与文案来写；`kernel` 段校验与 kernel-host 日志按 demo 分支和 #155 已有字符串，实现接线后才会在主干出现。

## 这是什么

第一期给人的感觉是：团队 server 多了一个可切换的算法内核。默认仍是 `native`（拆分、归类、编辑三个代理）。写成 `openearth` 之后，轨迹还是 agent-worker 拆成 Atom，Skill 仍写到现有 `skill_dir` 和 git；换的是「ready 之后谁来生成 Skill」。

不是换一套蒸馏系统，也不是新开一级导航。设置页还是现在的 `config.yaml` 全文编辑器。

## 用户怎么用

先装核、再填核自己的配置、最后才改平台 `kernel_id`。不要先改 `kernel_id` 再补目录。

### 1. 安装 wheel 并拷目录

在跑 `xskill serve` 的同一个 Python 环境里装 OpenEarth wheel，再把桥接目录拷到平台会扫的位置。目录名必须是 `openearth`，和 `KernelMetadata.id` 一致。

```
python -m pip install \
  examples/kernels/openearth/wheels/openearth_skill_sdk-0.10.1-py3-none-any.whl

mkdir -p "$HOME/.xskill/kernels"
cp -R examples/kernels/openearth "$HOME/.xskill/kernels/openearth"
cp "$HOME/.xskill/kernels/openearth/config.yaml.example" \
  "$HOME/.xskill/kernels/openearth/config.yaml"
```

平台不代装 pip，也不从看板上传 wheel。当前主干的 `config.yaml.server.example` 还没有 `kernel` 段；不写 `kernels_path` 时，发现目录默认是 `~/.xskill/kernels`。

### 2. 填写核自己的 config.yaml

只改 `~/.xskill/kernels/openearth/config.yaml`。平台不读、不改写这份文件。第一期只填 OpenCode，并且必须关掉核侧 benchmark：kernel-host 每次启动的第一圈是 `full_rebuild=True`，benchmark 开着会在启动时跑评测生产。

```
reflect:
  base_url: opencode
  model: YOUR_OPENEARTH_MODEL
  binary: opencode
  timeout: 600

benchmark:
  enabled: false
```

`model` 和 `binary` 换成这台机器上真实能跑的 OpenCode 模型。不要把平台 `~/.xskill/config.yaml` 里的 llm 段复制进这里。

### 3. 在设置页改平台 kernel.kernel_id

admin 登录看板，打开设置。编辑器里补上或改成：

```
kernel:
  kernel_id: openearth
  kernels_path: ~/.xskill/kernels
```

`kernels_path` 可以省略，默认就是 `~/.xskill/kernels`。点「校验并热加载」。`kernel` 段进重启域，页面会提示需要重启 serve；重启之后 agent-worker 才会停掉 cluster 和自动 SkillEdit，kernel-host 才会按新核跑。

不要用 `xskill distill` 换线上的核。`xskill generate` 与换核无关。

## 用户面输出（长这样）

主干设置页现在就长这样，第一期不改这块 DOM。页眉那行提示仍写 `dashboard/canary/recommend/skillhub` 热生效、`llm/watch_dirs` 需重启，不会点名 `kernel`。看按钮右边的结果，不要看页眉。

### 设置页全文编辑器

```
设置

config.yaml    /home/admin/.xskill/config.yaml
dashboard/canary/recommend/skillhub 段热生效; llm/watch_dirs 改动需重启 serve

┌─────────────────────────────────────────────┐
│ skill_dir: ~/.xskill/skill                  │
│                                             │
│ kernel:                                     │
│   kernel_id: openearth                      │
│   kernels_path: ~/.xskill/kernels           │
│                                             │
│ llm:                                        │
│   base_url: https://api.deepseek.com        │
│   ...                                       │
└─────────────────────────────────────────────┘

[仅校验]  [校验并热加载]
```

非 admin 只看得到：「仅 admin。请先以 admin 登录（左下角）。」

点「仅校验」且 yaml 合法、`kernel_id` 能被发现时：

```
✓ 校验通过
```

### 写错 kernel_id（400）

校验失败不落盘、不生效，旧核继续工作。按钮右边是红字，正文来自 `POST /admin/config/validate` 或 `reload` 的 `detail`。

格式不对（例如写成 `OpenEarth`）：

```
✗ kernel id must match [a-z0-9][a-z0-9_-]{0,63}: 'OpenEarth'
```

目录里没有这个核：

```
✗ kernel not found: not-a-real-kernel
```

目录在、wheel 没装上，核标成不可用：

```
✗ kernel openearth is unavailable: ModuleNotFoundError: No module named 'openearth_skill_sdk'
```

`kernel` 段写成列表而不是 mapping：

```
✗ kernel 必须是 mapping，got list
```

yaml 本身坏了：

```
✗ YAML 解析失败: ...
```

### 切核成功、等待 worker 重启

只改了 `kernel` 段并点「校验并热加载」：

```
✓ 已生效 无变更;⚠ kernel 段需重启 serve
```

这句话是琥珀色。热加载段为空所以是「无变更」；`kernel` 在 `RESTART_SECTIONS` 里，所以要重启 serve。重启后 agent-worker 按外核构造（停 cluster 与自动 SkillEdit），kernel-host 才会绑定 `openearth`。

没有改其它段时，不要期待「已生效 kernel」。

### xskill.log 里的 kernel-host

第一期只看 `~/.xskill/logs/xskill.log`。核自己 print 的内容和 `xskill.kernel.log` 第一期看不到：调度器把常驻子进程的 stdout 送了 DEVNULL，看板也不串流。

`xskill serve --server` 且 `kernel_id: openearth` 之后，日志格式与现有文件日志相同（`HH:MM:SS [logger] LEVEL message`）：

```
10:28:01 [xskill.kernel.host] INFO external kernel host selected openearth (interval 30.0s, server=True)
10:28:02 [xskill.kernel.host] INFO external kernel openearth run finished (changed=12, full_rebuild=True)
```

`kernel_id` 仍是 `native` 时，host 每秒醒一次就继续睡，这条 selected 不会出现。

某一圈失败：

```
10:29:10 [xskill.kernel.host] ERROR external kernel openearth run failed
```

后面跟 traceback。无变化且不是启动后第一圈时，host 不调用 `run()`，也就没有 `run finished`。

## 两份配置各管什么

平台 `~/.xskill/config.yaml` 的 `kernel` 段只做两件事：选中哪个核（`kernel_id`），以及到哪里找第三方桥接目录（`kernels_path`）。兼容旧字段 `kernel.active`、`kernel.plugin_dir`，但和新字段不能打架。

核自己的 `~/.xskill/kernels/openearth/config.yaml` 只给 OpenEarth 用：OpenCode 的模型、binary、超时，以及必须关掉的 benchmark。平台 `kernel_config()` 不解析这份文件。

不要把 llm、embedding、canary、agent_worker 写进核私有配置。也不要在平台配置里给 OpenEarth 加私有键。

## 它在哪运行

这些都发生在团队 server 那台机器上，也就是跑 `xskill serve --server` 的进程和它拉起的子进程。客户端 `xskill connect` 不读 `kernel` 段。

核目录和私有配置在 server 的 `~/.xskill/kernels/openearth`。设置页读写的是 server 的 `~/.xskill/config.yaml`。日志在 server 的 `~/.xskill/logs/xskill.log`。

本机 Docker、本机 `xskill serve`、`hub.xskill.wiki` 都不是现网。现网是格力内网那台 serve，要等这两块合进主干并升级 server 之后，在现网侧验收。

## 不要做的

不要原样搬 #155 README 的「运行日志」那一节：里面写了算法内核页、`xskill.kernel.log` 和 SSE，第一期都没有。

不要新开设置页「算法内核」卡片，不要做 `GET/POST /admin/kernels`。那是 #205。

不要在看板里代装 pip、上传 wheel、从 examples 自动拷目录。

不要开核侧 benchmark。不要用 `xskill distill --kernel` 当线上换核。不要把 edit 池人数调成 0（generate 和 SkillEdit 共用这个池）。

不要把本机 Docker 或 `hub.xskill.wiki` 写成现网地址。

## 以后再说

设置页换核卡片、已发现核列表、缺 SDK 时点不了启用、`POST /admin/kernels/active`（#205）。`xskill.kernel.log` 和看板 SSE。总览只读当前核、流水线页写「外核已停」。第一期无变化轮次仍会读 ready 行算指纹，改读投影表以后再说。

## 怎么算做完

admin 按上面三步做完，设置页能把 `kernel_id` 改成 `openearth`。写错三种坏 id（格式、找不到、不可用）都是红字 400，旧核仍工作。成功后按钮旁是「kernel 段需重启 serve」。重启之后 `xskill.log` 出现 `external kernel host selected openearth`，技能库里能看到 OpenEarth 提交的 main 或 staging。native 时没有 selected 这条。现网仍由用户在格力内网验收。

实现接线时给 `config.yaml.server.example` 补上 `kernel` 段，避免示例配置继续缺这一段。

---

# OpenEarth Kernel

第二块合入时重写 `examples/kernels/openearth/README.md`。只留下面这些章节。不要把 #155 的「运行日志」整节搬过来。

## 数据流

## 安装

## 核自己的 config.yaml

## 平台如何选中这个核

## staging 发布队列

## 不要做的
