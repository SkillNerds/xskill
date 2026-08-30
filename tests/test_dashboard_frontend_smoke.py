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
    for pg in ("pg-skills", "pg-traj", "pg-users", "pg-canary"):
        assert f'id="{pg}"' in html


def test_language_switch_loads_local_i18n_before_app():
    """语言开关常驻侧栏，翻译层先于动态渲染脚本启动。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'data-language="en"' in html
    assert 'data-language="zh"' in html
    assert "English" in html and "中文" in html
    assert html.index('src="i18n.js') < html.index('src="app.js')
    assert 'src="i18n.js?v=' in html


def test_i18n_translates_static_and_dynamic_dashboard_copy():
    """纯翻译函数覆盖静态标签和带运行时数字的文案。"""
    script = f"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync({json.dumps(str(STATIC / "i18n.js"))}, 'utf8');
const context = vm.createContext({{ window: {{}} }});
vm.runInContext(source, context);
const i18n = context.window.XSkillI18n;
process.stdout.write(JSON.stringify([
  i18n.translateText('总览', 'en'),
  i18n.translateText('42 份使用打分', 'en'),
  i18n.translateText('席位 3 · 已跑 8s', 'en'),
  i18n.translateText('总览', 'zh'),
]));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(result.stdout) == [
        "Overview",
        "42 usage ratings",
        "Slot 3 · running for 8s",
        "总览",
    ]


def test_i18n_covers_detail_and_operational_ui_copy():
    """English mode must cover UI created after routing or API responses."""
    samples = [
        "总触发",
        "· 贡献原子",
        "点黄点 / main HEAD 看推送给谁；其它节点看 diff",
        "main HEAD · 点击看推送给谁",
        "来源模型",
        "点击跳原子详情",
        "触发 / UX / 去重原子 / 首用",
        "版本（点击看 diff）",
        "点左侧文件或版本、或进化路径节点查看",
        "描述质量信号——真跑代理在语义相关技能清单里抢触发；区别于上方\"总触发\"的线上真实使用",
        "无 case（该 skill 还没跑过触发优化）",
        "按链表序 pre/post_atom_id · 点击节点查看详情",
        "traj — atom — skill · 贡献边标 weightscore · 点节点跳转",
        "demo-skill（weightscore 9 · 已采纳）",
        "行 6575 – 7550",
        "原文切片（按 offset 行号定位 · 只读）",
        "（截取 8000/59767 字符）",
        "席位 3 · 已跑 8s",
        "这一栏同时拆几条轨迹。占满后新轨迹先等。下一轮扫描生效，不用重启。",
        "安装个数为 0：服务器仍可能推送，但本机不装 harness",
        "alice 的当前推送 4 槽 · pinned=1 blocked=2",
        "暂停后仍会接收并保存轨迹，恢复后自动补处理。可填写暂停原因：",
        "删除不可逆：skill 目录与 git 历史将被移除。\n请输入 skill 名确认: demo",
        "相似度 0.8 · 共同标签:android/testing",
    ]
    script = f"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync({json.dumps(str(STATIC / "i18n.js"))}, 'utf8');
const context = vm.createContext({{ window: {{}} }});
vm.runInContext(source, context);
const translated = {json.dumps(samples, ensure_ascii=False)}
  .map(value => context.window.XSkillI18n.translateText(value, 'en'));
process.stdout.write(JSON.stringify(translated));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    translated = json.loads(result.stdout)
    assert len(translated) == len(samples)
    assert all(not re.search(r"[\u3400-\u9fff]", value) for value in translated)


def test_i18n_persists_language_and_observes_dynamic_content():
    js = (STATIC / "i18n.js").read_text(encoding="utf-8")

    assert "xskill.dashboard.language" in js
    assert "localStorage.setItem(STORAGE_KEY, next)" in js
    assert "new global.MutationObserver" in js


def test_i18n_round_trip_restores_source_copy():
    """English → 中文 必须恢复源文案，不能把英文误记为新的源文案。"""
    script = f"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync({json.dumps(str(STATIC / "i18n.js"))}, 'utf8');
const text = {{ nodeType: 3, nodeValue: '总览', parentElement: {{ tagName: 'DIV' }} }};
const body = {{
  nodeType: 1,
  tagName: 'BODY',
  childNodes: [text],
  hasAttribute() {{ return false; }},
}};
const document = {{
  body,
  documentElement: {{}},
  title: 'xskill 控制台',
  querySelectorAll() {{ return []; }},
  addEventListener() {{}},
  dispatchEvent() {{}},
}};
const window = {{
  document,
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
  MutationObserver: class {{ observe() {{}} }},
  CustomEvent: class {{}},
}};
const context = vm.createContext({{ window }});
vm.runInContext(source, context);
window.XSkillI18n.setLanguage('en');
const english = text.nodeValue;
window.XSkillI18n.setLanguage('zh');
process.stdout.write(JSON.stringify([english, text.nodeValue]));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(result.stdout) == ["Overview", "总览"]


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


def test_admin_drawer_separates_current_push_from_history():
    """当前推送不回退成历史总数；历史曝光独立按需分页。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "u.current_slots != null ? u.current_slots : '—'" in js
    assert "u.current_slots != null ? u.current_slots : u.exposures" not in js
    assert "adm-history-toggle" in js
    assert "async function loadAdminRecommendationHistory(" in js
    assert "/recommendations?offset=" in js
    assert "按首次曝光时间倒序" in js
    assert "adm-history-page" in js


def test_pipeline_log_scroll_is_sticky_not_forced():
    """#178: 流水线日志仅在贴底时跟随；pmStartLog 用 kind/name 比较，避免抽屉重渲染重启轮询。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function pmLogNearBottom(" in js
    assert "if (stickBottom) log.scrollTop = log.scrollHeight" in js
    # 禁止无条件贴底（旧逻辑）
    assert re.search(
        r"if \(r\.truncated\).*?\n\s*log\.scrollTop = log\.scrollHeight",
        js,
        re.S,
    ) is None
    assert "pmLogKey.kind === kind && pmLogKey.name === name" in js
    assert "回流受阻" in js
    assert "reverse_sync" in js
    # 抽屉重渲染后恢复滚动
    assert "stickBottom ? newLog.scrollHeight : savedScroll" in js


def test_pm_log_near_bottom_behavior():
    """pmLogNearBottom：贴底/上滚两种姿态。"""
    script = f"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync({json.dumps(str(STATIC / "app.js"))}, 'utf8');
const start = source.indexOf('function pmLogNearBottom(');
const end = source.indexOf('function pmRenderDrawer(');
if (start < 0 || end < 0) throw new Error('pmLogNearBottom marker missing');
const context = vm.createContext({{}});
vm.runInContext(source.slice(start, end) + '\\nthis.pmLogNearBottom = pmLogNearBottom;', context);
const near = context.pmLogNearBottom;
const bottom = {{ scrollHeight: 1000, scrollTop: 920, clientHeight: 80 }};
const mid = {{ scrollHeight: 1000, scrollTop: 200, clientHeight: 80 }};
process.stdout.write(JSON.stringify([near(null), near(bottom), near(mid)]));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == [True, True, False]
