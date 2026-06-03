# Design Doc — xskill 控制台 Dashboard（serve 内置 Web 面板）

- 日期：2026-06-01
- 状态：v0 + 埋点(instrumentation)三件套均已落地并入 main；端到端核验通过
- 实现计划：`docs/superpowers/plans/2026-06-01-dashboard.md`
- 关联：Issue #43（成本统计）、`docs/deployment-mode.md`、`docs/adr/0001-rate-limit-diy-not-litellm.md`
- 设计原型（沙滩品牌版 B · 内容丰富版）：`xskill.wiki/dashboarddemo/m-brand-rich.html`

## 0. v0 落地状态（2026-06-01）

已实现（`src/xskill/dashboard/` 子包 + `tests/test_dashboard_*.py`）：
- `config.dashboard` 段（enabled/public/password）
- `DashboardMetrics`：overview 衍生率 + 按生态/按模型分域对比（纯读 registry）
- `DashboardAccessMiddleware`：默认仅本机 + 可选 HTTP Basic 密码
- `router` 端点 `/api/v1/dashboard/overview`、`/by-domain` + 静态壳 `/` + `/app.js`
- `create_app` 按 config 挂载；前端 B 版式运行时取数

未落地（埋点 backlog，见 §5.1）：推荐触发率、原子采纳率精确值、canary 晋升率 —— 前端已占位 + ⓘ tooltip；成本/画像/生态详情分区前端为占位，后续接既有 `/api/v1/stats` 等。

## 1. 目标与范围

把 `xskill.wiki/dashboarddemo` 那个**静态只读快照 demo**，落地成 `xskill serve` 内置的**真·控制台**：实时读后端、可视化整条"轨迹 → 技能"管线的状态、成本与质量指标，方便运营者**优化管线和推荐**。

**v0 范围（本设计）**：
- 一个可插拔的 `dashboard` 子包，由 `serve` 按配置挂起。
- 总览 / 成本&用量 / 用户&画像 / 技能库 / 灰度 / 生态 / 设置 七个分区（前端按分区组织，单页 SPA-lite）。
- 衍生质量指标：**能现算的先上**，需埋点的标注并进 backlog。
- 默认仅本机访问 + 可选密码。

**不在 v0**：推荐触发率等需新增埋点的指标的**真实数据**（先占位 + 标 `*`，埋点单列 backlog 分期做）；多用户画像的丰富展示（当前本机冷启动）。

## 2. UI / 信息架构

定稿方向 = **方案 B（Tabler 骨架 + xskill 海洋品牌皮肤）**，左侧栏 + 顶栏 app 外壳，原 B 配色（海洋青 `#1C7A8C` + 暖沙底）。

分区与每块"传达什么有用信息"：

| 分区 | 关键内容 |
| --- | --- |
| 📊 总览 | 5 KPI（目录/轨迹/技能/今日成本/安装）；**关键率**（单轨迹均原子、原子采纳率、技能产出率、处理成功率、重试率、平均 ux、推荐触发率\*、canary 晋升率\*）；**分域对比**（按生态、按用户模型）；成本/灰度/生态/模型占比/Pipeline 摘要卡，各带「详情 →」跳子页 |
| 💰 成本 & 用量 | 今日/本周/累计/每-skill 均价；**按模型**（调用、prompt/completion token、缓存命中率、成本、价格源）；按步骤占比；价格 watchdog 行；近 7 日趋势 |
| 🏄 用户 & 画像 | 用户表（轨迹/用过 skill/主力模型）；画像构成（兴趣标签云 + 偏好 skill 推荐桶）；冷启动状态显式标注；该用户模型占比 |
| 🐚 技能库 | 搜索 + 卡片；**单 skill 触发率排行**（推荐次/被采用/触发率\*/ux/加载）；点开 = 描述/版本/🌱 进化路径(git)/触发率趋势 |
| 🧪 灰度 Canary | staging vs main 得分、held、裁决（晋升/回滚/排队） |
| 🏝 生态目录 | 生态/轨迹/已索引/路径 |
| ⚙️ 设置 | 映射 config.yaml：启用、密码、公网开关（只读展示当前值） |

`*` = 需新增埋点才能算准，见 §5。

## 3. 代码结构与隔离

### 3.1 目录（自包含子包）

```
src/xskill/dashboard/
  __init__.py
  router.py      # FastAPI APIRouter：GET / 返回前端壳；GET /api/v1/dashboard/* 聚合端点
  metrics.py     # OOP DashboardMetrics：衍生指标聚合（纯读 registry，无 FastAPI 依赖，可单测）
  security.py    # LoopbackOnly 中间件 + 可选密码校验
  static/
    index.html   # 由 demo 的 index.html 演化；运行时 fetch 取数（零构建）
    app.js
    app.css
```

### 3.2 三层隔离

- **模块隔离**：依赖单向 `dashboard → 核心(pipeline/registry/usage/prices)`；核心**绝不 import dashboard**。删掉整个 `dashboard/` 目录，`serve` 仍正常。
- **安全隔离**：看板比 API 敏感（成本、画像）。默认仅 loopback 可达，见 §4.3。
- **测试隔离**：`metrics.py` 全为纯函数式读库，脱离服务可直接单测（符合"改代码必带单测"）。

### 3.3 扩展方式

新增一个面板分区 = `metrics.py` 加一个聚合方法 + `router.py` 加一个 REST 端点 + 前端加一个 `section`。不改核心。

## 4. 与 CLI / 配置 / 网络的结合

### 4.1 CLI 拉起（不新增子命令）

沿用现有 `xskill serve [--host] [--port]`（守"5 子命令、无散 flag"）。`create_app()`（`src/xskill/api/app.py`）在工厂内读 config：

```python
if cfg.get("dashboard", {}).get("enabled"):
    from xskill.dashboard.router import build_dashboard_router
    app.include_router(build_dashboard_router(cfg))
    app.add_middleware(DashboardAccessMiddleware, cfg=cfg)
```

一条 `xskill serve` 同时拉起 API(`/api/v1/*`)、docs(`/docs`)、看板(`/`)。不加 `--dashboard` flag。

### 4.2 配置（config.yaml 新增段，与 canary/watcher 同构）

```yaml
# ===== Dashboard (the built-in web console served by `xskill serve`) =====
dashboard:
  enabled:  true
  public:   false      # 默认仅本机可达；true 才放行公网（看板路由）
  password: ""         # 可选；非空时看板要求登录（API 不受影响）
```

按 CLAUDE.md：缺字段不做静默兜底——`dashboard` 段缺失即视为 `enabled:false`（不挂载），这是**显式默认**而非 fallback 兼容；新部署由 `CONFIG_TEMPLATE` 写出该段。

### 4.3 端口与公网访问

- **端口**：与 serve 同端口（默认 8000），看板 `/`、API `/api/v1/`、docs `/docs` 同一 app/进程/端口；`--port` 改它。不单开端口。
- **公网访问**：`serve --host` 默认 `0.0.0.0`（为 team server 的 `/api/v1/*`）。`DashboardAccessMiddleware` 让 `dashboard.public` **只管看板路由**：
  - `public:false`（默认）：非 loopback 源访问 `/` 与 `/api/v1/dashboard/*` → `403`；`/api/v1/team/*` 等不受影响。
  - `public:true`：放行；若 `password` 非空，则看板要求登录（cookie/session）。

## 5. 数据与指标可用性（如实标注）

| 指标 | 现在可算 | 来源 / 缺口 |
| --- | --- | --- |
| 单轨迹均原子、技能产出率、处理成功率、重试率、平均 ux | ✅ | `trajectories`（tasks_extracted / status / skill_generated / retry_count / ux_score）聚合 |
| 分域对比（按生态 / 按用户模型） | ✅ | 按 `watch_dirs.ecosystem` / `trajectories.source_model` 分组 |
| 成本 / token / 缓存命中 / 按模型按步骤 / 趋势 | ✅ | `llm_usage` + `usage_summary()`；价格健康 `prices.refresh_health()` |
| **原子采纳率** | ⚠️ 部分 | 总原子有（tasks_extracted）；"被采纳"需**原子级采纳标记**，当前只有 traj 级 skill_generated。需轻量埋点 |
| **canary 晋升率** | ⚠️ | 需 canary **裁决历史日志**（晋升/回滚事件），当前只有即时状态 |
| **推荐触发率（整体 + 单 skill）** | ❌ | 当前只记 `skill_used`，未记"何时把哪个 skill 推荐给了谁"。需新表 `recommendation_log` |

### 5.1 埋点(instrumentation) —— 三件套均已落地

best-effort（try/except 包裹，失败不阻断管线）：

1. ✅ `recommendation_log(ts, client_id, skill, side, bucket)` —— 记于 `build_manifest`（只记 recommended bucket，per-sync 低频）。触发率分母按 `COUNT(DISTINCT client_id)` 去重，抗反复同步膨胀。
2. ✅ `atom_adoption(ts, atom_id, skill, weightscore, was_new)` —— 记于 `process_atom_task`（cluster 大模型调用之后，写入相对可忽略）。采纳数按 `COUNT(DISTINCT atom_id)` 去重，抗重复聚类。
3. ✅ `canary_decision(ts, skill, action, …)` —— 记于 `check_and_decide` 三终态（promoted/rejected/timeout_discarded，周期性低频）。晋升率 = 晋升/已裁决。

聚合见 `DashboardMetrics.trigger_rate/adoption_rate/promotion_rate`，端点 `/api/v1/dashboard/rates`。可靠性（去重/封顶/除零）经 `tests/test_dashboard_instrumentation.py` 单测 + 独立代理端到端核验（63/63）。

**测试保真**：`test_scan_then_harvest_full_chain` 的 `_StubAgno` cluster 分支原本瞬时返回（不真实），放大了逐 atom 旁路写入的相对开销、扰动 candidates 晋升竞态。给 stub 加 `sleep(0.03)` 模拟真实大模型耗时后，该测试稳定（12/12）——这是修测试保真度，不是为过测试砍功能。

**测试隔离**：`tests/conftest.py` 新增 autouse fixture，把 `get_registry_db_path` 重定向到 tmp，杜绝任何 `record_*(db_path=None)` 污染真实 `~/.xskill/registry.db` 或与线上 serve 抢 WAL 锁。

**已知近似**：`trajectories` 无 `client_id`，推荐触发率为 skill 粗粒度（被推荐 skill 是否被任意用户采用），非按人精确归因；面板 tooltip 已注明。

## 6. 测试

- `metrics.py`：每个聚合方法用内存/临时 registry 喂样本数据单测（含空库、冷启动、单生态等边界）。
- `security.py`：loopback 放行 / 非 loopback 403 / 密码校验，单测中间件。
- `router.py`：FastAPI TestClient 验证端点形状、`enabled:false` 时不挂载。
- 价格/成本聚合复用 `test_usage.py` 既有夹具。

## 7. 未决问题

- 前端取数：轮询间隔 vs 复用现有 SSE？v0 先简单轮询（stats 类只读，秒级足够）。
- 密码方案：明文 config vs hash？v0 先简单 session + 明文比对，后续可加 hash。
- 画像在"有数据"时的标签云聚类算法（沿用 `profile_reco` 质心，前端只渲染结果）。
