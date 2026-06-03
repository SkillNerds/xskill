# Dashboard 控制台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `xskill serve` 扩成内置一个可插拔的 Web 控制台子包，按 config 挂载，默认仅本机，展示管线状态/成本/衍生质量指标。

**Architecture:** 自包含子包 `src/xskill/dashboard/`（metrics 纯函数读 registry / security 中间件 / router 挂端点与静态壳 / static 零构建前端）。核心不依赖它；`create_app()` 仅在 `config.dashboard.enabled` 时 `include_router` + 加访问中间件。看板与 API 同端口。

**Tech Stack:** Python 3.11、FastAPI/Starlette、sqlite3（经 `registry.get_connection`）、零构建前端（vanilla JS + Tabler CDN）、pytest + FastAPI TestClient。

**设计依据：** `docs/superpowers/specs/2026-06-01-dashboard-design.md`

---

### Task 1: config 增加 dashboard 段

**Files:**
- Modify: `src/xskill/config.py`（`CONFIG_TEMPLATE` 末尾 + 新增 `dashboard_config()`）
- Test: `tests/test_dashboard_config.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dashboard_config.py
from xskill.config import dashboard_config

def test_dashboard_config_defaults_when_absent():
    assert dashboard_config({}) == {"enabled": False, "public": False, "password": ""}

def test_dashboard_config_reads_values():
    cfg = {"dashboard": {"enabled": True, "public": True, "password": "s3cret"}}
    assert dashboard_config(cfg) == {"enabled": True, "public": True, "password": "s3cret"}

def test_dashboard_config_partial_fills_defaults():
    assert dashboard_config({"dashboard": {"enabled": True}}) == \
        {"enabled": True, "public": False, "password": ""}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3.11 -m pytest tests/test_dashboard_config.py -v`
Expected: FAIL（`ImportError: cannot import name 'dashboard_config'`）

- [ ] **Step 3: 实现 `dashboard_config`**

在 `src/xskill/config.py` 末尾追加：

```python
def dashboard_config(cfg: dict) -> dict:
    """从已加载 config 取 dashboard 段，缺字段用显式默认（非 fallback 兼容）。"""
    d = cfg.get("dashboard") or {}
    return {
        "enabled": bool(d.get("enabled", False)),
        "public": bool(d.get("public", False)),
        "password": str(d.get("password", "") or ""),
    }
```

- [ ] **Step 4: 在 CONFIG_TEMPLATE 末尾补段**

在 `CONFIG_TEMPLATE` 的 `team:` 段之后、闭合 `"""` 之前插入：

```yaml

# ===== Dashboard (the built-in web console served by `xskill serve`) =====
dashboard:
  enabled:  false      # 设 true 才挂载控制台到 serve 的 /
  public:   false      # 默认仅本机可达；true 才放行公网（仅看板路由）
  password: ""         # 可选；非空时看板要求 HTTP Basic 登录（API 不受影响）
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python3.11 -m pytest tests/test_dashboard_config.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/xskill/config.py tests/test_dashboard_config.py
git commit -m "feat(dashboard): config 增加 dashboard 段 | add dashboard config section"
```

---

### Task 2: 衍生指标层 DashboardMetrics

**Files:**
- Create: `src/xskill/dashboard/__init__.py`（空）
- Create: `src/xskill/dashboard/metrics.py`
- Test: `tests/test_dashboard_metrics.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dashboard_metrics.py
from xskill.pipeline.registry import get_connection
from xskill.dashboard.metrics import DashboardMetrics


def _seed(db):
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES('/cc','cc','claude_code')")
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES('/oc','oc','opencode')")
    rows = [  # (wd, status, atoms, skill_generated, retry, ux, model)
        (1,'done',6,'nginx-skill',0,8.0,'deepseek-v4-pro'),
        (1,'done',4,'',1,7.0,'deepseek-v4-flash'),
        (1,'splitting',2,None,0,None,'deepseek-v4-flash'),
        (2,'done',3,'oc-skill',0,7.5,'deepseek-v4-flash'),
    ]
    for wd,st,a,sg,rt,ux,m in rows:
        conn.execute("INSERT INTO trajectories(watch_dir_id,filename,status,tasks_extracted,"
                     "skill_generated,retry_count,ux_score,source_model) VALUES(?,?,?,?,?,?,?,?)",
                     (wd,f"f{a}{st}",st,a,sg,rt,ux,m))
    conn.commit(); conn.close()


def test_overview_ratios(tmp_path):
    db = tmp_path/"r.db"; _seed(db)
    o = DashboardMetrics(db_path=db).overview()
    assert o["trajs"] == 4 and o["atoms"] == 15
    assert o["avg_atoms_per_traj"] == 3.75          # 15/4
    assert o["success_rate"] == 75.0                # 3 done / 4
    assert o["skill_yield"] == 50.0                 # 2 有 skill / 4
    assert o["retry_rate"] == 25.0                  # 1 retried / 4
    assert round(o["avg_ux"], 2) == 7.5             # (8+7+7.5)/3


def test_overview_empty_db_no_zerodiv(tmp_path):
    db = tmp_path/"e.db"; get_connection(db).close()
    o = DashboardMetrics(db_path=db).overview()
    assert o == {"trajs":0,"atoms":0,"avg_atoms_per_traj":0.0,"success_rate":0.0,
                 "skill_yield":0.0,"retry_rate":0.0,"avg_ux":0.0}


def test_by_ecosystem(tmp_path):
    db = tmp_path/"r.db"; _seed(db)
    rows = {r["ecosystem"]: r for r in DashboardMetrics(db_path=db).by_ecosystem()}
    assert rows["claude_code"]["trajs"] == 3 and rows["claude_code"]["atoms"] == 12
    assert rows["claude_code"]["skills"] == 1
    assert rows["opencode"]["trajs"] == 1


def test_by_model(tmp_path):
    db = tmp_path/"r.db"; _seed(db)
    rows = {r["model"]: r for r in DashboardMetrics(db_path=db).by_model()}
    assert rows["deepseek-v4-flash"]["trajs"] == 3
    assert rows["deepseek-v4-pro"]["skills"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3.11 -m pytest tests/test_dashboard_metrics.py -v`
Expected: FAIL（`ModuleNotFoundError: xskill.dashboard.metrics`）

- [ ] **Step 3: 建空 `__init__.py` 与实现 `metrics.py`**

```python
# src/xskill/dashboard/__init__.py
```

```python
# src/xskill/dashboard/metrics.py
"""DashboardMetrics — 衍生质量指标(纯读 registry,无 FastAPI 依赖,可单测)。

只算"现在数据就能算"的指标;需埋点的(推荐触发率/原子采纳率精确值/canary 晋升率)
不在此层,见 design doc §5 backlog。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from xskill.pipeline.registry import get_connection


def _pct(num: float, den: float) -> float:
    return round(num / den * 100, 1) if den else 0.0


class DashboardMetrics:
    def __init__(self, db_path: Optional[Path] = None):
        self._db = db_path

    def overview(self) -> dict:
        conn = get_connection(self._db)
        try:
            r = conn.execute(
                "SELECT COUNT(*) trajs, COALESCE(SUM(tasks_extracted),0) atoms,"
                " SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done,"
                " SUM(CASE WHEN skill_generated IS NOT NULL AND skill_generated!='' THEN 1 ELSE 0 END) skilled,"
                " SUM(CASE WHEN retry_count>0 THEN 1 ELSE 0 END) retried,"
                " AVG(ux_score) avg_ux FROM trajectories"
            ).fetchone()
        finally:
            conn.close()
        n = r["trajs"] or 0
        return {
            "trajs": n,
            "atoms": r["atoms"] or 0,
            "avg_atoms_per_traj": round((r["atoms"] or 0) / n, 2) if n else 0.0,
            "success_rate": _pct(r["done"] or 0, n),
            "skill_yield": _pct(r["skilled"] or 0, n),
            "retry_rate": _pct(r["retried"] or 0, n),
            "avg_ux": round(r["avg_ux"], 2) if r["avg_ux"] is not None else 0.0,
        }

    def by_ecosystem(self) -> list[dict]:
        conn = get_connection(self._db)
        try:
            rows = conn.execute(
                "SELECT wd.ecosystem ecosystem, COUNT(t.id) trajs,"
                " COALESCE(SUM(t.tasks_extracted),0) atoms,"
                " SUM(CASE WHEN t.skill_generated IS NOT NULL AND t.skill_generated!='' THEN 1 ELSE 0 END) skills,"
                " AVG(t.ux_score) avg_ux"
                " FROM watch_dirs wd LEFT JOIN trajectories t ON t.watch_dir_id=wd.id"
                " GROUP BY wd.ecosystem ORDER BY trajs DESC"
            ).fetchall()
        finally:
            conn.close()
        return [self._row(r, "ecosystem") for r in rows]

    def by_model(self) -> list[dict]:
        conn = get_connection(self._db)
        try:
            rows = conn.execute(
                "SELECT COALESCE(source_model,'unknown') model, COUNT(*) trajs,"
                " COALESCE(SUM(tasks_extracted),0) atoms,"
                " SUM(CASE WHEN skill_generated IS NOT NULL AND skill_generated!='' THEN 1 ELSE 0 END) skills,"
                " AVG(ux_score) avg_ux FROM trajectories"
                " GROUP BY COALESCE(source_model,'unknown') ORDER BY trajs DESC"
            ).fetchall()
        finally:
            conn.close()
        return [self._row(r, "model") for r in rows]

    @staticmethod
    def _row(r, key: str) -> dict:
        t = r["trajs"] or 0
        return {
            key: r[key],
            "trajs": t,
            "atoms": r["atoms"] or 0,
            "avg_atoms": round((r["atoms"] or 0) / t, 2) if t else 0.0,
            "skills": r["skills"] or 0,
            "avg_ux": round(r["avg_ux"], 2) if r["avg_ux"] is not None else 0.0,
        }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3.11 -m pytest tests/test_dashboard_metrics.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/xskill/dashboard/__init__.py src/xskill/dashboard/metrics.py tests/test_dashboard_metrics.py
git commit -m "feat(dashboard): DashboardMetrics 衍生指标层 | derived metrics layer"
```

---

### Task 3: 访问控制中间件（仅本机 + 可选密码）

**Files:**
- Create: `src/xskill/dashboard/security.py`
- Test: `tests/test_dashboard_security.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dashboard_security.py
import base64
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from xskill.dashboard.security import DashboardAccessMiddleware


def _app(public, password):
    app = FastAPI()
    app.add_middleware(DashboardAccessMiddleware, public=public, password=password,
                       guarded_prefixes=("/", "/api/v1/dashboard"))
    @app.get("/")
    def root(): return PlainTextResponse("panel")
    @app.get("/api/v1/team/x")
    def team(): return PlainTextResponse("team")
    return app


def test_loopback_allowed_when_not_public():
    c = TestClient(_app(False, ""))           # TestClient 客户端 host = testclient(非 loopback)
    assert c.get("/").status_code == 403       # 默认仅本机 → 非 loopback 被挡
    assert c.get("/api/v1/team/x").status_code == 200  # 非看板路由不受限


def test_public_allows_any_source():
    c = TestClient(_app(True, ""))
    assert c.get("/").status_code == 200


def test_password_requires_basic_auth():
    c = TestClient(_app(True, "s3cret"))
    assert c.get("/").status_code == 401
    tok = base64.b64encode(b"admin:s3cret").decode()
    assert c.get("/", headers={"Authorization": f"Basic {tok}"}).status_code == 200
    bad = base64.b64encode(b"admin:wrong").decode()
    assert c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3.11 -m pytest tests/test_dashboard_security.py -v`
Expected: FAIL（`ModuleNotFoundError: xskill.dashboard.security`）

- [ ] **Step 3: 实现 `security.py`**

```python
# src/xskill/dashboard/security.py
"""看板访问控制:默认仅 loopback;public 时放行;password 非空则 HTTP Basic。

只作用于 guarded_prefixes(看板路由),不碰 /api/v1/team 等其它路由。
"""
from __future__ import annotations

import base64
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class DashboardAccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, public: bool, password: str,
                 guarded_prefixes=("/", "/api/v1/dashboard")):
        super().__init__(app)
        self._public = public
        self._password = password or ""
        self._prefixes = tuple(guarded_prefixes)

    def _guarded(self, path: str) -> bool:
        return path == "/" or any(
            path.startswith(p) for p in self._prefixes if p != "/")

    async def dispatch(self, request, call_next):
        if not self._guarded(request.url.path):
            return await call_next(request)
        if not self._public:
            host = request.client.host if request.client else ""
            if host not in _LOOPBACK:
                return PlainTextResponse("dashboard is local-only", status_code=403)
        if self._password and not self._check_basic(request):
            return PlainTextResponse(
                "auth required", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="xskill"'})
        return await call_next(request)

    def _check_basic(self, request) -> bool:
        h = request.headers.get("authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
        except Exception:  # pylint: disable=broad-exception-caught
            return False
        return hmac.compare_digest(pw, self._password)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3.11 -m pytest tests/test_dashboard_security.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/xskill/dashboard/security.py tests/test_dashboard_security.py
git commit -m "feat(dashboard): 访问控制中间件(仅本机+可选密码) | access-control middleware"
```

---

### Task 4: router（聚合端点 + 静态壳）

**Files:**
- Create: `src/xskill/dashboard/router.py`
- Create: `src/xskill/dashboard/static/index.html`（占位，Task 6 替换为真前端）
- Test: `tests/test_dashboard_router.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dashboard_router.py
from fastapi import FastAPI
from fastapi.testclient import TestClient
from xskill.pipeline.registry import get_connection
from xskill.dashboard.router import build_dashboard_router


def _client(tmp_path):
    db = tmp_path/"r.db"
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES('/cc','cc','claude_code')")
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,status,tasks_extracted,"
                 "skill_generated,ux_score,source_model) VALUES(1,'f','done',6,'s',8.0,'m')")
    conn.commit(); conn.close()
    app = FastAPI()
    app.include_router(build_dashboard_router(db_path=db))
    return TestClient(app)


def test_overview_endpoint(tmp_path):
    r = _client(tmp_path).get("/api/v1/dashboard/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["trajs"] == 1 and body["atoms"] == 6 and body["skill_yield"] == 100.0


def test_by_domain_endpoint(tmp_path):
    r = _client(tmp_path).get("/api/v1/dashboard/by-domain")
    assert r.status_code == 200
    body = r.json()
    assert body["by_ecosystem"][0]["ecosystem"] == "claude_code"
    assert body["by_model"][0]["model"] == "m"


def test_serves_index_html(tmp_path):
    r = _client(tmp_path).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3.11 -m pytest tests/test_dashboard_router.py -v`
Expected: FAIL（`ModuleNotFoundError: xskill.dashboard.router`）

- [ ] **Step 3: 建占位前端 + 实现 router**

```html
<!-- src/xskill/dashboard/static/index.html -->
<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>xskill 控制台</title></head>
<body><h1>xskill 控制台</h1><p>placeholder — Task 6 替换</p></body></html>
```

```python
# src/xskill/dashboard/router.py
"""看板路由:静态壳 GET / + 聚合端点 /api/v1/dashboard/*。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from xskill.dashboard.metrics import DashboardMetrics

_STATIC = Path(__file__).with_name("static")


def build_dashboard_router(db_path: Optional[Path] = None) -> APIRouter:
    router = APIRouter()
    metrics = DashboardMetrics(db_path=db_path)

    @router.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @router.get("/api/v1/dashboard/overview")
    def overview() -> dict:
        return metrics.overview()

    @router.get("/api/v1/dashboard/by-domain")
    def by_domain() -> dict:
        return {"by_ecosystem": metrics.by_ecosystem(), "by_model": metrics.by_model()}

    return router
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3.11 -m pytest tests/test_dashboard_router.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/xskill/dashboard/router.py src/xskill/dashboard/static/index.html tests/test_dashboard_router.py
git commit -m "feat(dashboard): router 聚合端点 + 静态壳 | router endpoints + static shell"
```

---

### Task 5: 在 create_app 按 config 挂载

**Files:**
- Modify: `src/xskill/api/app.py`（`create_app()` 内，返回 app 前）
- Test: `tests/test_dashboard_mount.py`

- [ ] **Step 1: 先确认挂载点**

Run: `grep -n "def create_app\|return app" src/xskill/api/app.py`
Expected: 拿到 `create_app` 起始行与其 `return app` 行号，挂载代码插在该 `return app` 之前。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_dashboard_mount.py
from xskill.dashboard.mount import mount_dashboard
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_mounts_when_enabled(tmp_path):
    db = tmp_path/"r.db"
    from xskill.pipeline.registry import get_connection; get_connection(db).close()
    app = FastAPI()
    mount_dashboard(app, {"dashboard": {"enabled": True, "public": True}}, db_path=db)
    assert TestClient(app).get("/api/v1/dashboard/overview").status_code == 200


def test_not_mounted_when_disabled(tmp_path):
    app = FastAPI()
    mount_dashboard(app, {"dashboard": {"enabled": False}}, db_path=tmp_path/"r.db")
    assert TestClient(app).get("/api/v1/dashboard/overview").status_code == 404
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3.11 -m pytest tests/test_dashboard_mount.py -v`
Expected: FAIL（`ModuleNotFoundError: xskill.dashboard.mount`）

- [ ] **Step 4: 实现 `mount.py`（薄封装，给 app.py 调一行）**

```python
# src/xskill/dashboard/mount.py
"""把看板挂到一个 FastAPI app:include_router + 访问中间件。仅在 enabled 时动。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from xskill.config import dashboard_config
from xskill.dashboard.router import build_dashboard_router
from xskill.dashboard.security import DashboardAccessMiddleware


def mount_dashboard(app, cfg: dict, *, db_path: Optional[Path] = None) -> None:
    dc = dashboard_config(cfg)
    if not dc["enabled"]:
        return
    app.include_router(build_dashboard_router(db_path=db_path))
    app.add_middleware(DashboardAccessMiddleware, public=dc["public"],
                       password=dc["password"])
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python3.11 -m pytest tests/test_dashboard_mount.py -v`
Expected: PASS（2 passed）

- [ ] **Step 6: 在 `create_app()` 接线**

在 `src/xskill/api/app.py` 的 `create_app(...)` 内、`return app` 之前插入（`cfg` 为该函数已加载的配置 dict；若变量名不同，按实际改）：

```python
    # 看板:仅当 config.dashboard.enabled 时挂载(默认不挂)
    from xskill.dashboard.mount import mount_dashboard
    mount_dashboard(app, cfg)
```

若 `create_app` 内没有现成的已加载 `cfg`，在插入处上方加：

```python
    from xskill.config import get_config
    cfg = get_config()
```

- [ ] **Step 7: 跑相关测试确认不回归**

Run: `python3.11 -m pytest tests/test_dashboard_mount.py tests/test_server_watcher_startup.py -v`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add src/xskill/dashboard/mount.py src/xskill/api/app.py tests/test_dashboard_mount.py
git commit -m "feat(dashboard): serve 按 config 挂载看板 | mount dashboard from serve config"
```

---

### Task 6: 真前端（适配 B 内容丰富版，运行时取数）

**Files:**
- Modify: `src/xskill/dashboard/static/index.html`（替换占位，落地 B 版式）
- Create: `src/xskill/dashboard/static/app.js`
- Test: `tests/test_dashboard_frontend_smoke.py`

> 前端以 `xskill.wiki/dashboarddemo/m-brand-rich.html` 为版式蓝本（原 B 配色、左栏分区、各卡片），把写死的 mock 数据改成启动后 `fetch` 真实端点：
> `/api/v1/stats`(成本/角色/模型) · `/api/v1/dashboard/overview`(衍生率) · `/api/v1/dashboard/by-domain`(分域对比) · `/api/v1/trajectories/list`(轨迹状态) · `/api/v1/skills`(技能) · `/api/v1/canary/overview`(灰度)。
> 未埋点指标(推荐触发率/原子采纳率/canary 晋升率)前端渲染为 `—` 并带 `*` tooltip“需埋点”。

- [ ] **Step 1: 写冒烟测试（前端壳与取数脚本可达、含关键挂载点）**

```python
# tests/test_dashboard_frontend_smoke.py
from pathlib import Path
import re

STATIC = Path("src/xskill/dashboard/static")

def test_index_references_appjs_and_tabler():
    html = (STATIC/"index.html").read_text(encoding="utf-8")
    assert "app.js" in html
    assert "tabler" in html.lower()
    assert 'id="pg-overview"' in html   # 分区容器存在

def test_appjs_fetches_overview_endpoint():
    js = (STATIC/"app.js").read_text(encoding="utf-8")
    assert "/api/v1/dashboard/overview" in js
    assert "/api/v1/dashboard/by-domain" in js
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3.11 -m pytest tests/test_dashboard_frontend_smoke.py -v`
Expected: FAIL（`app.js` 不存在 / 断言失败）

- [ ] **Step 3: 落地前端**

- 取 `xskill.wiki/dashboarddemo/m-brand-rich.html` 的 `<head>`/`<style>`/`<body>` 版式作 `index.html`，把 `<script src="app.js">` 引入；保留左栏分区与各卡片骨架，数值用占位 span（如 `<span data-m="overview.trajs">—</span>`）。
- `app.js`：页面加载后并发 `fetch` 上述端点，按 `data-m` 选择器把返回字段填进 DOM；保留分区切换逻辑（沿用 m-brand-rich.html 里的 `data-pg` 事件委托）。未埋点字段保持 `—` + `*`。

```javascript
// src/xskill/dashboard/static/app.js (骨架要点)
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error(u+' '+r.status);return r.json();}
function put(sel,val){document.querySelectorAll(`[data-m="${sel}"]`).forEach(e=>e.textContent=val);}
async function load(){
  const o=await j('/api/v1/dashboard/overview');
  put('overview.trajs',o.trajs); put('overview.atoms',o.atoms);
  put('overview.avg_atoms_per_traj',o.avg_atoms_per_traj);
  put('overview.success_rate',o.success_rate+'%'); put('overview.skill_yield',o.skill_yield+'%');
  put('overview.retry_rate',o.retry_rate+'%'); put('overview.avg_ux',o.avg_ux);
  const d=await j('/api/v1/dashboard/by-domain');     // 渲染分域对比两张表
  renderDomain(d.by_ecosystem,'#eco-body','ecosystem');
  renderDomain(d.by_model,'#model-body','model');
}
load().catch(e=>console.error(e));
```

- [ ] **Step 4: 跑冒烟测试确认通过**

Run: `python3.11 -m pytest tests/test_dashboard_frontend_smoke.py -v`
Expected: PASS

- [ ] **Step 5: 人工验收（serve 起真服务）**

```bash
# 临时在 ~/.xskill/config.yaml 把 dashboard.enabled 改 true 后：
python3.11 -m xskill serve --port 8011 &
curl -s -o /dev/null -w "panel %{http_code}\n" http://127.0.0.1:8011/
curl -s http://127.0.0.1:8011/api/v1/dashboard/overview | head
```
Expected: panel 200；overview 返回真实数字。浏览器打开核对各分区渲染。

- [ ] **Step 6: 提交**

```bash
git add src/xskill/dashboard/static/ tests/test_dashboard_frontend_smoke.py
git commit -m "feat(dashboard): 前端落地(B 版式+运行时取数) | live frontend"
```

---

### Task 7: 收尾 — 全量测试 + 文档勾稽

**Files:**
- Modify: `docs/superpowers/specs/2026-06-01-dashboard-design.md`（把已实现项标注；埋点 backlog 保留）

- [ ] **Step 1: 全量单测**

Run: `make test`
Expected: 全绿（含 5 个新测试文件）

- [ ] **Step 2: lint**

Run: `python3.11 -m pylint src/xskill/dashboard --disable=all --enable=E,W`
Expected: 无 E/W

- [ ] **Step 3: 标注 spec 已落地项并提交**

在 design doc §1 范围处标注 v0 已实现；§5 backlog（recommendation_log / 原子采纳计数 / canary 裁决日志）保留为后续。

```bash
git add docs/superpowers/specs/2026-06-01-dashboard-design.md
git commit -m "docs(dashboard): 标注 v0 已落地项 | mark v0 shipped items"
```

---

## Self-Review

- **Spec 覆盖**：§2 UI→Task 6；§3 子包/隔离→Task 2/3/4/5；§4 CLI 挂载/config/端口→Task 1/5；§5 可算指标→Task 2，未埋点项→前端占位(Task 6)+backlog 保留(Task 7)；§6 测试→每 Task 自带。推荐触发率/原子采纳率/canary 晋升率的**真实数据**明确不在 v0（spec 已声明），无遗漏。
- **占位符**：无 TBD；每步含真实代码/命令/期望。
- **类型一致**：`DashboardMetrics(db_path=...)`、`build_dashboard_router(db_path=...)`、`mount_dashboard(app, cfg, db_path=...)`、`DashboardAccessMiddleware(public,password,guarded_prefixes)` across Task 2–5 一致；端点路径 `/api/v1/dashboard/overview`、`/by-domain` 在 Task 4/5/6 一致。
