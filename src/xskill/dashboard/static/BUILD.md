# dashboard static 构建说明 / Build Notes

前端为零构建运行时（vanilla JS + Tailwind 编译产物），浏览器端不加载任何
CDN / 外部资源。`i18n.js` 提供稳定 key 的翻译表与语言状态；静态文案通过
`data-i18n`、动态文案通过渲染时调用 `tr()` 完成翻译，API 数据与日志不做
翻译。语言选择保存在浏览器 localStorage。仅当改动 `index.html` /
`app.js` / `i18n.js` 里的 Tailwind 类名后需要
重新编译 CSS。

## 为什么 CSS 是内联的而不是 `<link href="tw.css">`

后端路由（`router.py`）只服务 `/`（index.html）、`/app.js` 与 `/i18n.js`
三个静态路径，
没有静态目录挂载——单独的 `tw.css` 请求会 404。因此编译产物 `tw.css`
的内容被整体内联进 `index.html` 的 `<style id="twcss">…</style>` 块。
`tw.css` 文件保留在仓库里作为构建产物与 diff 基准。

## 重新构建步骤

1. 安装 Tailwind CLI（一次性，任意目录；内网可用 npm 镜像）：

   ```bash
   cd /tmp && mkdir -p twbuild && cd twbuild && npm i tailwindcss@3
   ```

2. 编译（在本目录 `src/xskill/dashboard/static/` 下执行）：

   ```bash
   npx --prefix /tmp/twbuild tailwindcss \
     -c tailwind.config.js -i tw.in.css -o tw.css --minify
   ```

   `tailwind.config.js` 的 `content` 指向 `./index.html`、`./app.js` 与
   `./i18n.js`，
   按需产出实际用到的类。app.js 里所有类名均为完整字面量
   （颜色映射表如 `STATE_BADGE`/`AV_COLORS` 不做字符串拼接），
   保证扫描器能全部捕获。

3. 把 `tw.css` 内联回 `index.html`：

   ```bash
   python3 - <<'EOF'
   import re
   from pathlib import Path
   html = Path("index.html").read_text(encoding="utf-8")
   css = Path("tw.css").read_text(encoding="utf-8").strip()
   new, n = re.subn(r'(<style id="twcss">).*?(</style>)',
                    lambda m: m.group(1) + css + m.group(2), html, flags=re.S)
   assert n == 1
   Path("index.html").write_text(new, encoding="utf-8")
   EOF
   ```

4. 验证：`python3.11 -m pytest tests/test_dashboard_frontend_smoke.py -q`
   （断言零外联 + 编译产物已内联）。

## npm 完全不可用时的退路

把 `openspec/changes/dashboard-console-redesign/mockups/tailwind.js`
复制进本目录，`index.html` 里以 `<script src="tailwind.js">` 引入
（本地文件，仍是零外联）——但需要后端补一条静态路由才能被服务，
故仅作最后手段。当前采用的是编译产物内联方案，无需该退路。
