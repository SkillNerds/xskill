# Testing Strategy — Risk Axes and Execution Layers

> 目标：持续覆盖 Python 3.9–3.12、Linux / macOS / Windows，以及 Claude Code / Codex / OpenCode 三类轨迹，同时避免把彼此独立的兼容性风险做成昂贵的全量笛卡尔积。

## 1. 测试分层

| 层 | 目的 | 外部 LLM | 真实 agent CLI | PR 执行拓扑 |
| --- | --- | :--: | :--: | --- |
| **UT + IT** | 纯逻辑、状态机、持久化与模块协作 | ❌ | ❌ | Ubuntu × Python 3.9–3.12，macOS / Windows × Python 3.11，共 6 jobs |
| **SkillEdit BDD** | 通过本地 aimock 验证 SkillEdit 行为场景 | ❌ | ❌ | Ubuntu × Python 3.11，共 1 job |
| **Smoke E2E** | fixture 轨迹 → ingester → installer，验证三种生态路径 | ❌ | ❌ | 三个平台各运行完整测试文件，共 3 jobs |
| **Team lifecycle E2E** | 本地 fake LLM 下的 connect 生命周期与 Team C/S 完整闭环 | ❌ | ❌ | Ubuntu × Python 3.11，共 1 job |
| **Live-agent E2E** | mock LLM 驱动真实 Codex / OpenCode 进程并采集会话 | ❌ | ✅ | PR/main 为 Ubuntu × 2 agents，共 2 jobs |
| **Real-LLM E2E** | 用 DeepSeek 验证真实模型边界 | ✅ | ❌ | 非 PR 事件的 Linux job；无密钥时安全跳过 |
| **Control-plane stress** | 300×300 压力与 nightly 标记用例 | ❌ | ❌ | 独立 nightly workflow，不阻塞常规 PR |

分层原则：

- UT/IT 覆盖全部受支持 Python 版本和三个操作系统，但两个风险轴不做完整交叉。
- BDD 只在带 aimock 的专用 job 中执行，普通 UT/IT 显式忽略 `tests/bdd`。
- Smoke 不安装 agent CLI；它直接调用 ingester 和 installer，真实进程契约由 live-agent 层负责。
- 已知时序问题必须在具体测试上使用 `@pytest.mark.flaky(...)`。平台 job 不做全局 rerun，避免隐藏新回归。
- 常规 pytest job 输出最慢 25 条测试，为后续优化提供证据。

## 2. CI 触发与矩阵

### 触发表

| 事件 | UT + IT | BDD | Smoke / Team lifecycle | Live-agent | Real-LLM | Build |
| --- | :--: | :--: | :--: | :--: | :--: | :--: |
| `pull_request` to main | ✅ 6 | ✅ 1 | ✅ 3 + 1 | ✅ Ubuntu × 2 | ❌ | ✅ 1 |
| `push` to main | ✅ 6 | ✅ 1 | ✅ 3 + 1 | ✅ Ubuntu × 2 | ✅ 1 | ✅ 1 |
| `workflow_dispatch` | ✅ 6 | ✅ 1 | ✅ 3 + 1 | ✅ 3 OS × 2 | ✅ 1 | ✅ 1 |
| `schedule` | ✅ 6 | ✅ 1 | ✅ 3 + 1 | ✅ 3 OS × 2 | ✅ 1 | ✅ 1 |

独立的 `nightly-control-plane-stress.yml` 还会在 schedule/manual 下运行三平台 nightly 标记用例和 Linux 300×300 压力测试。

### UT + IT 风险轴

```yaml
include:
  - {os: ubuntu-latest, python-version: "3.9"}
  - {os: ubuntu-latest, python-version: "3.10"}
  - {os: ubuntu-latest, python-version: "3.11"}
  - {os: ubuntu-latest, python-version: "3.12"}
  - {os: macos-latest,  python-version: "3.11"}
  - {os: windows-latest, python-version: "3.11"}
```

Ubuntu 验证 Python 兼容性轴，Python 3.11 验证平台轴。这样仍覆盖所有声明支持的版本和平台，但由 12 个完整 job 收敛为 6 个。

### Smoke

Smoke 仅按操作系统展开：

```yaml
os: [ubuntu-latest, macos-latest, windows-latest]
```

每个 job 一次执行 `tests/e2e/test_smoke.py`，包括三个参数化 agent 用例和 real-home 隔离回归。测试使用仓库内 fixture，不联网、不启动 agent 子进程，也不需要安装 Codex/OpenCode CLI。

### Live-agent

PR 和 main push 使用已验证的固定 CLI 版本，在 Ubuntu 上运行 Codex/OpenCode 两条真实进程契约。schedule/manual 扩展到三平台，并安装 latest，以非阻塞方式暴露上游 CLI 或平台兼容性变化。

固定版本应只在一轮成功的跨平台 nightly/manual 之后更新，并在更新 PR 中记录通过的版本号。当前基线：

- Codex CLI `0.149.0`
- OpenCode CLI `1.18.21`

## 3. 跨平台注意事项

| 风险 | 处理方式 |
| --- | --- |
| Windows symlink 权限 | installer 走 junction/copy fallback；断言最终文件存在，不绑定链接类型 |
| `Path.home()` 平台差异 | 测试显式传入隔离的 `home_root` / `target_root`，并保留 real-home 污染回归 |
| CRLF / LF | Windows job 配置 `core.autocrlf=input`；路径和文本测试使用 `pathlib` 与显式编码 |
| SQLite WAL 与线程调度 | 已知不稳定测试按用例精确标记 flaky；未标记失败立即暴露 |
| 外部 CLI 发布漂移 | PR 固定已验证版本，nightly/manual 验证 latest |

## 4. 本地验证

常规测试：

```bash
make test
```

与常规 CI 的选择范围一致地验证非 E2E、非 BDD 用例：

```bash
pytest tests/ --ignore=tests/e2e --ignore=tests/bdd -q -m "not nightly" --durations=25
```

分层验证：

```bash
XSKILL_AIMOCK_E2E=1 pytest tests/bdd -v --timeout=120 --durations=25
pytest tests/e2e/test_smoke.py -v --durations=25
pytest tests/e2e/test_connect_lifecycle_e2e.py tests/e2e/test_team_cs_e2e.py -v --durations=25
```

涉及 ingestion、install 或 daemon 的代码改动还必须按贡献指南运行 `make e2e`。live-agent 测试需要本机安装对应 CLI，并显式覆盖 pytest 的默认 `tests/live` 忽略设置。

## 5. 维护规则

- `.github/workflows/ci.yml` 是可执行的唯一真相；本文档描述其设计。废弃 draft 不再复制 workflow 内容。
- `tests/test_ci_workflow.py` 静态锁定风险轴、Smoke 覆盖、live-agent 触发范围和 CLI 版本策略。
- 新增 Python 版本时扩 Ubuntu 兼容性轴；新增平台时扩 Python 3.11 平台轴。
- 新增 agent 时，先补 fixture Smoke，再补真实进程 live-agent 契约；不要为不启动 CLI 的测试安装 CLI。
- CI 拓扑变更必须同时更新静态契约与本文档，并通过一次完整线上 CI 后再合并。
