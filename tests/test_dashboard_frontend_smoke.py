"""test_dashboard_frontend_smoke.py —— 前端壳与取数脚本静态冒烟"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

STATIC = Path("src/xskill/dashboard/static")


def test_index_references_appjs_and_sections():
    """壳页面引用取数脚本，且五个分区容器齐全。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "app.js" in html
    assert 'id="pg-overview"' in html   # 分区容器存在
    for pg in ("pg-skills", "pg-traj", "pg-users", "pg-canary", "pg-kernels"):
        assert f'id="{pg}"' in html


def test_kernel_page_exposes_switch_and_evaluation_contract():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'data-pg="kernels"' in html
    assert 'id="kernels-list-body"' in html
    assert 'id="kernel-eval-body"' in html
    assert 'id="kernel-runs-body"' in html
    assert "/api/v1/dashboard/admin/kernels" in js
    assert "/api/v1/dashboard/admin/kernels/activate" in js
    assert "loadKernels" in js


def test_index_is_fully_vendored():
    """零外联：内网 headless 环境不允许任何会发起网络请求的外部引用。

    Tailwind 用构建期编译产物内联进 <style id="twcss">（后端只服务 / 与
    /app.js 两个路径，单独的 css 文件会 404，见 static/BUILD.md）。
    minified CSS 里的 MIT license 注释含 URL，不算外部引用。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for pat in ('src="http', "src='http", 'href="http', "href='http",
                "url(http", "@import"):
        assert pat not in html, f"external reference found: {pat}"
    assert "cdn.jsdelivr" not in html          # 旧 Tabler CDN 已移除
    assert 'id="twcss"' in html                # Tailwind 编译产物内联锚点
    assert "tailwindcss" in html               # 内联的确是编译产物


def test_appjs_fetches_overview_endpoint():
    """取数脚本以相对路径 fetch 核心端点。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    # 前端 fetch 用相对路径(去前导 /)以支持经 nginx 子路径反代；断言不带前导
    # 斜杠，对相对/绝对两种写法都成立。
    assert "api/v1/dashboard/overview" in js
    assert "api/v1/dashboard/by-domain" in js


def test_avg_atoms_display_uses_metric_nullability():
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "o.avg_atoms_per_traj != null" in js
    assert "o.trajs > 0 ? o.avg_atoms_per_traj" not in js


def test_avg_atoms_display_behavior_executes_load_overview():
    script = f"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(
  {json.dumps(str(STATIC / "app.js"))},
  'utf8',
);
const end = source.indexOf('async function loadRates()');
if (end < 0) throw new Error('loadRates marker missing');
const overviewSource = source.slice(0, end)
  + '\\nthis.loadOverviewForTest = loadOverview;';

async function renderAverage(trajs, average) {{
  const elements = Object.create(null);
  const document = {{
    querySelectorAll(selector) {{
      if (!elements[selector]) {{
        let text = '';
        const element = {{}};
        Object.defineProperty(element, 'textContent', {{
          get() {{ return text; }},
          set(value) {{ text = String(value); }},
        }});
        elements[selector] = [element];
      }}
      return elements[selector];
    }},
    getElementById() {{ return null; }},
  }};
  const fetch = async url => ({{
    ok: true,
    status: 200,
    json: async () => url.endsWith('/overview')
      ? {{
          trajs,
          atoms: 0,
          avg_atoms_per_traj: average,
          avg_ux: null,
          ux_n: 0,
          retry_rate: 0,
          filtered: 0,
          success_rate: 0,
          price_health: null,
        }}
      : {{ stages: {{ done: 0, error: 0 }} }},
  }});
  const context = vm.createContext({{
    document,
    fetch,
    console: {{ error() {{}} }},
  }});
  vm.runInContext(overviewSource, context);
  await context.loadOverviewForTest();
  return elements['[data-m="overview.avg_atoms_per_traj"]'][0].textContent;
}}

(async () => {{
  const rendered = [];
  rendered.push(await renderAverage(8, null));
  rendered.push(await renderAverage(8, 0));
  rendered.push(await renderAverage(0, 2.75));
  process.stdout.write(JSON.stringify(rendered));
}})().catch(error => {{
  console.error(error);
  process.exit(1);
}});
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(result.stdout) == ["—", "0", "2.75"]


def test_avg_atoms_tooltip_describes_matching_split_scope():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    adjacent_hint = re.search(
        r'data-m="overview\.avg_atoms_per_traj"[^>]*>[^<]*</span>\s*'
        r'<span class="hint" title="([^"]+)">ⓘ</span>',
        html,
    )

    assert adjacent_hint is not None
    assert adjacent_hint.group(1) == (
        "已成功拆分轨迹的原子数之和 ÷ 已成功拆分轨迹数。"
    )


def test_appjs_routes_skillhub_detail_by_source():
    """技能详情按 source 分流：三方(skillhub)技能调 skillhub ux 端点，
    自产技能不受影响。断言存在 source==='skillhub' 分支与 skillhub ux
    端点调用（版本聚合 + 关联原子）。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    # 基于 source 的分流判定
    assert "source === 'skillhub'" in js
    assert "s.source === 'native'" in js
    # 调三方 ux 端点(版本聚合分)与关联原子端点，而非自产 detail
    assert "dashboard/skillhub/" in js
    assert "/ux?days=" in js
    assert "/ux/atoms?days=" in js
