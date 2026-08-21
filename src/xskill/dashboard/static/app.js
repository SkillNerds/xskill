// xskill 控制台前端（P1 重写）：Tailwind 视觉体系 + SPA-lite hash 路由。
// 各端点独立加载互不阻塞；指标算不出（分母 0/无记录）显示 — 而非 0%。

// ── 基础工具 ─────────────────────────────────────────────────────
const _cache = {};
async function j(u) {
  const r = await fetch(u);
  if (!r.ok) throw new Error(u + ' ' + r.status);
  return r.json();
}
// 同一端点多个渲染方共享一次请求
const jc = u => (_cache[u] ||= j(u));

function put(sel, val) {
  document.querySelectorAll(`[data-m="${sel}"]`).forEach(e => { e.textContent = val; });
}
function rows(bodyId, html, empty) {
  const tb = document.getElementById(bodyId);
  if (tb) tb.innerHTML = html
    || `<tr><td colspan="9" class="py-2 text-slate-400">${empty || '暂无数据'}</td></tr>`;
}
const money = n => '$' + (Number(n) || 0).toFixed(4);
const tok = n => { n = Number(n) || 0; return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : '' + n; };
// 任何要塞进 innerHTML 的值一律转义（model 名可能是 `<synthetic>`）
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
// ux 是 1–10 分；null/0 = 还没有评分，显示 —
const ux = v => (v == null || Number(v) === 0) ? '—' : v;
// 分母为 0 → —（无数据 ≠ 0%）
const pctOr = (rate, denom) => (denom > 0 ? rate + '%' : '—');
const fdate = ts => esc(String(ts || '').replace('T', ' ').slice(0, 16)) || '—';

// 用户头像圈：名字前 2 字符 + 确定性配色（完整类名字面量，供 Tailwind 扫描）
const AV_COLORS = ['bg-indigo-100 text-indigo-700', 'bg-sky-100 text-sky-700',
  'bg-amber-100 text-amber-700', 'bg-rose-100 text-rose-700',
  'bg-emerald-100 text-emerald-700', 'bg-violet-100 text-violet-700'];
function avatar(name, size) {
  let h = 0;
  for (const ch of String(name || '?')) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  const cls = AV_COLORS[h % AV_COLORS.length];
  const sz = size === 'sm' ? 'w-5 h-5 text-[9px]' : 'w-6 h-6 text-[10px]';
  return `<span class="${sz} rounded-full ${cls} inline-flex items-center justify-center font-bold shrink-0">${esc(String(name || '?').slice(0, 2))}</span>`;
}
// 占比条
function bar(pct, color) {
  return `<div class="flex-1 h-1.5 rounded-full bg-slate-100 min-w-[60px]"><div class="h-full rounded-full ${color || 'bg-teal-500'}" style="width:${Math.max(0, Math.min(100, pct)).toFixed(1)}%"></div></div>`;
}

// ── 总览 ─────────────────────────────────────────────────────────
async function loadOverview() {
  const o = await jc('api/v1/dashboard/overview');
  put('overview.trajs', o.trajs);
  put('overview.atoms', o.atoms);
  put('overview.avg_atoms_per_traj', o.avg_atoms_per_traj != null ? o.avg_atoms_per_traj : '—');
  put('overview.avg_ux', (o.ux_n > 0 && o.avg_ux != null) ? o.avg_ux : '—');
  put('overview.ux_n', o.ux_n > 0 ? `${o.ux_n} 份使用打分` : '还没有使用打分');
  put('overview.retry_rate', o.trajs > 0 ? o.retry_rate + '%' : '—');
  put('overview.filtered', o.filtered > 0 ? `filtered ${o.filtered} 条不进分母` : '');
  // 成功率是终态口径：分母 = done+error+filtered。done/error 在 pipeline 端点里。
  try {
    const p = await jc('api/v1/dashboard/pipeline');
    const finished = (p.stages.done || 0) + (p.stages.error || 0) + (o.filtered || 0);
    put('overview.success_rate', finished > 0 ? o.success_rate + '%' : '—');
  } catch (e) {
    put('overview.success_rate', o.trajs > 0 ? o.success_rate + '%' : '—');
  }
  const h = o.price_health, el = document.getElementById('price-warn');
  if (el && h && h.ok === false) {
    const reason = { schema_changed: '上游格式变更', source_moved: '上游地址失效', unreachable: '上游不可达' }[h.kind] || '刷新异常';
    el.innerHTML = `<div class="mt-2 rounded-xl bg-amber-50/70 ring-1 ring-amber-100 px-3.5 py-2 text-[11px] text-amber-700">价格表 ${h.stale_days != null ? h.stale_days + 'd' : '从未'} 未刷新 · ${reason}，沿用旧价</div>`;
  }
}

async function loadRates() {
  const r = await jc('api/v1/dashboard/rates');
  const recsTotal = (r.trigger.by_skill || []).reduce((a, s) => a + (s.recommended || 0), 0);
  put('rates.trigger', pctOr(r.trigger.overall, recsTotal));
  put('rates.adoption', pctOr(r.adoption.rate, r.adoption.total));
  put('rates.promotion', pctOr(r.promotion.rate, r.promotion.decided));
  put('rates.promotion2', pctOr(r.promotion.rate, r.promotion.decided));
  put('promotion.detail', r.promotion.decided > 0
    ? `${r.promotion.promoted}/${r.promotion.decided} 已裁决` : '还没有灰度裁决');
  rows('trigger-body', (r.trigger.by_skill || []).map(s =>
    `<tr><td class="py-2 font-medium text-slate-800">${esc(s.skill)}</td>`
    + `<td class="text-right tabular-nums">${s.recommended}</td>`
    + `<td class="text-right tabular-nums">${s.used}</td>`
    + `<td class="text-right"><div class="flex items-center gap-2 justify-end">${bar(s.rate)}<span class="tabular-nums text-[11px] text-slate-500 w-10 text-right">${pctOr(s.rate, s.recommended)}</span></div></td></tr>`).join(''),
    '还没有推荐曝光记录');
}

const STAGE_DEFS = [
  ['pending_split', '待拆分'], ['splitting', '拆分中'],
  ['clustering', '聚类分派中'], ['done', '已完成'], ['error', '错误'],
];
async function loadPipeline() {
  const p = await jc('api/v1/dashboard/pipeline');
  const cells = STAGE_DEFS.map(([k, label]) => {
    const n = p.stages[k] || 0;
    const active = n > 0 && (k === 'splitting' || k === 'clustering');
    const isErr = k === 'error' && n > 0;
    if (active) return `<div class="flex-1 min-w-[92px] rounded-xl ring-1 ring-teal-200 bg-teal-50/50 px-3.5 py-3">
      <div class="text-[11px] text-teal-600 flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-teal-500 animate-pulse"></span>${label}</div>
      <div class="mt-1 text-xl font-semibold tabular-nums text-teal-700">${n}</div></div>`;
    if (isErr) return `<div class="flex-1 min-w-[92px] rounded-xl ring-1 ring-rose-200 bg-rose-50/50 px-3.5 py-3">
      <div class="text-[11px] text-rose-600">${label}</div>
      <div class="mt-1 text-xl font-semibold tabular-nums text-rose-700">${n}</div></div>`;
    return `<div class="flex-1 min-w-[92px] rounded-xl ring-1 ring-slate-200 px-3.5 py-3">
      <div class="text-[11px] text-slate-400">${label}</div>
      <div class="mt-1 text-xl font-semibold tabular-nums">${n}</div></div>`;
  }).join('<div class="self-center text-slate-300 shrink-0">→</div>');
  document.getElementById('pipe-stages').innerHTML = cells;
  // 冷启动屏障：signal 不存在（null）整块不渲染
  const cold = document.getElementById('pipe-cold');
  cold.innerHTML = (p.cold_start && p.cold_start.active)
    ? `<div class="mt-4 rounded-xl bg-slate-50 ring-1 ring-slate-100 px-4 py-3 flex items-center gap-2">
        <span class="w-1.5 h-1.5 rounded-full bg-teal-500 animate-pulse"></span>
        <span class="text-xs font-medium text-slate-600">冷启动屏障激活中</span>
        <span class="text-[11px] text-slate-400">收集满后统一蒸馏，避免碎片化 skill</span></div>`
    : '';
  const cands = document.getElementById('pipe-cands');
  if (!(p.candidates || []).length) { cands.innerHTML = ''; return; }
  cands.innerHTML = `<div class="text-[11px] text-slate-400 mb-2">候选孵化进度 · weightscore 满 ${esc(p.candidates[0].threshold)} 触发蒸馏</div>
    <div class="space-y-3">` + p.candidates.map(c => `
      <div>
        <div class="flex items-baseline justify-between">
          <span class="font-medium text-slate-800 text-xs">${esc(c.skill)}</span>
          <span class="text-[11px] tabular-nums ${c.progress >= 0.8 ? 'text-teal-700' : 'text-slate-600'} font-semibold">${esc(c.weightscore)} <span class="text-slate-300 font-normal">/ ${esc(c.threshold)}</span></span>
        </div>
        <div class="mt-1.5 h-2 rounded-full bg-slate-100 overflow-hidden"><div class="h-full rounded-full bg-teal-500" style="width:${(c.progress * 100).toFixed(0)}%"></div></div>
        <div class="mt-1 text-[10.5px] text-slate-400">${c.atoms} 个原子贡献</div>
      </div>`).join('') + '</div>';
}

function shareBars(elId, arr, key) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!(arr || []).length) { el.innerHTML = '<span class="text-slate-400">暂无数据</span>'; return; }
  const total = arr.reduce((a, r) => a + (r.trajs || 0), 0) || 1;
  const max = Math.max(...arr.map(r => r.trajs || 0)) || 1;
  el.innerHTML = arr.map(r => `
    <div class="flex items-center gap-2.5">
      <span class="w-24 text-slate-600 text-xs text-right truncate" title="${esc(r[key])}">${esc(r[key])}</span>
      ${bar(r.trajs / max * 100)}
      <span class="tabular-nums text-slate-500 w-9 text-right text-[11px]">${Math.round(r.trajs / total * 100)}%</span>
      <span class="tabular-nums text-slate-400 w-20 text-right text-[11px]">${r.trajs} · ${r.atoms} 原子</span>
    </div>`).join('');
}
async function loadDomain() {
  const d = await jc('api/v1/dashboard/by-domain');
  shareBars('eco-bars', d.by_ecosystem, 'ecosystem');
  shareBars('model-bars', d.by_model, 'model');
}

async function loadCost() {
  const c = await jc('api/v1/dashboard/cost');
  put('cost.today', money(c.today_usd));
  put('cost.total', money(c.total_usd));
  put('cost.tokens', tok(c.total_tokens));
  put('cost.calls', c.total_calls);
  rows('cost-model-body', (c.by_model || []).map(m =>
    `<tr><td class="py-2">${esc(m.model)}</td><td class="text-right tabular-nums">${tok(m.tokens)}</td><td class="text-right tabular-nums">${m.calls}</td><td class="text-right tabular-nums">${money(m.cost)}</td></tr>`).join(''),
    '还没有调用记录');
  rows('cost-step-body', (c.by_step || []).map(s =>
    `<tr><td class="py-2">${esc(s.step)}</td><td class="text-right tabular-nums">${tok(s.tokens)}</td><td class="text-right tabular-nums">${money(s.cost)}</td></tr>`).join(''),
    '还没有调用记录');
}

// ── 技能库 ───────────────────────────────────────────────────────
const STATE_BADGE = {
  main: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
  staging: 'bg-amber-50 text-amber-700 ring-1 ring-amber-200',
  baby: 'bg-sky-50 text-sky-700 ring-1 ring-sky-200',
  unknown: 'bg-slate-100 text-slate-500',
};
const stateBadge = s =>
  `<span class="px-2 py-0.5 rounded-md text-[11px] font-medium ${STATE_BADGE[s] || STATE_BADGE.unknown}">${esc(s)}</span>`;

// 来源徽章：skillhub 三方技能标醒目的"第三方"并显示 hub 来源；自产技能标淡色"自产"。
const sourceBadge = s => s.source === 'skillhub'
  ? `<span class="ml-2 inline-block px-2 py-0.5 rounded-md text-[11px] font-medium bg-indigo-100 text-indigo-700">第三方 · skillhub</span>`
    + (s.hub ? `<span class="ml-2 inline-block text-[11px] text-slate-400">${esc(s.hub)}</span>` : '')
  : s.source === 'native'
    ? `<span class="ml-2 inline-block px-2 py-0.5 rounded-md text-[11px] font-medium bg-slate-100 text-slate-500">自产</span>`
    : '';

// 海量 skill(如 1 万条)分页:一次只拉/渲一页,别让前端一次性渲 1 万行 DOM 炸锅。
let skillsPage = 0;
const SKILLS_PAGE_SIZE = 100;

async function loadSkills() {
  const off = skillsPage * SKILLS_PAGE_SIZE;
  const d = await jc(`api/v1/dashboard/skills?limit=${SKILLS_PAGE_SIZE}&offset=${off}`);
  const bs = d.by_state || {};
  const parts = Object.keys(bs).sort().map(k => `${k} ${bs[k]}`).join(' · ');
  put('skills.summary', `共 ${d.total} 个${parts ? ' · ' + parts : ''}`);
  rows('skills-body', (d.skills || []).map(s =>
    `<tr class="hover:bg-slate-50 cursor-pointer" data-skill-row="${esc(s.name)}">`
    + `<td class="py-2.5 font-medium text-teal-700">${esc(s.name)}${sourceBadge(s)}</td>`
    + `<td>${stateBadge(s.state)}</td>`
    + `<td class="text-slate-500 max-w-[480px] truncate" title="${esc(s.description)}">${esc(s.description) || '—'}</td>`
    + `<td class="text-right tabular-nums">v${esc(s.version)}</td>`
    + `<td class="text-right tabular-nums">${s.candidates || 0}</td></tr>`).join(''),
    '技能库还是空的');
  renderSkillsPager(d.total || 0);
}

function renderSkillsPager(total) {
  const pager = document.getElementById('skills-pager');
  if (!pager) return;
  const pages = Math.max(1, Math.ceil(total / SKILLS_PAGE_SIZE));
  if (pages <= 1) { pager.innerHTML = ''; return; }
  if (skillsPage > pages - 1) skillsPage = pages - 1;
  const btn = (label, page, disabled) =>
    `<button class="px-2 py-0.5 rounded border border-slate-200 ${disabled ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-50'}"`
    + `${disabled ? ' disabled' : ` data-skills-page="${page}"`}>${label}</button>`;
  pager.innerHTML = btn('‹ 上一页', skillsPage - 1, skillsPage <= 0)
    + `<span>第 ${skillsPage + 1} / ${pages} 页 · 共 ${total} 个</span>`
    + btn('下一页 ›', skillsPage + 1, skillsPage >= pages - 1);
  pager.querySelectorAll('[data-skills-page]').forEach(b => {
    b.onclick = () => { skillsPage = parseInt(b.getAttribute('data-skills-page'), 10) || 0; loadSkills(); };
  });
}

// 进化路径：git-log 式行视图（mockup ①）。main 泳道 x=22，staging/rejected x=64。
function renderGraph(g) {
  const all = (g.nodes || []).slice();
  // API 按 ts 降序；同秒提交（自动化链路常见）ts 相同,再按父子拓扑深度
  // 决序（子在前）——否则 HEAD 可能排到祖先下面。
  const bySha = new Map(all.map(n => [n.sha, n]));
  const gen = new Map();
  const depth = sha => {
    if (!bySha.has(sha)) return 0;
    if (gen.has(sha)) return gen.get(sha);
    gen.set(sha, 0); // 防环
    const d = 1 + Math.max(0, ...(bySha.get(sha).parents || []).map(depth));
    gen.set(sha, d);
    return d;
  };
  all.forEach(n => depth(n.sha));
  all.sort((a, b) => (b.ts - a.ts) || (gen.get(b.sha) - gen.get(a.sha)));
  const nodes = all.slice(0, 30);
  if (!nodes.length) return '<div class="text-slate-400 text-xs mt-3">还没有提交历史</div>';
  const ROW = 48, top = 24;
  const xOf = n => (n.lanes || []).includes('main') ? 22 : 64;
  const hasStg = nodes.some(n => xOf(n) === 64);
  const laneRows = x => nodes.map((n, i) => xOf(n) === x ? i : -1).filter(i => i >= 0);
  const laneLine = x => {
    const rs = laneRows(x);
    if (rs.length < 2) return '';
    return `<line x1="${x}" y1="${top + rs[0] * ROW}" x2="${x}" y2="${top + rs[rs.length - 1] * ROW}" stroke="#e2e8f0" stroke-width="2"/>`;
  };
  const dots = nodes.map((n, i) => {
    const y = top + i * ROW, x = xOf(n);
    if (n.decision === 'promoted') return `<circle cx="${x}" cy="${y}" r="6.5" fill="#10b981"/>`;
    if (n.decision === 'rejected') return `<circle cx="${x}" cy="${y}" r="6.5" fill="#f43f5e"/>`;
    if (n.is_head_staging) return `<circle cx="${x}" cy="${y}" r="6.5" fill="#fbbf24"/>`;
    return `<circle cx="${x}" cy="${y}" r="6.5" fill="#fff" stroke="#94a3b8" stroke-width="2"/>`;
  }).join('');
  const svg = `<svg width="88" height="${top + (nodes.length - 1) * ROW + 24}" class="shrink-0">
    <text x="22" y="10" font-size="9.5" fill="#94a3b8" text-anchor="middle">main</text>
    ${hasStg ? '<text x="64" y="10" font-size="9.5" fill="#94a3b8" text-anchor="middle">staging</text>' : ''}
    ${laneLine(22)}${laneLine(64)}${dots}</svg>`;
  const rowsHtml = nodes.map(n => {
    let sub = '', subCls = 'text-slate-400', rowCls = '';
    if (n.decision === 'promoted') {
      const d = n.decision_detail || {};
      sub = `晋升${d.staging_avg != null && d.main_avg != null ? ` · ${d.staging_avg} > ${d.main_avg}` : ''}${n.is_head_main ? ' · main HEAD' : ''}`;
      subCls = 'text-emerald-600';
    } else if (n.decision === 'rejected') {
      const d = n.decision_detail || {};
      sub = `回滚${d.staging_avg != null && d.main_avg != null ? ` · ${d.staging_avg} < ${d.main_avg}` : ''}`;
      subCls = 'text-rose-600';
    } else if (n.is_head_staging) {
      sub = '灰度观察中 · staging HEAD'; subCls = 'text-amber-700';
      rowCls = 'bg-amber-50/60 ring-1 ring-amber-100';
    } else if (n.is_head_main) {
      sub = 'main HEAD'; subCls = 'text-slate-500';
    } else {
      sub = (n.lanes || []).includes('main') ? 'main 提交' : 'staging 提交';
    }
    const rej = (n.lanes || []).includes('rejected') && n.decision !== 'rejected'
      ? ' <span class="px-1.5 py-0.5 rounded bg-rose-50 text-rose-600 text-[10px]">rejected</span>' : '';
    return `<div class="h-12 flex items-center justify-between gap-2 rounded-lg px-2 -mx-2 cursor-pointer hover:bg-slate-50 ${rowCls}" data-gnode="${esc(n.sha)}">
      <div class="min-w-0"><div class="font-medium truncate">${esc(n.subject) || '(无提交说明)'}${rej}</div>
        <div class="text-[11px] ${subCls}">${esc(sub)}</div></div>
      <code class="text-[11px] text-slate-400 shrink-0">${esc(n.sha.slice(0, 7))}</code></div>`;
  }).join('');
  const unloc = (g.decisions_unlocated || []).length;
  return `<div class="flex mt-3">${svg}<div class="flex-1 min-w-0" style="padding-top:2px">${rowsHtml}</div></div>
    <div class="flex gap-4 mt-3 pt-3 border-t border-slate-100 text-[11px] text-slate-500 flex-wrap">
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-emerald-500"></span>晋升</span>
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-rose-500"></span>回滚</span>
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-amber-400"></span>观察中</span>
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-white ring-2 ring-slate-300"></span>普通提交</span>
      ${(g.nodes || []).length > 30 ? '<span class="text-slate-400">仅显示最近 30 个节点</span>' : ''}
    </div>
    ${unloc ? `<div class="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-[11px] text-slate-400">${unloc} 条历史裁决无法定位到节点</div>` : ''}`;
}

// 得分趋势：main/staging 双折线（main 实线 blue-600 / staging 虚线 emerald-500）
function renderDual(daily) {
  const pts = (daily || []).filter(d => d.avg_ux != null);
  if (!pts.length) return '<div class="text-slate-400 text-xs mt-3">还没有使用打分</div>';
  const dates = [...new Set(pts.map(p => p.date))].sort();
  const W = 620, H = 200, L = 34, R = 12, T = 16, B = 26;
  const xOf = d => dates.length > 1
    ? L + dates.indexOf(d) / (dates.length - 1) * (W - L - R) : (L + W - R) / 2;
  const yOf = v => T + (10 - v) / 10 * (H - T - B);
  const series = side => pts.filter(p => p.side === side)
    .sort((a, b) => a.date < b.date ? -1 : 1);
  const line = (arr, color, dash) => {
    if (!arr.length) return '';
    const path = arr.map((p, i) => `${i ? 'L' : 'M'}${xOf(p.date).toFixed(1)} ${yOf(p.avg_ux).toFixed(1)}`).join(' ');
    return `<path d="${path}" fill="none" stroke="${color}" stroke-width="2"${dash ? ' stroke-dasharray="5 4"' : ''}/>`
      + arr.map(p => `<circle class="trend-pt cursor-pointer" data-day="${esc(p.date)}" data-side="${esc(p.side)}" cx="${xOf(p.date).toFixed(1)}" cy="${yOf(p.avg_ux).toFixed(1)}" r="4.5" fill="${color}" stroke="#fff" stroke-width="1.5"><title>${esc(p.date)} ${esc(p.side)} ${p.avg_ux} · ${p.n} 份 · 点击看当日原子</title></circle>`).join('');
  };
  const grid = [2, 4, 6, 8, 10].map(v =>
    `<line x1="${L}" y1="${yOf(v)}" x2="${W - R}" y2="${yOf(v)}" stroke="#f1f5f9"/>`
    + `<text x="${L - 5}" y="${yOf(v) + 3}" font-size="10" fill="#94a3b8" text-anchor="end">${v}</text>`).join('');
  const step = Math.max(1, Math.ceil(dates.length / 6));
  const xlabels = dates.filter((_, i) => i % step === 0 || i === dates.length - 1).map(d =>
    `<text x="${xOf(d).toFixed(1)}" y="${H - 8}" font-size="10" fill="#94a3b8" text-anchor="middle">${esc(d.slice(5))}</text>`).join('');
  return `<div class="flex gap-4 text-[11px] text-slate-500 mt-2">
      <span class="flex items-center gap-1.5"><span class="w-4 h-0.5 bg-blue-600 rounded"></span>main</span>
      <span class="flex items-center gap-1.5"><svg width="16" height="2"><line x1="0" y1="1" x2="16" y2="1" stroke="#10b981" stroke-width="2" stroke-dasharray="4 3"/></svg>staging</span>
    </div>
    <svg viewBox="0 0 ${W} ${H}" class="w-full mt-1" style="max-height:220px">${grid}
      <line x1="${L}" y1="${H - B + 4}" x2="${W - R}" y2="${H - B + 4}" stroke="#e2e8f0"/>${xlabels}
      ${line(series('main'), '#2563eb', false)}${line(series('staging'), '#10b981', true)}</svg>
    <div id="trend-drill"></div>`;
}

// C4：趋势点下钻——取该 skill 的逐条打分（/ux/atoms），按日期+side 过滤展示
async function drillTrendDay(skill, day, side) {
  const box = document.getElementById('trend-drill');
  if (!box) return;
  box.innerHTML = '<div class="text-slate-400 text-[11px] mt-2">加载当日原子…</div>';
  let data;
  try {
    data = await j('api/v1/dashboard/skill/' + encodeURIComponent(skill)
      + '/ux/atoms?days=365' + (side ? '&side=' + encodeURIComponent(side) : ''));
  } catch (e) {
    box.innerHTML = `<div class="text-rose-600 text-[11px] mt-2">下钻失败：${esc(e.message)}</div>`;
    return;
  }
  const rows_ = (data.scores || []).filter(r => (r.scored_at || '').slice(0, 10) === day);
  if (!rows_.length) {
    box.innerHTML = `<div class="text-slate-400 text-[11px] mt-2">${esc(day)} · ${esc(side)}：无逐条记录</div>`;
    return;
  }
  box.innerHTML = `<div class="mt-2 rounded-xl ring-1 ring-slate-100 divide-y divide-slate-50">
    <div class="px-3 py-1.5 text-[11px] text-slate-400">${esc(day)} · ${esc(side)} · ${rows_.length} 份打分</div>
    ${rows_.map(r => `<div class="px-3 py-2 flex items-center gap-2 text-xs">
      <span class="atom-jump font-mono text-teal-700 cursor-pointer" data-atom="${esc(r.atom_id || r.traj_id || '')}">${esc(r.atom_id || r.traj_id || '?')}</span>
      <span class="text-slate-400 flex-1 truncate">${esc((r.atom && r.atom.intent) || r.reasons || '')}</span>
      <span class="px-2 py-0.5 rounded-md bg-teal-50 text-teal-700 text-[11px] font-semibold tabular-nums shrink-0">${r.score}</span>
    </div>`).join('')}
  </div>`;
}

// 血缘：贡献来源（用户占比条 + 模型 chips）与贡献原子列表
function renderLineage(lin) {
  const byUser = lin.by_user || [];
  const maxU = Math.max(...byUser.map(u => u.atoms), 1);
  const userRows = byUser.map(u => `
    <div class="flex items-center gap-2.5">
      ${avatar(u.user)}<span class="w-16 text-slate-600 truncate" title="${esc(u.user)}">${esc(u.user)}</span>
      ${bar(u.atoms / maxU * 100)}
      <span class="tabular-nums text-slate-700 font-medium w-6 text-right">${u.atoms}</span>
    </div>`).join('') || '<span class="text-slate-400 text-xs">还没有贡献原子</span>';
  const modelChips = (lin.by_model || []).map(m =>
    `<span class="px-2.5 py-1 rounded-lg bg-slate-100 text-xs text-slate-600">${esc(m.model)} <b class="text-slate-800">${m.atoms}</b></span>`).join(' ');
  const atomRows = (lin.atoms || []).map(a => {
    const clickable = !a.source_cleaned && a.traj_id;
    const title = a.source_cleaned
      ? '<span class="text-slate-400">源已清理 <span class="text-[11px]">（原子文件已过期回收，保留记录）</span></span>'
      : `<span class="text-slate-800">${esc(a.intent) || esc(a.atom_id)}</span>`;
    const st = a.state === 'adopted'
      ? '<span class="text-[10.5px] text-emerald-600">已采纳</span>'
      : '<span class="text-[10.5px] text-amber-600">候选中</span>';
    return `<div class="py-2.5 flex items-center justify-between gap-2 ${clickable ? 'cursor-pointer hover:bg-slate-50 rounded-lg px-2 -mx-2' : ''}"
        ${clickable ? `data-atom-jump="${esc(a.traj_id)}/${esc(a.atom_id)}"` : ''}>
      <div class="min-w-0"><div class="truncate">${title}</div>
        <div class="text-[11px] ${a.source_cleaned ? 'text-slate-300' : 'text-slate-400'}">${esc(a.user)} · ${esc(a.model)} ${st}</div></div>
      <span class="px-2 py-0.5 rounded-md ${a.source_cleaned ? 'bg-slate-50 text-slate-400' : 'bg-teal-50 text-teal-700'} text-[11px] font-semibold tabular-nums shrink-0">${a.weightscore != null ? esc(a.weightscore) : '—'}</span>
    </div>`;
  }).join('') || '<div class="text-slate-400 text-xs py-2">还没有贡献原子</div>';
  return { userRows, modelChips, atomRows };
}

function renderDiff(diff) {
  if (!diff) return '<span class="text-slate-400">无 diff</span>';
  return '<pre class="text-[11.5px] leading-relaxed overflow-x-auto">' + diff.split('\n').map(line => {
    const e = esc(line);
    if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="block bg-emerald-50 text-emerald-800">${e}</span>`;
    if (line.startsWith('-') && !line.startsWith('---')) return `<span class="block bg-rose-50 text-rose-800">${e}</span>`;
    if (line.startsWith('@@')) return `<span class="block text-violet-600">${e}</span>`;
    return e;
  }).join('\n') + '</pre>';
}

let _curSkill = null;
// 判定某 skill 属于自产(native)还是三方(skillhub)——从技能库列表载荷取
// source 字段(列表由另一路渲染，本函数只读)。jc 已缓存该端点，无额外请求。
// 拿不到 source / 请求失败 → 安全兜底按自产走，绝不因缺字段崩。
async function skillSource(name) {
  try {
    // 定向查这一条(?name=),别为判 source 拉全量 1 万条 skill。
    const d = await jc('api/v1/dashboard/skills?name=' + encodeURIComponent(name));
    const hit = (d.skills || []).find(s => s.name === name);
    return (hit && hit.source === 'skillhub') ? 'skillhub' : 'native';
  } catch (_e) {
    return 'native';
  }
}

async function openSkill(name) {
  _curSkill = name;
  const box = document.getElementById('skill-detail');
  box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5 text-slate-400">加载 ${esc(name)} …</div>`;
  // 三方 skill 无 git / staging，走 skillhub 专用端点(自产 detail 对其 404)。
  if (await skillSource(name) === 'skillhub') { await renderSkillhubDetail(name, box); return; }
  const [dR, gR, uR, lR, tR] = await Promise.allSettled([
    jc('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/detail'),
    jc('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/graph'),
    jc('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/ux/daily'),
    jc('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/lineage'),
    jc('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/tree'),
  ]);
  if (dR.status === 'rejected') {
    box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5 text-rose-600">加载失败：${esc(dR.reason)}</div>`;
    return;
  }
  const d = dR.value;
  const g = gR.status === 'fulfilled' ? gR.value : null;
  const daily = uR.status === 'fulfilled' ? uR.value.daily : [];
  const lin = lR.status === 'fulfilled' ? lR.value : { atoms: [], by_user: [], by_model: [], uses: 0, avg_ux: null };
  const tree = tR.status === 'fulfilled' ? tR.value : { files: [] };

  const heads = (g && g.heads) || {};
  const headChips = [
    heads.main ? `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-white ring-1 ring-slate-200 text-xs font-medium text-slate-600">main <code class="text-slate-400">${esc(heads.main.slice(0, 7))}</code></span>` : '',
    heads.staging ? `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-amber-50 ring-1 ring-amber-200 text-xs font-medium text-amber-700"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>staging 灰度中 <code class="opacity-60">${esc(heads.staging.slice(0, 7))}</code></span>` : '',
  ].join(' ');

  const vrows = (d.versions || []).map(v =>
    `<tr><td class="py-2"><code class="text-[11px]">${esc((v.sha || '').slice(0, 8))}</code></td>`
    + `<td class="text-right tabular-nums">${v.triggers}</td>`
    + `<td class="text-right tabular-nums">${ux(v.avg_ux)}</td>`
    + `<td class="text-right tabular-nums">${v.atoms}</td>`
    + `<td class="text-slate-500 pl-4">${fdate(v.first_ts).slice(0, 10)}</td></tr>`).join('')
    || '<tr><td colspan="5" class="py-2 text-slate-400">还没有版本触发数据</td></tr>';
  const byUserRows = (d.by_user || []).map(u =>
    `<tr><td class="py-2"><span class="flex items-center gap-2">${avatar(u.user, 'sm')}${esc(u.user)}</span></td>`
    + `<td class="text-right tabular-nums">${u.triggers}</td>`
    + `<td class="text-right tabular-nums">${ux(u.avg_ux)}</td></tr>`).join('')
    || '<tr><td colspan="3" class="py-2 text-slate-400">还没有触发记录</td></tr>';

  const L = renderLineage(lin);
  const fileItems = (tree.files || []).map(f =>
    `<a href="javascript:void(0)" class="skf block px-2 py-1 rounded hover:bg-slate-50 text-xs text-slate-600" data-skill="${esc(name)}" data-path="${esc(f.path)}">${esc(f.path)} <span class="text-slate-300">(${f.size})</span></a>`).join('')
    || '<span class="text-slate-400 text-xs px-2">空目录</span>';
  const gitItems = (d.versions_git || []).map(v =>
    `<a href="javascript:void(0)" class="skd block px-2 py-1 rounded hover:bg-slate-50 text-xs text-slate-600" data-skill="${esc(name)}" data-sha="${esc(v.sha)}"><code class="text-[11px] text-slate-400">${esc(v.short)}</code> ${esc(v.subject)}</a>`).join('')
    || '<span class="text-slate-400 text-xs px-2">非 git 仓</span>';

  box.innerHTML = `
  <div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5">
    <div class="text-xs text-slate-400 mb-1.5">技能库 <span class="mx-1">/</span> <span class="text-slate-600">${esc(name)}</span></div>
    <div class="flex items-start justify-between gap-3 flex-wrap">
      <div>
        <h2 class="text-lg font-bold tracking-tight">${esc(name)}</h2>
        <div class="text-slate-500 text-xs mt-1">总触发 <b class="text-slate-800 tabular-nums">${d.total_triggers}</b> 次
          · 贡献原子 <b class="text-slate-800 tabular-nums">${(lin.atoms || []).length}</b> 个
          ${lin.avg_ux != null ? `· 血缘平均 ux <b class="text-slate-800 tabular-nums">${lin.avg_ux}</b>` : ''}</div>
      </div>
      <div class="flex gap-2">${headChips}</div>
    </div>

    <div class="grid grid-cols-12 gap-4 mt-4">
      <div class="col-span-12 lg:col-span-5 rounded-2xl ring-1 ring-slate-200 p-5">
        <div class="flex items-baseline justify-between">
          <h3 class="font-semibold text-sm">进化路径</h3>
          <span class="text-[11px] text-slate-400">点击节点查看该版本 diff</span>
        </div>
        ${g ? renderGraph(g) : '<div class="text-slate-400 text-xs mt-3">非 git 仓，暂无进化路径</div>'}
      </div>
      <div class="col-span-12 lg:col-span-7 space-y-4">
        <div class="rounded-2xl ring-1 ring-slate-200 p-5">
          <h3 class="font-semibold text-sm">得分趋势 <span class="font-normal text-[11px] text-slate-400 ml-2">ux 日均 · 悬停节点看当日样本数</span></h3>
          ${renderDual(daily)}
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="rounded-2xl ring-1 ring-slate-200 p-5">
            <h3 class="font-semibold text-sm">贡献来源 <span class="font-normal text-[11px] text-slate-400 ml-1">${(lin.atoms || []).length} 个原子</span></h3>
            <div class="mt-3 space-y-2.5">${L.userRows}</div>
            ${L.modelChips ? `<div class="mt-4 pt-3 border-t border-slate-100 text-[11px] text-slate-400">来源模型</div><div class="mt-2 flex gap-2 flex-wrap">${L.modelChips}</div>` : ''}
          </div>
          <div class="rounded-2xl ring-1 ring-slate-200 p-5">
            <h3 class="font-semibold text-sm">贡献原子 <span class="font-normal text-[11px] text-slate-400 ml-1">点击跳原子详情</span></h3>
            <div class="mt-1 divide-y divide-slate-100 max-h-72 overflow-y-auto">${L.atomRows}</div>
          </div>
        </div>
      </div>
    </div>

    <div class="grid grid-cols-12 gap-4 mt-4">
      <div class="col-span-12 lg:col-span-7">
        <h3 class="font-semibold text-sm">版本统计 <span class="font-normal text-[11px] text-slate-400 ml-1">触发 / UX / 去重原子 / 首用</span></h3>
        <div class="overflow-x-auto"><table class="w-full mt-1 text-[12.5px]">
          <thead><tr class="text-[11px] text-slate-400 border-b border-slate-100"><th class="text-left font-medium py-2">版本</th><th class="text-right font-medium">触发</th><th class="text-right font-medium">UX</th><th class="text-right font-medium">原子</th><th class="text-left font-medium pl-4">首用</th></tr></thead>
          <tbody class="divide-y divide-slate-50">${vrows}</tbody></table></div>
      </div>
      <div class="col-span-12 lg:col-span-5">
        <h3 class="font-semibold text-sm">按用户</h3>
        <div class="overflow-x-auto"><table class="w-full mt-1 text-[12.5px]">
          <thead><tr class="text-[11px] text-slate-400 border-b border-slate-100"><th class="text-left font-medium py-2">用户</th><th class="text-right font-medium">触发</th><th class="text-right font-medium">UX</th></tr></thead>
          <tbody class="divide-y divide-slate-50">${byUserRows}</tbody></table></div>
      </div>
    </div>

    <div class="grid grid-cols-12 gap-4 mt-4">
      <div class="col-span-12 md:col-span-4">
        <h3 class="font-semibold text-sm">文件目录</h3>
        <div class="mt-1 max-h-44 overflow-y-auto rounded-xl ring-1 ring-slate-100 py-1">${fileItems}</div>
        <h3 class="font-semibold text-sm mt-3">版本（点击看 diff）</h3>
        <div class="mt-1 max-h-36 overflow-y-auto rounded-xl ring-1 ring-slate-100 py-1">${gitItems}</div>
      </div>
      <div class="col-span-12 md:col-span-8">
        <h3 class="font-semibold text-sm">预览 / diff</h3>
        <div id="skill-preview" class="mt-1 rounded-xl ring-1 ring-slate-100 p-3 max-h-80 overflow-auto"><span class="text-slate-400 text-xs">点左侧文件或版本、或进化路径节点查看</span></div>
      </div>
    </div>

    <div id="skill-trigger" class="mt-4"><div class="text-slate-400 text-xs">加载离线触发评测…</div></div>
  </div>`;
  box.scrollIntoView({ behavior: 'smooth' });
  loadTriggerPanel(name).catch(console.error);
}

// 三方(skillhub)技能详情：无 git / 无灰度 staging / 无进化路径，只有按
// content_sha 版本聚合的 ux 均分与关联打分原子。故不渲染自产才有的
// 进化路径 / staging 灰度 / 晋升 / 离线探针版块，仅展示三方可得的评分事实。
async function renderSkillhubDetail(name, box) {
  const [vR, aR] = await Promise.allSettled([
    jc('api/v1/dashboard/skillhub/' + encodeURIComponent(name) + '/ux?days=30'),
    jc('api/v1/dashboard/skillhub/' + encodeURIComponent(name) + '/ux/atoms?days=30'),
  ]);
  if (vR.status === 'rejected') {
    box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5 text-rose-600">加载失败：${esc(vR.reason)}</div>`;
    return;
  }
  const d = vR.value;
  const at = aR.status === 'fulfilled' ? aR.value : { scores: [], atom_lookup: 'unavailable' };
  const versions = d.versions || [];
  const curSha = (d.current_version && d.current_version.content_sha) || '';
  const totalSamples = versions.reduce((n, v) => n + (v.count || 0), 0);

  const vrows = versions.map(v => {
    const isCur = curSha && v.commit_sha === curSha;
    return `<tr><td class="py-2"><code class="text-[11px]">${esc((v.commit_sha || '').slice(0, 8))}</code>`
      + `${isCur ? ' <span class="px-1.5 py-0.5 rounded bg-sky-50 text-sky-700 ring-1 ring-sky-200 text-[10px] font-medium">当前</span>' : ''}</td>`
      + `<td class="text-right tabular-nums">${v.count}</td>`
      + `<td class="text-right tabular-nums">${ux(v.avg)}</td>`
      + `<td class="text-slate-500 pl-4">${fdate(v.first_scored_at).slice(0, 10)}</td>`
      + `<td class="text-slate-500 pl-4">${fdate(v.last_scored_at).slice(0, 10)}</td></tr>`;
  }).join('') || '<tr><td colspan="5" class="py-2 text-slate-400">还没有 ux 打分数据</td></tr>';

  const scores = at.scores || [];
  const atomUnavailable = (at.atom_lookup !== 'ok');
  const arows = scores.map(s =>
    `<div class="py-2.5">
      <div class="flex items-center justify-between gap-2">
        <span class="flex items-center gap-2 min-w-0">
          <code class="text-[11px] text-slate-400 shrink-0">${esc((s.commit_sha || '').slice(0, 7))}</code>
          <span class="text-[11px] text-slate-400 truncate">${esc(s.user_model) || '—'} · ${fdate(s.scored_at).slice(0, 10)}</span>
        </span>
        <span class="px-2 py-0.5 rounded-md bg-teal-50 text-teal-700 text-[11px] font-semibold tabular-nums shrink-0">${s.score != null ? esc(s.score) : '—'}</span>
      </div>
      ${s.reasons ? `<div class="text-[11px] text-slate-500 mt-1">${esc(s.reasons)}</div>` : ''}
    </div>`).join('') || '<div class="py-2 text-slate-400 text-xs">还没有关联打分原子</div>';

  box.innerHTML = `
  <div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5">
    <div class="text-xs text-slate-400 mb-1.5">技能库 <span class="mx-1">/</span> <span class="text-slate-600">${esc(name)}</span></div>
    <div class="flex items-start justify-between gap-3 flex-wrap">
      <div>
        <div class="flex items-center gap-2 flex-wrap">
          <h2 class="text-lg font-bold tracking-tight">${esc(name)}</h2>
          <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-sky-50 ring-1 ring-sky-200 text-xs font-medium text-sky-700">第三方 · skillhub</span>
        </div>
        <div class="text-slate-500 text-xs mt-1">按 content_sha 版本聚合 · 累计样本 <b class="text-slate-800 tabular-nums">${totalSamples}</b> 次
          ${curSha ? `· 当前版本 <code class="text-[11px] text-slate-400">${esc(curSha.slice(0, 8))}</code>` : ''}</div>
      </div>
      <div class="text-[11px] text-slate-400 max-w-[280px]">三方技能无 git / 无灰度 staging，故无进化路径、晋升与灰度裁决版块。</div>
    </div>

    <div class="grid grid-cols-12 gap-4 mt-4">
      <div class="col-span-12 lg:col-span-7">
        <h3 class="font-semibold text-sm">版本统计 <span class="font-normal text-[11px] text-slate-400 ml-1">content_sha 聚合 · 样本 / UX / 首末打分</span></h3>
        <div class="overflow-x-auto"><table class="w-full mt-1 text-[12.5px]">
          <thead><tr class="text-[11px] text-slate-400 border-b border-slate-100"><th class="text-left font-medium py-2">版本</th><th class="text-right font-medium">样本</th><th class="text-right font-medium">UX</th><th class="text-left font-medium pl-4">首次</th><th class="text-left font-medium pl-4">末次</th></tr></thead>
          <tbody class="divide-y divide-slate-50">${vrows}</tbody></table></div>
      </div>
      <div class="col-span-12 lg:col-span-5 rounded-2xl ring-1 ring-slate-200 p-5">
        <h3 class="font-semibold text-sm">关联打分原子 <span class="font-normal text-[11px] text-slate-400 ml-1">${scores.length} 条</span></h3>
        ${atomUnavailable ? '<div class="text-[11px] text-slate-400 mt-1">非团队服务器模式，原子内容不可反查（仅评分）</div>' : ''}
        <div class="mt-1 divide-y divide-slate-100 max-h-80 overflow-y-auto">${arows}</div>
      </div>
    </div>
  </div>`;
  box.scrollIntoView({ behavior: 'smooth' });
}

// 离线探针触发率面板（描述质量信号；区别于"总触发"的线上真实使用率）
function pctf(x) { return Math.round((Number(x) || 0) * 100) + '%'; }
async function loadTriggerPanel(name) {
  const el = document.getElementById('skill-trigger');
  if (!el) return;
  let hist = { history: [] }, cases = { cases: [], exp: null };
  try { hist = await j('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/trigger'); } catch (e) { /* 空 */ }
  try { cases = await j('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/trigger/cases'); } catch (e) { /* 空 */ }
  const hrows = (hist.history || []).map(h =>
    `<tr><td class="py-2"><code class="text-[11px]">${esc((h.version_sha || '—').slice(0, 8))}</code></td>`
    + `<td class="text-right tabular-nums">${pctf(h.test_score)}</td>`
    + `<td class="text-right tabular-nums">${pctf(h.train_score)}</td>`
    + `<td class="text-right tabular-nums">${h.n_cases}</td>`
    + `<td class="text-right tabular-nums">${h.catalog_size}</td>`
    + `<td class="text-slate-500 pl-4">${fdate(h.ts)}</td></tr>`).join('')
    || '<tr><td colspan="6" class="py-2 text-slate-400">还没有离线触发评测</td></tr>';
  const crows = (cases.cases || []).map(c =>
    `<tr><td class="py-2 max-w-[280px] truncate" title="${esc(c.query)}">${esc(c.query)}</td>`
    + `<td class="text-center">${c.should_trigger ? '是' : '否'}</td>`
    + `<td class="text-center">${c.did_trigger ? '触发' : '未触发'}</td>`
    + `<td class="text-center">${c.passed
      ? '<span class="px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 text-[10.5px] font-medium">通过</span>'
      : '<span class="px-1.5 py-0.5 rounded bg-rose-50 text-rose-600 text-[10.5px] font-medium">未过</span>'}</td>`
    + `<td class="text-slate-400 text-[11px] max-w-[200px] truncate" title="${esc((c.catalog || []).join(', '))}">${esc((c.catalog || []).join(', '))}</td>`
    + `<td class="text-right"><button class="trig-rerun px-2.5 py-1 rounded-lg ring-1 ring-slate-200 text-[11px] text-slate-600 hover:bg-slate-50" data-skill="${esc(name)}" data-query="${esc(c.query)}">重跑</button></td></tr>`).join('')
    || '<tr><td colspan="6" class="py-2 text-slate-400">无 case（该 skill 还没跑过触发优化）</td></tr>';
  el.innerHTML = `<h3 class="font-semibold text-sm">离线探针触发率 <span class="font-normal text-[11px] text-slate-400 ml-1">描述质量信号——真跑代理在语义相关技能清单里抢触发；区别于上方"总触发"的线上真实使用</span></h3>
    <div class="overflow-x-auto"><table class="w-full mt-1 text-[12.5px]">
      <thead><tr class="text-[11px] text-slate-400 border-b border-slate-100"><th class="text-left font-medium py-2">版本</th><th class="text-right font-medium">test 触发率</th><th class="text-right font-medium">train</th><th class="text-right font-medium">cases</th><th class="text-right font-medium">诱饵数</th><th class="text-left font-medium pl-4">时间</th></tr></thead>
      <tbody class="divide-y divide-slate-50">${hrows}</tbody></table></div>
    <h3 class="font-semibold text-sm mt-3">逐 case <span class="font-normal text-[11px] text-slate-400 ml-1">实验 ${esc(cases.exp || '—')} · 点"重跑"用当前描述真跑一轮探针</span></h3>
    <div class="overflow-x-auto"><table class="w-full mt-1 text-[12.5px]">
      <thead><tr class="text-[11px] text-slate-400 border-b border-slate-100"><th class="text-left font-medium py-2">query</th><th class="text-center font-medium">应触发</th><th class="text-center font-medium">实测</th><th class="text-center font-medium">判定</th><th class="text-left font-medium">诱饵清单</th><th></th></tr></thead>
      <tbody class="divide-y divide-slate-50">${crows}</tbody></table></div>`;
}

// ── 轨迹 & 原子 ──────────────────────────────────────────────────
let _curTraj = null;
async function openTraj(trajId, atomId) {
  _curTraj = trajId;
  const box = document.getElementById('traj-detail');
  box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5 text-slate-400">加载 ${esc(trajId)} …</div>`;
  let meta, atoms;
  try {
    [meta, atoms] = await Promise.all([
      jc('api/v1/dashboard/traj/' + encodeURIComponent(trajId)),
      jc('api/v1/dashboard/traj/' + encodeURIComponent(trajId) + '/atoms'),
    ]);
  } catch (e) {
    box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5 text-rose-600">轨迹加载失败：${esc(e.message)}</div>`;
    return;
  }
  const list = atoms.atoms || [];
  const steps = list.map((a, i) => {
    const orphan = a.chain === 'orphan';
    const num = String(i + 1).padStart(2, '0');
    return `<div class="flex flex-col items-center w-36 shrink-0 text-center cursor-pointer atom-step" data-atom="${esc(a.atom_id)}" ${orphan ? 'title="链表断裂，按位置排序"' : ''}>
      <div class="w-9 h-9 rounded-full ${orphan ? 'bg-amber-400 ring-4 ring-amber-100 text-white' : 'bg-white ring-2 ring-slate-300 text-slate-500'} flex items-center justify-center text-[11px] font-semibold z-10 atom-dot">${num}</div>
      <div class="mt-2.5 font-medium text-slate-700 text-xs line-clamp-2" title="${esc(a.intent)}">${esc(a.intent) || esc(a.atom_id)}</div>
      <div class="text-[11px] text-slate-400 mt-0.5">${a.ux_score != null ? 'ux ' + esc(a.ux_score) : ''}</div>
    </div>`;
  }).join('');
  box.innerHTML = `
  <div class="bg-white rounded-2xl ring-1 ring-slate-200 p-6">
    <div class="text-xs text-slate-400 mb-1.5">轨迹 &amp; 原子 <span class="mx-1">/</span> <span class="text-slate-600 font-mono">${esc(trajId)}</span></div>
    <div class="flex items-start justify-between gap-3 flex-wrap">
      <h2 class="text-lg font-bold tracking-tight font-mono break-all">${esc(trajId)}</h2>
      <div class="flex gap-2 text-xs flex-wrap">
        <span class="px-2.5 py-1 rounded-lg bg-white ring-1 ring-slate-200 text-slate-600">${esc(meta.harness) || '?'} · ${esc(meta.model) || '?'}</span>
        <span class="px-2.5 py-1 rounded-lg bg-white ring-1 ring-slate-200 text-slate-600">${esc(meta.user)}</span>
        <span class="px-2.5 py-1 rounded-lg ${meta.status === 'done' ? 'bg-emerald-50 ring-1 ring-emerald-200 text-emerald-700' : meta.status === 'error' ? 'bg-rose-50 ring-1 ring-rose-200 text-rose-700' : 'bg-slate-100 ring-1 ring-slate-200 text-slate-600'} font-medium">${esc(meta.status)}</span>
        <span class="px-2.5 py-1 rounded-lg bg-white ring-1 ring-slate-200 text-slate-600">原子 <b class="tabular-nums">${meta.atoms}</b></span>
        <span class="px-2.5 py-1 rounded-lg bg-white ring-1 ring-slate-200 text-slate-600">${fdate(meta.discovered_at)}</span>
      </div>
    </div>
    <h3 class="font-semibold text-sm mt-6">原子时间线 <span class="font-normal text-[11px] text-slate-400 ml-2">按链表序 pre/post_atom_id · 点击节点查看详情</span></h3>
    ${list.length ? `<div class="relative mt-6 overflow-x-auto pb-2">
      <div class="relative flex gap-2 min-w-max px-2">
        <div class="absolute left-6 right-6 top-[17px] h-0.5 bg-slate-200"></div>
        ${steps}
      </div></div>` : '<div class="text-slate-400 text-xs mt-3">该轨迹还没有拆出原子</div>'}
    ${relationGraph(trajId, list)}
    <div id="atom-detail" class="mt-4"></div>
  </div>`;
  if (atomId) openAtom(trajId, atomId).catch(console.error);
  else if (list.length) openAtom(trajId, list[0].atom_id).catch(console.error);
}

// D5：traj—atom—skill 二部关系图（分层布局；贡献边加粗标 weightscore）
function relationGraph(trajId, atoms) {
  if (!atoms.length) return '';
  const skills = [];
  atoms.forEach(a => (a.destinations || []).forEach(d => {
    if (!skills.includes(d.skill)) skills.push(d.skill);
  }));
  const rowH = 52, H = Math.max(atoms.length, skills.length || 1) * rowH + 24;
  const ay = i => 24 + i * rowH + (Math.max(0, skills.length - atoms.length) * rowH) / 2;
  const sy = i => 24 + i * rowH + (Math.max(0, atoms.length - skills.length) * rowH) / 2;
  const midY = 12 + (H - 24) / 2;
  const edges = atoms.map((a, i) =>
    `<path d="M96 ${midY} C 140 ${midY} 140 ${ay(i)} 168 ${ay(i)}" fill="none" stroke="#e2e8f0" stroke-width="1.5"/>`).join('')
    + atoms.flatMap((a, i) => (a.destinations || []).map(d => {
      const si = skills.indexOf(d.skill);
      return `<path d="M186 ${ay(i)} C 250 ${ay(i)} 250 ${sy(si)} 300 ${sy(si)}" fill="none" stroke="#0d9488" stroke-width="2.5"/>
        <text x="243" y="${(ay(i) + sy(si)) / 2 - 6}" font-size="9.5" fill="#94a3b8" text-anchor="middle">${d.weightscore != null ? 'ws ' + esc(d.weightscore) : ''}</text>`;
    })).join('');
  const atomNodes = atoms.map((a, i) =>
    `<g class="atom-jump cursor-pointer" data-atom="${esc(a.atom_id)}">
      <circle cx="177" cy="${ay(i)}" r="${(a.destinations || []).length ? 11 : 9}" fill="${(a.destinations || []).length ? '#0d9488' : '#e2e8f0'}"/>
      <text x="177" y="${ay(i) + 3.5}" font-size="10" font-family="ui-monospace,monospace" text-anchor="middle" fill="${(a.destinations || []).length ? '#fff' : '#475569'}">${esc(a.atom_id.slice(-2))}</text>
    </g>`).join('');
  const skillNodes = skills.map((sk, i) =>
    `<g class="skill-jump cursor-pointer" data-skill="${esc(sk)}">
      <rect x="300" y="${sy(i) - 15}" width="150" height="30" rx="9" fill="#f0fdfa" stroke="#99f6e4"/>
      <text x="375" y="${sy(i) + 4}" font-size="10.5" text-anchor="middle" fill="#0f766e" font-weight="600">${esc(sk.length > 22 ? sk.slice(0, 21) + '…' : sk)}</text>
    </g>`).join('');
  return `<h3 class="font-semibold text-sm mt-6">关系图 <span class="font-normal text-[11px] text-slate-400 ml-2">traj — atom — skill · 贡献边标 weightscore · 点节点跳转</span></h3>
    ${skills.length ? '' : '<div class="text-[11px] text-slate-400 mt-1">该轨迹的原子尚未进入任何 skill（无贡献边）</div>'}
    <svg viewBox="0 0 470 ${H}" class="mt-2" style="max-width:470px">
      <rect x="10" y="${midY - 16}" width="86" height="32" rx="9" fill="#134e4a"/>
      <text x="53" y="${midY + 4}" font-size="10" fill="#fff" text-anchor="middle" font-family="ui-monospace,monospace">${esc(trajId.length > 12 ? trajId.slice(0, 11) + '…' : trajId)}</text>
      ${edges}${atomNodes}${skillNodes}
    </svg>`;
}

async function openAtom(trajId, atomId) {
  const el = document.getElementById('atom-detail');
  if (!el) return;
  el.innerHTML = '<div class="text-slate-400 text-xs">加载原子…</div>';
  // 高亮选中节点
  document.querySelectorAll('.atom-step .atom-dot').forEach(d => {
    d.classList.remove('bg-teal-600', 'ring-4', 'ring-teal-100', 'text-white');
  });
  const sel = document.querySelector(`.atom-step[data-atom="${CSS.escape(atomId)}"] .atom-dot`);
  if (sel && !sel.classList.contains('bg-amber-400')) {
    sel.classList.remove('bg-white', 'ring-2', 'ring-slate-300', 'text-slate-500');
    sel.classList.add('bg-teal-600', 'ring-4', 'ring-teal-100', 'text-white');
  }
  let a;
  try {
    a = await jc('api/v1/dashboard/traj/' + encodeURIComponent(trajId) + '/atom/' + encodeURIComponent(atomId));
  } catch (e) {
    el.innerHTML = `<div class="text-rose-600 text-xs">原子加载失败:${esc(e.message)}</div>`;
    return;
  }
  const chips = arr => (arr || []).map(t =>
    `<span class="px-2 py-0.5 rounded-md bg-slate-100 text-slate-600 text-[11px]">${esc(t)}</span>`).join(' ') || '<span class="text-slate-300">—</span>';
  const skillChips = (a.used_skills || []).map(s =>
    `<span class="skill-jump px-2 py-0.5 rounded-md bg-teal-50 ring-1 ring-teal-200 text-teal-700 text-[11px] font-medium cursor-pointer" data-skill="${esc(s)}">${esc(s)}</span>`).join(' ') || '<span class="text-slate-300">—</span>';
  const dest = (a.destinations || []).map(d =>
    `<span class="skill-jump text-teal-700 font-medium underline decoration-teal-200 underline-offset-2 cursor-pointer" data-skill="${esc(d.skill)}">${esc(d.skill)}</span>
     <span class="text-slate-500">（weightscore ${d.weightscore != null ? esc(d.weightscore) : '—'} · ${d.state === 'adopted' ? '已采纳' : '候选中'}）</span>`).join('<br>')
    || '<span class="text-slate-400">未进入任何 skill</span>';
  const rawBlock = a.raw_status === 'source_cleaned'
    ? '<div class="rounded-xl bg-slate-900 p-4 font-mono text-[11.5px] text-rose-400">源已清理（轨迹原文已过期回收，保留原子记录）</div>'
    : `<div class="rounded-xl bg-slate-900 p-4 font-mono text-[11.5px] leading-relaxed text-slate-300 whitespace-pre-wrap max-h-80 overflow-auto">${esc(a.raw || '')}${a.raw_total_chars > 8000 ? `\n<span class="text-slate-500">（截取 8000/${a.raw_total_chars} 字符）</span>` : ''}</div>`;
  el.innerHTML = `
  <div class="rounded-2xl ring-1 ring-slate-200 p-5">
    <div class="flex items-center justify-between">
      <h3 class="font-semibold text-sm font-mono break-all">${esc(a.atom_id)}</h3>
      ${a.ux_score != null ? `<span class="px-2 py-0.5 rounded-md bg-teal-50 text-teal-700 text-[11px] font-semibold tabular-nums">ux ${esc(a.ux_score)}</span>` : ''}
    </div>
    <dl class="mt-4 space-y-3">
      <div class="flex gap-4"><dt class="w-20 text-slate-400 shrink-0">intent</dt><dd class="text-slate-800">${esc(a.intent) || '—'}</dd></div>
      <div class="flex gap-4"><dt class="w-20 text-slate-400 shrink-0">summary</dt><dd class="text-slate-800">${esc(a.summary) || '—'}</dd></div>
      <div class="flex gap-4"><dt class="w-20 text-slate-400 shrink-0">tags</dt><dd class="flex gap-1.5 flex-wrap">${chips(a.tags)}</dd></div>
      <div class="flex gap-4"><dt class="w-20 text-slate-400 shrink-0">used_skills</dt><dd class="flex gap-1.5 flex-wrap">${skillChips}</dd></div>
      <div class="flex gap-4"><dt class="w-20 text-slate-400 shrink-0">去向</dt><dd class="text-slate-800">${dest}</dd></div>
      <div class="flex gap-4"><dt class="w-20 text-slate-400 shrink-0">offset</dt><dd class="tabular-nums text-slate-600">行 ${a.offset_start} – ${a.offset_end}</dd></div>
    </dl>
    <div class="text-[11px] text-slate-400 mt-5 mb-1.5">原文切片（按 offset 行号定位 · 只读）</div>
    ${rawBlock}
  </div>`;
}

async function loadDirs() {
  const d = await jc('api/v1/dashboard/dirs');
  rows('dirs-body', (d.dirs || []).map(x =>
    `<tr><td class="py-2"><span class="px-2 py-0.5 rounded-md bg-teal-50 text-teal-700 text-[11px] font-medium">${esc(x.ecosystem || 'manual')}</span></td>`
    + `<td class="text-right tabular-nums">${x.traj_count}</td>`
    + `<td class="text-right tabular-nums">${x.indexed_count}</td>`
    + `<td class="pl-6 text-slate-500 font-mono text-[11px]">${x.path ? esc(x.path) : '独立只读实例隐藏路径'}</td></tr>`).join(''),
    '还没有注册目录');
}

// ── 用户 & 画像 ──────────────────────────────────────────────────
async function loadUsersStatus() {
  const d = await jc('api/v1/dashboard/users/status');
  const users = d.users || [];
  put('users.online', `在线 ${d.online} / ${users.length}`);
  const rEl = document.getElementById('users-reason');
  if (d.reason) rEl.innerHTML = `<div class="mt-2 rounded-xl bg-slate-50 ring-1 ring-slate-100 px-3.5 py-2 text-[11px] text-slate-400">${esc(d.reason)}</div>`;
  rows('ustatus-body', users.map(u => {
    const hs = (u.harness || []).slice(0, 2).map(h =>
      `<span class="px-1.5 py-0.5 rounded bg-teal-50 text-teal-700 text-[10.5px]">${esc(h.harness)} ${h.pct}%</span>`).join(' ') || '<span class="text-slate-300">—</span>';
    const topM = (u.models || [])[0];
    const model = u.trajs <= 1
      ? '<span class="text-slate-400">样本不足</span>'
      : topM ? `${esc(topM.model)} <span class="text-slate-400">${topM.pct}%</span>` : '<span class="text-slate-300">—</span>';
    return `<tr data-uid="${esc(u.user)}" class="cursor-pointer hover:bg-slate-50">
      <td class="py-2.5"><span class="flex items-center gap-2">${avatar(u.user)}<b>${esc(u.user)}</b></span></td>
      <td>${u.online
        ? '<span class="inline-flex items-center gap-1.5 text-emerald-600 font-medium text-xs"><span class="w-2 h-2 rounded-full bg-emerald-500"></span>在线</span>'
        : '<span class="inline-flex items-center gap-1.5 text-slate-400 text-xs"><span class="w-2 h-2 rounded-full bg-slate-300"></span>离线</span>'}</td>
      <td class="text-slate-500 text-xs">${fdate(u.last_seen)}</td>
      <td class="text-xs">${u.client_version
        ? `${esc(u.client_version)}${u.version_stale ? ' <span class="px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 text-[10px]">落后</span>' : ''}`
        : '<span class="text-slate-300">未上报</span>'}</td>
      <td class="text-right tabular-nums text-slate-600">${u.trajs} · ${u.atoms}</td>
      <td class="pl-6">${hs}</td>
      <td class="text-slate-600 text-xs">${model}</td></tr>`;
  }).join(''), '暂无团队用户（非 team server 或尚无 client 连接）');
}

async function loadTags() {
  const d = await jc('api/v1/dashboard/tags');
  const el = document.getElementById('tagcloud');
  const tags = d.tags || [];
  if (!el) return;
  if (!tags.length) { el.innerHTML = '<span class="text-slate-400">暂无标签（轨迹还没拆出带 tags 的原子）</span>'; return; }
  const max = Math.max(...tags.map(t => t.count)), min = Math.min(...tags.map(t => t.count));
  el.innerHTML = tags.map(t => {
    const sz = (12 + (max > min ? (t.count - min) / (max - min) * 16 : 4)).toFixed(0);
    const users = (t.users || []).map(esc).join(' ');
    return `<span class="tagchip inline-block px-2 py-0.5 rounded-lg bg-teal-50 text-teal-700 mr-2 mb-1" data-users="${users}" title="${esc(t.count)} 次" style="font-size:${sz}px">${esc(t.tag)}</span>`;
  }).join(' ');
}

// 用户 ⇄ 标签联动：悬浮（或点击 pin）用户行 → 高亮其贡献的标签、淡化其余
let _pinnedUid = null;
function highlightUser(uid) {
  document.querySelectorAll('#tagcloud .tagchip').forEach(ch => {
    const us = (ch.dataset.users || '').split(' ').filter(Boolean);
    const on = uid && us.includes(uid);
    ch.classList.toggle('hot', !!on);
    ch.classList.toggle('dim', !!uid && !on);
  });
  document.querySelectorAll('#ustatus-body tr[data-uid]').forEach(tr =>
    tr.classList.toggle('bg-teal-50/40', !!uid && tr.dataset.uid === uid));
}
document.addEventListener('mouseover', e => {
  const tr = e.target.closest('#ustatus-body tr[data-uid]');
  if (tr && !_pinnedUid) highlightUser(tr.dataset.uid);
});
document.addEventListener('mouseout', e => {
  const tr = e.target.closest('#ustatus-body tr[data-uid]');
  if (tr && !_pinnedUid) highlightUser(null);
});

// ── 灰度 Canary ──────────────────────────────────────────────────
async function loadCanary() {
  const c = await jc('api/v1/dashboard/canary');
  rows('canary-body', (c.sides || []).map(s =>
    `<tr><td class="py-2">${s.side === 'staging'
      ? '<span class="px-2 py-0.5 rounded-md bg-amber-50 text-amber-700 ring-1 ring-amber-200 text-[11px] font-medium">staging</span>'
      : `<span class="px-2 py-0.5 rounded-md bg-slate-100 text-slate-600 text-[11px] font-medium">${esc(s.side)}</span>`}</td>`
    + `<td class="text-right tabular-nums">${s.uses}</td>`
    + `<td class="text-right tabular-nums">${ux(s.avg_ux)}</td></tr>`).join(''),
    '还没有灰度使用记录');
}

// ── SPA-lite 路由（hash）─────────────────────────────────────────
const NAMES = { overview: '总览', skills: '技能库', traj: '轨迹 & 原子', users: '用户 & 画像', canary: '灰度 Canary', my: '我的', admin: '管理', kernels: '算法内核', settings: '设置' };
let IDENT = null;   // {user, role} | null；必须在首次 route()/showPage 之前声明
let _kernelLogES = null;
let _kernelLogPaused = false;
let _kernelLogLines = [];
const KERNEL_LOG_MAX = 1000;

function kernelLogPageOn() {
  const page = document.getElementById('pg-kernels');
  return !!(page && page.classList.contains('on'));
}

function appendKernelLog(line) {
  const view = document.getElementById('kernel-log-view');
  if (!view) return;
  const stageMatch = String(line).match(/\bstage=([^\s]+)/);
  if (stageMatch) {
    const stageEl = document.getElementById('kernel-log-stage');
    if (stageEl) stageEl.textContent = 'stage=' + stageMatch[1];
  }
  const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 32;
  const placeholder = view.textContent === '等待连接…' || view.textContent === '正在连接…';
  _kernelLogLines.push(line);
  if (_kernelLogLines.length > KERNEL_LOG_MAX) {
    _kernelLogLines = _kernelLogLines.slice(-KERNEL_LOG_MAX);
    view.textContent = _kernelLogLines.join('\n');
  } else if (placeholder || _kernelLogLines.length === 1) {
    view.textContent = line;
  } else {
    view.append(document.createTextNode('\n' + line));
  }
  if (!_kernelLogPaused && atBottom) view.scrollTop = view.scrollHeight;
}

function startKernelLogStream() {
  if (!IDENT || IDENT.role !== 'admin') return;
  if (_kernelLogES) return;
  _kernelLogLines = [];
  _kernelLogPaused = false;
  const pauseBtn = document.getElementById('kernel-log-pause');
  if (pauseBtn) pauseBtn.textContent = '暂停滚动';
  const stageEl = document.getElementById('kernel-log-stage');
  if (stageEl) stageEl.textContent = 'stage=—';
  const status = document.getElementById('kernel-log-status');
  const view = document.getElementById('kernel-log-view');
  if (view) view.textContent = '正在连接…';
  if (status) status.textContent = '连接中';
  const source = new EventSource('api/v1/dashboard/admin/kernels/logs');
  _kernelLogES = source;
  source.onopen = () => {
    if (status) status.textContent = '已连接';
  };
  source.onmessage = ev => {
    let payload;
    try { payload = JSON.parse(ev.data); } catch { payload = { t: 'log', line: ev.data }; }
    if (payload.t === 'meta') return;
    if (payload.line != null) appendKernelLog(payload.line);
  };
  source.onerror = () => {
    if (status) status.textContent = '已断开，重连中';
  };
}

function stopKernelLogStream() {
  const source = _kernelLogES;
  _kernelLogES = null;
  if (source) {
    source.onerror = null;
    source.onmessage = null;
    source.onopen = null;
    source.close();
  }
  const status = document.getElementById('kernel-log-status');
  if (status) status.textContent = '未连接';
}

function syncKernelLogStream() {
  if (kernelLogPageOn() && IDENT && IDENT.role === 'admin') startKernelLogStream();
  else stopKernelLogStream();
}

function showPage(pg) {
  if (!document.getElementById('pg-' + pg)) pg = 'overview';
  document.querySelectorAll('.sec-page').forEach(s => s.classList.remove('on'));
  document.getElementById('pg-' + pg).classList.add('on');
  document.querySelectorAll('#nav .nav-link').forEach(n => {
    const on = n.dataset.pg === pg;
    n.classList.toggle('bg-teal-50', on);
    n.classList.toggle('text-teal-800', on);
    n.classList.toggle('font-semibold', on);
    n.classList.toggle('text-slate-500', !on);
  });
  document.getElementById('pgname').textContent = NAMES[pg] || '总览';
  window.scrollTo(0, 0);
  syncKernelLogStream();
}
function route() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ''));
  const parts = h.split('/').filter(Boolean);
  if (parts[0] === 'traj' && parts[1]) {
    showPage('traj');
    openTraj(parts[1], parts[2]).catch(console.error);
    return;
  }
  if (parts[0] === 'skill' && parts[1]) {
    showPage('skills');
    openSkill(parts[1]).catch(console.error);
    return;
  }
  showPage(parts[0] || 'overview');
}
window.addEventListener('hashchange', route);

// ── 全局点击委托 ────────────────────────────────────────────────
document.addEventListener('click', async e => {
  const tp = e.target.closest('.trend-pt');
  if (tp && _curSkill) { drillTrendDay(_curSkill, tp.dataset.day, tp.dataset.side).catch(console.error); return; }
  const ajump = e.target.closest('.atom-jump');
  if (ajump && ajump.dataset.atom && ajump.dataset.atom.startsWith('atom_')) {
    const abody = ajump.dataset.atom.slice(5); const aidx = abody.lastIndexOf('_');
    if (aidx > 0) { location.hash = '#traj/' + abody.slice(0, aidx) + '/' + ajump.dataset.atom; }
    return;
  }
  const row = e.target.closest('[data-skill-row]');
  if (row) { location.hash = 'skill/' + encodeURIComponent(row.dataset.skillRow); return; }
  const sj = e.target.closest('.skill-jump');
  if (sj) { location.hash = 'skill/' + encodeURIComponent(sj.dataset.skill); return; }
  const aj = e.target.closest('[data-atom-jump]');
  if (aj) { location.hash = 'traj/' + aj.dataset.atomJump; return; }
  const step = e.target.closest('.atom-step');
  if (step && _curTraj) { openAtom(_curTraj, step.dataset.atom).catch(console.error); return; }
  const gn = e.target.closest('[data-gnode]');
  if (gn && _curSkill) {
    const pv = document.getElementById('skill-preview');
    if (pv) {
      pv.innerHTML = '<span class="text-slate-400 text-xs">加载 diff…</span>';
      try {
        const r = await j('api/v1/dashboard/skill/' + encodeURIComponent(_curSkill) + '/diff?sha=' + encodeURIComponent(gn.dataset.gnode));
        pv.innerHTML = renderDiff(r.diff);
      } catch (err) { pv.innerHTML = `<span class="text-rose-600 text-xs">${esc(err.message)}</span>`; }
      pv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    return;
  }
  const fl = e.target.closest('.skf');
  if (fl) {
    const r = await j('api/v1/dashboard/skill/' + encodeURIComponent(fl.dataset.skill) + '/file?path=' + encodeURIComponent(fl.dataset.path));
    const pv = document.getElementById('skill-preview');
    if (pv) pv.innerHTML = r.content != null
      ? `<pre class="text-[11.5px] whitespace-pre-wrap">${esc(r.content)}</pre>`
      : `<span class="text-rose-600 text-xs">${esc(r.error || 'error')}</span>`;
    return;
  }
  const dl = e.target.closest('.skd');
  if (dl) {
    const r = await j('api/v1/dashboard/skill/' + encodeURIComponent(dl.dataset.skill) + '/diff?sha=' + encodeURIComponent(dl.dataset.sha));
    const pv = document.getElementById('skill-preview');
    if (pv) pv.innerHTML = renderDiff(r.diff);
    return;
  }
  // 逐 case"重跑"：用当前描述真跑一轮探针，结果回填按钮
  const rb = e.target.closest('.trig-rerun');
  if (rb) {
    rb.disabled = true; rb.textContent = '跑…';
    try {
      const resp = await fetch('api/v1/dashboard/skill/' + encodeURIComponent(rb.dataset.skill) + '/trigger/rerun',
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: rb.dataset.query }) });
      const data = await resp.json();
      rb.classList.remove('ring-slate-200', 'text-slate-600');
      if (data.error) { rb.textContent = '错误'; rb.classList.add('ring-rose-200', 'text-rose-600'); }
      else if (data.did_trigger) { rb.textContent = '已触发'; rb.classList.add('ring-emerald-200', 'text-emerald-700'); }
      else { rb.textContent = '未触发'; rb.classList.add('ring-slate-200', 'text-slate-400'); }
      rb.title = '诱饵清单: ' + ((data.catalog || []).join(', ') || '空');
    } catch (err) { rb.textContent = '错误'; }
    rb.disabled = false;
    return;
  }
  const pinTr = e.target.closest('#ustatus-body tr[data-uid]');
  if (pinTr) {
    _pinnedUid = (_pinnedUid === pinTr.dataset.uid) ? null : pinTr.dataset.uid;
    highlightUser(_pinnedUid);
    // P3:点行同时打开画像详情(散点)
    openUserProfile(pinTr.dataset.uid).catch(console.error);
  }
});

// 轨迹输入框
document.getElementById('traj-open').addEventListener('click', () => {
  const v = document.getElementById('traj-input').value.trim().replace(/\.md$/, '');
  if (v) location.hash = 'traj/' + encodeURIComponent(v);
});
document.getElementById('traj-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('traj-open').click();
});

// ── 启动：各端点独立加载，单个失败不拖垮整页 ───────────────────
route();
for (const f of [loadOverview, loadRates, loadPipeline, loadDomain, loadCost,
  loadSkills, loadDirs, loadUsersStatus, loadTags, loadCanary]) {
  f().catch(e => console.error(e));
}

// ═════════════ P2:登录/角色 + 我的/管理/设置 ═════════════

async function jpost(u, body, method) {
  const r = await fetch(u, { method: method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || (u + ' ' + r.status));
  return data;
}

function applyIdent() {
  const logged = !!IDENT, admin = logged && IDENT.role === 'admin';
  document.querySelectorAll('.auth-user').forEach(e => e.classList.toggle('hidden', !logged));
  document.querySelectorAll('.auth-admin').forEach(e => e.classList.toggle('hidden', !admin));
  document.getElementById('who-anon').classList.toggle('hidden', logged);
  document.getElementById('who-user').classList.toggle('hidden', !logged);
  if (logged) {
    document.getElementById('who-name').textContent = IDENT.user;
    document.getElementById('who-role').textContent = IDENT.role;
  }
  document.getElementById('my-guard').classList.toggle('hidden', logged);
  document.getElementById('my-body').classList.toggle('hidden', !logged);
  document.getElementById('admin-guard').classList.toggle('hidden', admin);
  document.getElementById('admin-body').classList.toggle('hidden', !admin);
  document.getElementById('kernels-guard').classList.toggle('hidden', admin);
  document.getElementById('kernels-body').classList.toggle('hidden', !admin);
  document.getElementById('settings-guard').classList.toggle('hidden', admin);
  document.getElementById('settings-body').classList.toggle('hidden', !admin);
  syncKernelLogStream();
}

async function initIdent() {
  try { IDENT = await j('/api/v1/dashboard/me'); } catch { IDENT = null; }
  applyIdent();
  if (IDENT) { loadMy().catch(console.error); initEvents(); }
  if (IDENT && IDENT.role === 'admin') { loadAdmin().catch(console.error); loadKernels().catch(console.error); loadSettings().catch(console.error); }
}

// 登录弹窗
const _lm = document.getElementById('login-modal');
document.getElementById('btn-login').addEventListener('click', () => { _lm.classList.remove('hidden'); document.getElementById('login-user').focus(); });
document.getElementById('login-cancel').addEventListener('click', () => _lm.classList.add('hidden'));
document.getElementById('login-submit').addEventListener('click', async () => {
  const user = document.getElementById('login-user').value.trim();
  const sec = document.getElementById('login-secret').value;
  const err = document.getElementById('login-err');
  err.textContent = '';
  try {
    IDENT = await jpost('/api/v1/dashboard/login', { user_name: user, secret: sec });
    _lm.classList.add('hidden');
    applyIdent();
    loadMy().catch(console.error);
    initEvents();
    if (IDENT.role === 'admin') { loadAdmin().catch(console.error); loadKernels().catch(console.error); loadSettings().catch(console.error); }
  } catch (e) { err.textContent = e.message; }
});
document.getElementById('login-secret').addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('login-submit').click(); });
document.getElementById('btn-logout').addEventListener('click', async () => {
  await jpost('/api/v1/dashboard/logout').catch(() => {});
  IDENT = null; applyIdent(); location.hash = '#overview';
});

// ── 我的 ────────────────────────────────────────────────────────
const BUCKET_CHIP = {
  pinned: 'bg-violet-100 text-violet-700',
  ranked: 'bg-teal-50 text-teal-700 ring-1 ring-teal-100',
  recommended: 'bg-sky-100 text-sky-700',
};
function bucketLabel(s) {
  if (s.bucket !== 'pinned') return s.bucket;
  return s.pin_scope === 'global' ? 'pinned·全局' : (s.user_removable ? 'pinned·自己' : 'pinned·admin');
}
async function loadMy() {
  if (!IDENT) return;
  const [m, ct, rt] = await Promise.all([
    j('/api/v1/dashboard/my/manifest'),
    j('/api/v1/dashboard/my/contributions'),
    j('/api/v1/dashboard/my/reco-trigger'),
  ]);
  document.getElementById('my-slot-sum').textContent = `${m.slots.length}/${m.total_slots} 槽位`;
  document.getElementById('my-slots').innerHTML = m.slots.map(s => `
    <div class="flex items-center gap-2.5 px-3 py-2 rounded-xl ring-1 ring-slate-100 hover:bg-slate-50">
      <span class="skill-jump cursor-pointer font-medium text-teal-700 underline decoration-teal-200 underline-offset-2" data-skill="${s.skill_name}">${s.skill_name}</span>
      <span class="text-[10px] px-1.5 py-0.5 rounded ${BUCKET_CHIP[s.bucket] || 'bg-slate-100 text-slate-500'}">${bucketLabel(s)}</span>
      <span class="text-[10px] text-slate-400">${s.side}</span>
      <span class="flex-1"></span>
      ${s.bucket === 'pinned'
        ? (s.user_removable ? `<button class="my-pref text-[11px] px-2 py-0.5 rounded ring-1 ring-slate-200 hover:bg-slate-50" data-skill="${s.skill_name}" data-act="clear">取消 pin</button>`
                            : `<span class="text-[10px] text-slate-300 cursor-not-allowed" title="admin/全局 pin,不可取消">锁定</span>`)
        : `<button class="my-pref text-[11px] px-2 py-0.5 rounded ring-1 ring-slate-200 hover:bg-slate-50" data-skill="${s.skill_name}" data-act="pin">pin</button>
           <button class="my-pref text-[11px] px-2 py-0.5 rounded ring-1 ring-slate-200 hover:bg-slate-50 text-rose-600" data-skill="${s.skill_name}" data-act="block" title="不再推送">✕</button>`}
    </div>`).join('') || '<span class="text-slate-400">暂无槽位</span>';
  document.getElementById('my-blocked').innerHTML = m.blocked.map(b => `
    <span class="inline-flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-lg bg-rose-50 text-rose-700 ring-1 ring-rose-200">${b.skill_name}
      <button class="my-pref font-medium" data-skill="${b.skill_name}" data-act="clear">恢复</button></span>`).join('')
    || '<span class="text-[11px] text-slate-400">无</span>';
  const st = ct.steps;
  document.getElementById('my-steps').innerHTML =
    [['轨迹', st.trajs], ['原子', st.atoms], ['被采纳', st.adopted_atoms], ['进入 skill', st.skills]]
      .map(([k, v], i) => `${i ? '<span class="text-slate-300">→</span>' : ''}
        <div class="px-4 py-2 rounded-xl bg-slate-50 ring-1 ring-slate-100 text-center">
          <div class="text-lg font-semibold tabular-nums">${v}</div><div class="text-[10.5px] text-slate-400">${k}</div></div>`).join('');
  document.getElementById('my-usage').innerHTML = ct.usage.map(u => `
    <div class="flex items-center gap-2 text-[12.5px]"><span class="skill-jump cursor-pointer text-teal-700" data-skill="${u.skill}">${u.skill}</span>
      <span class="text-[11px] text-slate-400">均分 ${u.avg_score ?? '—'}</span>
      <span class="flex flex-wrap gap-1">${u.users.map(x => `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">${x.user}×${x.count}</span>`).join('')}</span></div>`).join('')
    || '<span class="text-[11px] text-slate-400">还没有被他人使用的记录</span>';
  const VC = { '高价值': 'bg-emerald-100 text-emerald-700', '正常': 'bg-slate-100 text-slate-600' };
  rows('my-rt-body', rt.rows.map(r => `<tr>
    <td class="py-2"><span class="skill-jump cursor-pointer text-teal-700" data-skill="${r.skill}">${r.skill}</span></td>
    <td class="text-right tabular-nums">${r.exposures}</td><td class="text-right tabular-nums">${r.triggers}</td>
    <td class="text-right tabular-nums">${pctf(r.rate)}</td>
    <td class="pl-6"><span class="text-[10px] px-1.5 py-0.5 rounded ${VC[r.verdict] || 'bg-rose-100 text-rose-700'}">${r.verdict}</span></td></tr>`).join(''),
    '暂无推荐记录');
}
document.addEventListener('click', async e => {
  const b = e.target.closest('.my-pref');
  if (!b) return;
  try { await jpost('/api/v1/dashboard/my/prefs', { skill_name: b.dataset.skill, action: b.dataset.act }); await loadMy(); }
  catch (err) { alert(err.message); }
});

// ── 管理 ────────────────────────────────────────────────────────
async function loadAdmin() {
  if (!IDENT || IDENT.role !== 'admin') return;
  loadClusterGraph().catch(console.error);
  const [um, sk] = await Promise.all([
    j('/api/v1/dashboard/admin/users-matrix'),
    j('/api/v1/dashboard/admin/skills'),
  ]);
  document.getElementById('admin-gpins').innerHTML = um.global_pinned.map(g => `
    <span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-violet-100 text-violet-700">${g}
      <button class="gpin-del font-bold" data-skill="${g}" title="移除全局 pin">✕</button></span>`).join('') || '<span class="text-slate-400">无</span>';
  rows('admin-users-body', um.users.map(u => {
    const pauseDetail = [u.ingest_paused_at, u.ingest_paused_by, u.ingest_pause_reason]
      .filter(Boolean).join(' · ');
    const ingestState = u.ingest_paused
      ? `<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700" title="${esc(pauseDetail)}">已暂停</span>`
      : '<span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700">处理中</span>';
    return `<tr>
      <td class="py-2 font-medium">${esc(u.user)}</td>
      <td>${u.client_version ? esc(u.client_version) : '<span class="text-slate-300">未上报</span>'}</td>
      <td class="text-right tabular-nums">${u.exposures}</td>
      <td class="text-right tabular-nums">${u.rate === null ? '—' : pctf(u.rate)}</td>
      <td class="text-right tabular-nums">${u.pinned} · ${u.blocked}</td>
      <td class="pl-6">${ingestState}</td>
      <td class="pl-6">${u.stale_advice.map(a => `<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 mr-2">${esc(a.skill)}</span>`).join('') || '<span class="text-slate-300">—</span>'}</td>
      <td class="text-right whitespace-nowrap">
        <button class="adm-ingest text-[11px] px-2 py-0.5 rounded ring-1 ${u.ingest_paused ? 'ring-emerald-200 text-emerald-700 hover:bg-emerald-50' : 'ring-amber-200 text-amber-700 hover:bg-amber-50'}" data-client-id="${esc(u.client_id)}" data-paused="${u.ingest_paused ? '1' : '0'}">${u.ingest_paused ? '恢复轨迹' : '暂停轨迹'}</button>
        <button class="adm-cfg text-[11px] px-2 py-0.5 rounded ring-1 ring-slate-200 hover:bg-slate-50 ml-1" data-user="${esc(u.user)}">配置…</button>
      </td></tr>`;
  }).join(''),
    '暂无 client');
  const ST = { active: ['在役', 'bg-emerald-100 text-emerald-700'], canary: ['灰度中', 'bg-amber-100 text-amber-700'], retired: ['已下线', 'bg-rose-100 text-rose-700'] };
  rows('admin-skills-body', sk.skills.map(s => {
    const [label, cls] = ST[s.state];
    return `<tr><td class="py-2 font-medium">${s.name}</td>
      <td><span class="text-[10px] px-1.5 py-0.5 rounded ${cls}">${label}</span></td>
      <td class="text-right tabular-nums">${s.usage_30d}</td>
      <td class="text-right">
        ${s.state === 'retired'
          ? `<button class="adm-life text-[11px] px-2 py-0.5 rounded ring-1 ring-slate-200 hover:bg-slate-50" data-skill="${s.name}" data-act="unretire">恢复在役</button>
             <button class="adm-life text-[11px] px-2 py-0.5 rounded ring-1 ring-rose-200 text-rose-700 hover:bg-rose-50 ml-1" data-skill="${s.name}" data-act="delete">删除…</button>`
          : `<button class="adm-life text-[11px] px-2 py-0.5 rounded ring-1 ring-slate-200 hover:bg-slate-50" data-skill="${s.name}" data-act="retire">下线</button>`}
      </td></tr>`;
  }).join(''), '暂无 skill');
}
async function openAdminDrawer(user) {
  const d = document.getElementById('admin-drawer');
  const p = await j('/api/v1/dashboard/admin/user/' + encodeURIComponent(user) + '/prefs');
  d.classList.remove('hidden');
  d.innerHTML = `<div class="flex items-baseline justify-between">
      <h3 class="font-medium text-[12.5px]">${user} 的偏好 <span class="text-[10.5px] text-slate-400 font-normal ml-1">pinned=${p.effective.pinned.length} blocked=${p.effective.blocked.length}</span></h3>
      <button id="adm-drawer-x" class="text-[11px] text-slate-400 hover:bg-slate-100 px-1.5 rounded">收起</button></div>
    <div class="mt-2 flex flex-wrap gap-1.5">${p.prefs.map(r => `
      <span class="inline-flex items-center gap-1 text-[10.5px] px-2 py-1 rounded-lg ${r.pref === 'pinned' ? 'bg-violet-100 text-violet-700' : 'bg-rose-50 text-rose-700'} ring-1 ring-slate-200">
        ${r.skill_name} <span class="opacity-60">${r.pref}·${r.set_by}</span>
        <button class="adm-pref font-bold" data-user="${user}" data-skill="${r.skill_name}" data-act="clear">✕</button></span>`).join('') || '<span class="text-[11px] text-slate-400">无</span>'}</div>
    <div class="mt-3 flex gap-2">
      <input id="adm-skill-in" class="ring-1 ring-slate-200 rounded-lg px-2 py-1 outline-none focus:ring-teal-500 font-mono text-[11px] w-36" placeholder="skill 名">
      <button class="adm-pref px-2 py-1 rounded-lg bg-teal-600 hover:bg-teal-700 text-white text-[11px]" data-user="${user}" data-act="pin">代 pin</button>
      <button class="adm-pref px-2 py-1 rounded-lg ring-1 ring-rose-200 text-rose-700 hover:bg-rose-50 text-[11px]" data-user="${user}" data-act="block">代屏蔽</button>
    </div>`;
}
document.addEventListener('click', async e => {
  const ingest = e.target.closest('.adm-ingest');
  if (ingest) {
    const paused = ingest.dataset.paused === '1';
    const nextPaused = !paused;
    let reason = '';
    if (nextPaused) {
      const entered = prompt('暂停后仍会接收并保存轨迹，恢复后自动补处理。可填写暂停原因：', '');
      if (entered === null) return;
      reason = entered.trim();
    } else if (!confirm('恢复该用户的轨迹处理？暂停期间积压的轨迹将在下一轮自动处理。')) {
      return;
    }
    try {
      await jpost(
        '/api/v1/dashboard/admin/client/' + encodeURIComponent(ingest.dataset.clientId) + '/ingest',
        { paused: nextPaused, reason },
        'PUT',
      );
      await loadAdmin();
    } catch (err) { alert(err.message); }
    return;
  }
  const cfg = e.target.closest('.adm-cfg');
  if (cfg) { openAdminDrawer(cfg.dataset.user).catch(err => alert(err.message)); return; }
  if (e.target.id === 'adm-drawer-x') { document.getElementById('admin-drawer').classList.add('hidden'); return; }
  const ap = e.target.closest('.adm-pref');
  if (ap) {
    const skill = ap.dataset.skill || (document.getElementById('adm-skill-in') || {}).value;
    if (!skill) return;
    try {
      await jpost('/api/v1/dashboard/admin/prefs', { user_key: ap.dataset.user, skill_name: skill.trim(), action: ap.dataset.act });
      await openAdminDrawer(ap.dataset.user); await loadAdmin();
    } catch (err) { alert(err.message); }
    return;
  }
  const gd = e.target.closest('.gpin-del');
  if (gd) {
    try { await jpost('/api/v1/dashboard/admin/prefs', { user_key: '*global*', skill_name: gd.dataset.skill, action: 'clear' }); await loadAdmin(); }
    catch (err) { alert(err.message); }
    return;
  }
  const lf = e.target.closest('.adm-life');
  if (lf) {
    const name = lf.dataset.skill, act = lf.dataset.act;
    try {
      if (act === 'delete') {
        const typed = prompt(`删除不可逆：skill 目录与 git 历史将被移除。\n请输入 skill 名确认: ${name}`);
        if (typed === null) return;
        await jpost('/api/v1/dashboard/admin/skill/' + encodeURIComponent(name), { confirm_name: typed }, 'DELETE');
      } else {
        await jpost(`/api/v1/dashboard/admin/skill/${encodeURIComponent(name)}/${act}`);
      }
      await loadAdmin();
    } catch (err) { alert(err.message); }
  }
});
document.getElementById('gpin-add').addEventListener('click', async () => {
  const v = document.getElementById('gpin-input').value.trim();
  if (!v) return;
  try { await jpost('/api/v1/dashboard/admin/prefs', { user_key: '*global*', skill_name: v, action: 'pin' }); document.getElementById('gpin-input').value = ''; await loadAdmin(); }
  catch (err) { alert(err.message); }
});

// ── 算法内核 ────────────────────────────────────────────────────
async function loadKernels() {
  if (!IDENT || IDENT.role !== 'admin') return;
  const data = await j('/api/v1/dashboard/admin/kernels');
  const active = data.kernels.find(k => k.id === data.active);
  document.getElementById('kernel-active').textContent = active ? `${active.name} (${active.id})` : data.active;
  document.getElementById('kernel-active-desc').textContent = active ? active.description : '配置的内核未被发现';
  const exportButton = document.getElementById('kernel-export');
  exportButton.disabled = !active;
  exportButton.dataset.kernel = active ? active.id : '';
  document.getElementById('kernel-plugin-dir').textContent = data.plugin_dir;
  rows('kernels-list-body', data.kernels.map(k => {
    const state = k.available
      ? (k.active ? '<span class="px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700 text-[10px]">已激活</span>' : '<span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[10px]">可用</span>')
      : `<span class="px-1.5 py-0.5 rounded bg-rose-100 text-rose-700 text-[10px]" title="${esc(k.error)}">不可用</span>`;
    const action = k.active
      ? '<span class="text-[11px] text-slate-400">当前</span>'
      : (k.available
        ? `<button class="kernel-activate text-[11px] px-2 py-0.5 rounded ring-1 ring-teal-200 text-teal-700 hover:bg-teal-50" data-kernel="${esc(k.id)}">切换</button>`
        : '<span class="text-[11px] text-slate-300">修复依赖后可选</span>');
    return `<tr><td class="py-2"><div class="font-medium">${esc(k.name)} <span class="text-[10px] text-slate-400">v${esc(k.version)}</span></div><div class="text-[11px] text-slate-400 mt-0.5">${esc(k.id)} · ${esc(k.description)}</div></td>
      <td><div>${esc(k.source)}</div><div class="text-[10px] text-slate-400 mt-0.5">${k.triggers.map(esc).join(' / ') || '—'}</div></td>
      <td><div class="font-mono text-[10px] text-slate-600">${esc(k.workspace)}</div><div class="font-mono text-[10px] text-slate-400 mt-0.5">config: ${k.config_path ? esc(k.config_path) : '平台内置'}</div></td>
      <td>${state}</td><td class="text-right">${action}</td></tr>`;
  }).join(''), '未发现内核');

  rows('kernel-eval-body', data.evaluations.map(e => `<tr>
    <td class="py-2 font-medium">${esc(e.kernel_id)}</td>
    <td class="text-right tabular-nums">${e.success_rate == null ? '—' : pctf(e.success_rate)}</td>
    <td class="text-right tabular-nums">${e.avg_duration_s == null ? '—' : e.avg_duration_s.toFixed(2) + 's'}</td>
    <td class="text-right tabular-nums">${e.input_count} / ${e.output_count}</td>
    <td class="text-right tabular-nums">${e.skills_owned}</td>
    <td class="text-right tabular-nums">${e.avg_ux == null ? '—' : e.avg_ux.toFixed(2) + ` <span class="text-[10px] text-slate-400">(${e.ux_samples})</span>`}</td>
    <td class="pl-6"><span class="text-[10px] px-1.5 py-0.5 rounded ${e.last_status === 'error' ? 'bg-rose-100 text-rose-700' : e.last_status === 'success' ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-600'}">${esc(e.last_status || '尚未运行')}</span><div class="text-[10px] text-slate-400 mt-0.5">${esc(e.last_run_at || '')}</div></td></tr>`).join(''), '暂无运行与用户反馈数据');

  rows('kernel-runs-body', data.recent_runs.map(r => `<tr>
    <td class="py-2 text-[11px]">${esc(r.started_at)}</td><td class="font-medium">${esc(r.kernel_id)} <span class="text-[10px] text-slate-400">v${esc(r.kernel_version)}</span></td>
    <td>${esc(r.trigger)} <span class="text-[10px] text-slate-400">/ ${esc(r.dataset_id)}</span></td>
    <td class="text-right tabular-nums">${r.input_count} / ${r.output_count}</td><td class="text-right tabular-nums">${Number(r.duration_s).toFixed(2)}s</td>
    <td class="pl-6"><span class="text-[10px] px-1.5 py-0.5 rounded ${r.status === 'success' ? 'bg-emerald-100 text-emerald-700' : 'bg-rose-100 text-rose-700'}" title="${esc(r.error || '')}">${esc(r.status)}</span><div class="text-[10px] text-slate-400 font-mono mt-0.5">${esc(r.run_id.slice(0, 12))}</div></td></tr>`).join(''), '还没有内核运行记录');
}

const kernelLogPause = document.getElementById('kernel-log-pause');
if (kernelLogPause) {
  kernelLogPause.addEventListener('click', () => {
    _kernelLogPaused = !_kernelLogPaused;
    kernelLogPause.textContent = _kernelLogPaused ? '继续滚动' : '暂停滚动';
  });
}
const kernelLogClear = document.getElementById('kernel-log-clear');
if (kernelLogClear) {
  kernelLogClear.addEventListener('click', () => {
    _kernelLogLines = [];
    const view = document.getElementById('kernel-log-view');
    if (view) view.textContent = '';
    const stageEl = document.getElementById('kernel-log-stage');
    if (stageEl) stageEl.textContent = 'stage=—';
  });
}

document.addEventListener('click', async e => {
  const button = e.target.closest('.kernel-activate');
  if (!button) return;
  if (!confirm(`从下一轮 sweep 开始切换为 ${button.dataset.kernel}？`)) return;
  try {
    await jpost('/api/v1/dashboard/admin/kernels/activate', { kernel_id: button.dataset.kernel });
    await Promise.all([loadKernels(), loadSettings()]);
  } catch (err) { alert(err.message); }
});
document.getElementById('kernel-export').addEventListener('click', async e => {
  const kernelId = e.currentTarget.dataset.kernel;
  if (!kernelId) return;
  try {
    const report = await j('/api/v1/dashboard/admin/kernels/export?kernel_id=' + encodeURIComponent(kernelId));
    const blob = new Blob([JSON.stringify(report, null, 2) + '\n'], { type: 'application/json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `xskill-kernel-${kernelId}-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (err) { alert(err.message); }
});

async function loadSettings() {
  if (!IDENT || IDENT.role !== 'admin') return;
  const c = await j('/api/v1/dashboard/admin/config');
  document.getElementById('cfg-path').textContent = c.path;
  document.getElementById('cfg-editor').value = c.raw;
}
async function cfgAction(endpoint) {
  const res = document.getElementById('cfg-result');
  res.className = 'text-[12.5px] text-slate-500'; res.textContent = '…';
  try {
    const r = await jpost('/api/v1/dashboard/admin/config/' + endpoint, { raw: document.getElementById('cfg-editor').value });
    if (endpoint === 'validate') { res.className = 'text-[12.5px] text-emerald-700'; res.textContent = '✓ 校验通过'; }
    else {
      res.className = 'text-[12.5px] text-emerald-700';
      res.textContent = `✓ 已生效 ${r.hot_reloaded.join('/') || '无变更'}` +
        (r.needs_restart.length ? `;⚠ ${r.needs_restart.join('/')} 段需重启 serve` : '');
      if (r.needs_restart.length) res.className = 'text-[12.5px] text-amber-700';
    }
  } catch (err) { res.className = 'text-[12.5px] text-rose-600'; res.textContent = '✗ ' + err.message; }
}
document.getElementById('cfg-validate').addEventListener('click', () => cfgAction('validate'));
document.getElementById('cfg-reload').addEventListener('click', () => cfgAction('reload'));

initIdent().catch(console.error);

// ═════════════ P3:事件流(铃铛/toast/系统通知/世界消息) + 画像可视化 ═════════════

// ── 事件语义渲染:铃铛/toast/世界消息共用同一措辞口径 ──────────────
const BAND_CLS = { '好评': 'bg-emerald-100 text-emerald-700', '差劲': 'bg-rose-100 text-rose-700', '一般': 'bg-slate-100 text-slate-600' };
const CANARY_TXT = {
  promoted: ['晋升', 'bg-emerald-100 text-emerald-700'],
  rejected: ['回滚', 'bg-rose-100 text-rose-700'],
  timeout_discarded: ['超时丢弃', 'bg-amber-100 text-amber-700'],
};
function evParts(ev) {
  const p = ev.payload || {};
  const chip = `<span class="skill-jump px-1.5 py-0.5 rounded bg-teal-50 text-teal-700 text-[11px] font-medium cursor-pointer" data-skill="${esc(ev.skill)}">${esc(ev.skill)}</span>`;
  if (ev.kind === 'feedback') {
    const badge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-medium ${BAND_CLS[p.band] || BAND_CLS['一般']}">${esc(p.band || '')}</span>`;
    return { html: `${esc(ev.actor || '匿名')} 触发了 ${chip} ${badge} <span class="text-slate-400">均分 ${esc(p.score_avg)} · ${esc(p.n_atoms)} 原子</span>`,
             plain: `${ev.actor || '匿名'} 触发了 ${ev.skill}:${p.band}(均分 ${p.score_avg})` };
  }
  if (ev.kind === 'push_edit') {
    const diff = p.ref_sha ? ` <a href="javascript:void(0)" class="ev-diff text-teal-700 underline decoration-teal-200 underline-offset-2" data-skill="${esc(ev.skill)}" data-sha="${esc(p.ref_sha)}">看 diff</a>` : '';
    return { html: `${esc(ev.actor)} 手改了 ${chip} <span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-100 text-amber-700">修改意见</span> <span class="text-slate-400">${esc(p.branch || '')}</span>${diff}`,
             plain: `${ev.actor} 对 ${ev.skill} 提交了修改意见` };
  }
  if (ev.kind === 'canary') {
    const [t, cls] = CANARY_TXT[p.action] || [p.action, 'bg-slate-100 text-slate-600'];
    return { html: `${chip} 灰度裁决 <span class="px-1.5 py-0.5 rounded text-[10px] font-medium ${cls}">${esc(t)}</span> <span class="text-slate-400">staging ${esc(p.staging_avg)} vs main ${esc(p.main_avg)}</span>`,
             plain: `${ev.skill} 灰度${t}` };
  }
  if (ev.kind === 'pin') {
    const tgt = p.scope === 'global' ? '全局' : (p.target_user && p.target_user !== ev.actor ? `给 ${p.target_user}` : '');
    return { html: `${esc(ev.actor)} pin 了 ${chip} <span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-violet-100 text-violet-700">pin${tgt ? '·' + esc(tgt) : ''}</span>`,
             plain: `${ev.actor} pin 了 ${ev.skill}${tgt ? '(' + tgt + ')' : ''}` };
  }
  return { html: esc(ev.kind), plain: ev.kind };
}
// sqlite datetime('now') 是 UTC——补 Z 再本地化
const evDate = ev => new Date(String(ev.ts || '').replace(' ', 'T') + 'Z');
function relTime(ev) {
  const m = Math.max(0, (Date.now() - evDate(ev).getTime()) / 60000);
  if (m < 1) return '刚刚';
  if (m < 60) return Math.floor(m) + ' 分钟前';
  if (m < 1440) return Math.floor(m / 60) + ' 小时前';
  return fdate(evDate(ev).toISOString());
}

// ── 全局铃铛 + 轮询 + toast + 浏览器系统通知(D10:HTTPS 增强) ─────
let _evMaxSeen = 0, _evPollTimer = null, _evPrimed = false;
const EV_POLL_MS = 30000;

function initEvents() {
  if (_evPollTimer) return;
  pollEvents().catch(console.error);
  _evPollTimer = setInterval(() => pollEvents().catch(console.error), EV_POLL_MS);
  loadWorldFeed().catch(console.error);
}

async function pollEvents() {
  if (!IDENT) return;
  const d = await j('/api/v1/dashboard/events?scope=me&limit=10');
  const badge = document.getElementById('bell-badge');
  if (badge) {
    badge.classList.toggle('hidden', !d.unread);
    badge.textContent = d.unread > 99 ? '99+' : d.unread;
  }
  const fresh = (d.events || []).filter(ev => ev.id > _evMaxSeen && !ev.read);
  const maxId = Math.max(_evMaxSeen, ...(d.events || []).map(ev => ev.id));
  if (maxId > _evMaxSeen) _evMaxSeen = maxId;
  // 首次轮询只对齐水位不弹——刷新页面不该把历史未读全弹一遍
  if (!_evPrimed) { _evPrimed = true; return; }
  fresh.slice(0, 3).reverse().forEach(ev => { toast(ev); sysNotify(ev); });
}

function toast(ev) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const { html } = evParts(ev);
  const el = document.createElement('div');
  el.className = 'toast bg-white rounded-2xl ring-1 ring-slate-200 px-4 py-3 text-xs flex items-start gap-2';
  el.innerHTML = `${avatar(ev.actor || 'xs', 'sm')}<div class="min-w-0 flex-1">${html}<div class="text-[10.5px] text-slate-400 mt-0.5">${relTime(ev)}</div></div>
    <button class="toast-x text-slate-300 hover:bg-slate-50 rounded px-1 shrink-0">✕</button>`;
  box.appendChild(el);
  el.querySelector('.toast-x').addEventListener('click', () => el.remove());
  setTimeout(() => el.remove(), 8000);
}

// Web Notifications 仅在 secure context 存在(内网 http 下 API 不存在,D10)。
// 这是能力分层:基线=铃铛+toast;HTTPS 部署才有系统级通知,入口在铃铛下拉。
function sysNotify(ev) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  if (!document.hidden) return; // 页面在前台时 toast 已足够,不重复打扰
  const n = new Notification('xskill 控制台', { body: evParts(ev).plain, tag: 'xskill-ev-' + ev.id });
  n.onclick = () => { window.focus(); if (ev.skill) location.hash = 'skill/' + encodeURIComponent(ev.skill); n.close(); };
}
function updateSysNotifBtn() {
  const b = document.getElementById('bell-sysnotif');
  if (!b) return;
  b.classList.remove('hidden');
  if (!('Notification' in window)) { b.textContent = '系统通知需 HTTPS 部署'; b.disabled = true; return; }
  if (Notification.permission === 'granted') { b.textContent = '✓ 系统通知已开启'; b.disabled = true; }
  else if (Notification.permission === 'denied') { b.textContent = '系统通知被浏览器拒绝'; b.disabled = true; }
  else {
    b.textContent = '开启系统通知'; b.disabled = false;
    b.onclick = () => Notification.requestPermission().then(updateSysNotifBtn);
  }
}

async function openBell() {
  const dd = document.getElementById('bell-dd');
  dd.classList.remove('hidden');
  updateSysNotifBtn();
  const list = document.getElementById('bell-list');
  let d;
  try { d = await j('/api/v1/dashboard/events?scope=me&limit=20'); }
  catch (e) { list.innerHTML = `<span class="text-[11px] text-rose-600 px-1">${esc(e.message)}</span>`; return; }
  list.innerHTML = (d.events || []).map(ev => `
    <div class="px-2 py-2 rounded-lg ${ev.read ? '' : 'bg-teal-50/40'} hover:bg-slate-50 text-xs flex items-start gap-2">
      ${avatar(ev.actor || 'xs', 'sm')}
      <div class="min-w-0 flex-1">${evParts(ev).html}
        <div class="text-[10.5px] text-slate-400 mt-0.5">${relTime(ev)}</div></div>
    </div>`).join('') || '<div class="text-[11px] text-slate-400 px-1 py-2">还没有通知</div>';
  const maxId = Math.max(0, ...(d.events || []).map(ev => ev.id));
  if (maxId) {
    await jpost('/api/v1/dashboard/events/read', { last_id: maxId }).catch(() => {});
    const badge = document.getElementById('bell-badge');
    if (badge) badge.classList.add('hidden');
  }
}
document.getElementById('bell-btn').addEventListener('click', e => {
  e.stopPropagation();
  const dd = document.getElementById('bell-dd');
  if (dd.classList.contains('hidden')) openBell().catch(console.error);
  else dd.classList.add('hidden');
});
document.addEventListener('click', e => {
  const dd = document.getElementById('bell-dd');
  if (dd && !dd.classList.contains('hidden') && !e.target.closest('#bell-wrap')) dd.classList.add('hidden');
});
// push-edit"看 diff":跳 skill 页后把该分支引用的 diff 灌进预览区
document.addEventListener('click', async e => {
  const ed = e.target.closest('.ev-diff');
  if (!ed) return;
  location.hash = 'skill/' + encodeURIComponent(ed.dataset.skill);
  for (let i = 0; i < 20; i++) { // 等 openSkill 渲染出预览容器
    await new Promise(r => setTimeout(r, 250));
    const pv = document.getElementById('skill-preview');
    if (pv) {
      pv.innerHTML = '<span class="text-slate-400 text-xs">加载修改意见 diff…</span>';
      try {
        const r = await j('api/v1/dashboard/skill/' + encodeURIComponent(ed.dataset.skill) + '/diff?sha=' + encodeURIComponent(ed.dataset.sha));
        pv.innerHTML = renderDiff(r.diff);
      } catch (err) { pv.innerHTML = `<span class="text-rose-600 text-xs">${esc(err.message)}</span>`; }
      pv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      return;
    }
  }
});

// ── 世界消息 feed(卡片式,按天分组;Q6 登录可见) ──────────────────
let _feedBefore = null, _feedLastDay = null;
function dayLabel(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  return diff <= 0 ? '今天' : diff === 1 ? '昨天' : `${day.getMonth() + 1}-${String(day.getDate()).padStart(2, '0')}`;
}
async function loadWorldFeed(more) {
  const el = document.getElementById('world-feed');
  if (!el || !IDENT) return;
  const q = '/api/v1/dashboard/events?scope=world&limit=30' + (more && _feedBefore ? '&before_id=' + _feedBefore : '');
  let d;
  try { d = await j(q); }
  catch (e) { el.innerHTML = `<span class="text-[11px] text-rose-600">${esc(e.message)}</span>`; return; }
  const evs = d.events || [];
  if (!more) { el.innerHTML = ''; _feedLastDay = null; }
  if (!evs.length && !more) { el.innerHTML = '<span class="text-slate-400 text-xs">还没有团队动态</span>'; return; }
  const frag = document.createElement('div');
  evs.forEach(ev => {
    const dl = dayLabel(evDate(ev));
    if (dl !== _feedLastDay) {
      frag.insertAdjacentHTML('beforeend', `<div class="text-[10.5px] text-slate-400 font-medium mt-3 mb-1.5 first:mt-0">${esc(dl)}</div>`);
      _feedLastDay = dl;
    }
    frag.insertAdjacentHTML('beforeend', `
      <div class="flex items-start gap-2.5 px-3 py-2.5 rounded-xl ring-1 ring-slate-100 hover:bg-slate-50 mb-1.5 text-xs">
        ${avatar(ev.actor || 'xs', 'sm')}
        <div class="min-w-0 flex-1">${evParts(ev).html}</div>
        <span class="text-[10.5px] text-slate-400 shrink-0" title="${esc(ev.ts)} UTC">${relTime(ev)}</span>
      </div>`);
  });
  el.appendChild(frag);
  if (evs.length) _feedBefore = evs[evs.length - 1].id;
  const moreBtn = document.getElementById('feed-more');
  if (moreBtn) moreBtn.classList.toggle('hidden', evs.length < 30);
}
const _feedMoreBtn = document.getElementById('feed-more');
if (_feedMoreBtn) _feedMoreBtn.addEventListener('click', () => loadWorldFeed(true).catch(console.error));

// ── 画像散点(图③):t-SNE 投影(邻域保持,簇分离比线性 PCA 明显),原子=圆点按簇着色,中心=◆,skill=▲ ──
const CLUSTER_COLORS = ['#0d9488', '#6366f1', '#f59e0b', '#f43f5e', '#0ea5e9'];
function scScale(pts, W, H, pad) {
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sx = x1 > x0 ? (W - 2 * pad) / (x1 - x0) : 0, sy = y1 > y0 ? (H - 2 * pad) / (y1 - y0) : 0;
  return p => ({ x: pad + (sx ? (p.x - x0) * sx : (W - 2 * pad) / 2), y: pad + (sy ? (p.y - y0) * sy : (H - 2 * pad) / 2) });
}
// 凸包(Andrew 单调链):簇描边轮廓用,点数十的量级
function convexHull(pts) {
  if (pts.length < 3) return pts.slice();
  const s = pts.slice().sort((a, b) => a.x - b.x || a.y - b.y);
  const cross = (o, a, b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const lower = [], upper = [];
  for (const p of s) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }
  for (const p of s.reverse()) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }
  return lower.slice(0, -1).concat(upper.slice(0, -1));
}
// 散点降维算法:URL 路径含 umap → 默认 UMAP(dashboarddemoumap 子路径),否则 t-SNE。
// 页内切换按钮实时改这个变量并重画,同一份数据直观对比两种投影。
let SCATTER_METHOD = location.pathname.includes('umap') ? 'umap' : 'tsne';
let _lastProfileUid = null;
const METHOD_LABEL = { tsne: 't-SNE', umap: 'UMAP' };
async function openUserProfile(uid, isRetry) {
  _lastProfileUid = uid;
  const box = document.getElementById('user-profile');
  if (!box) return;
  box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5 text-slate-400">加载 ${esc(uid)} 的画像…</div>`;
  let d;
  try { d = await j('api/v1/dashboard/user/' + encodeURIComponent(uid) + '/scatter?method=' + SCATTER_METHOD); }
  catch (e) {
    box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5 text-slate-400 text-xs">${esc(uid)}:${esc(e.message)}</div>`;
    return;
  }
  // #106 端点只读:未物化时返回 pending,显示占位并在 5s 后自动重试一次(不做复杂轮询)。
  if (d && d.status === 'pending') {
    box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5 text-slate-400 text-xs">${esc(uid)} 的画像散点计算中…${isRetry ? '' : '（约几秒后自动刷新）'}</div>`;
    if (!isRetry) setTimeout(() => { if (_lastProfileUid === uid) openUserProfile(uid, true).catch(console.error); }, 5000);
    return;
  }
  if (!(d.points || []).length) {
    box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5">
      <h2 class="font-semibold text-sm">${esc(uid)} 的兴趣画像</h2>
      <div class="mt-2 text-xs text-slate-400">${esc(d.note || '暂无可投影的原子')}</div></div>`;
    return;
  }
  const W = 680, H = 400, pad = 34;
  const all = [...d.points, ...(d.centers || []), ...(d.skills || [])];
  const sc = scScale(all, W, H, pad);
  // 簇描边:每簇 ≥3 点画凸包轮廓(半透明填充+同簇色描边),类群一眼可辨
  const byCluster = {};
  d.points.forEach(p => { (byCluster[p.cluster] = byCluster[p.cluster] || []).push(sc(p)); });
  const hullEls = Object.entries(byCluster).map(([cl, pts]) => {
    const hull = convexHull(pts);
    if (hull.length < 3) return '';
    const col = CLUSTER_COLORS[cl % CLUSTER_COLORS.length];
    const cx = hull.reduce((s, p) => s + p.x, 0) / hull.length;
    const cy = hull.reduce((s, p) => s + p.y, 0) / hull.length;
    const path = hull.map((p, i) => {  // 从质心向外扩 12px,轮廓不贴着点画
      const dx = p.x - cx, dy = p.y - cy, m = Math.sqrt(dx * dx + dy * dy) || 1;
      const x = p.x + dx / m * 12, y = p.y + dy / m * 12;
      return `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(' ') + ' Z';
    return `<path class="sc-hull" d="${path}" fill="${col}" fill-opacity="0.07"
      stroke="${col}" stroke-opacity="0.45" stroke-width="1.5" stroke-linejoin="round" stroke-dasharray="5 4"/>`;
  }).join('');
  const ptEls = d.points.map(p => {
    const c = sc(p), col = CLUSTER_COLORS[p.cluster % CLUSTER_COLORS.length];
    return `<circle class="sc-pt atom-jump cursor-pointer" data-atom="${esc(p.atom_id)}"
      data-tip="${esc(p.atom_id)}|${esc(p.summary)}|${p.ux != null ? esc(p.ux) : '—'}"
      cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="5" fill="${col}" fill-opacity="0.72"/>`;
  }).join('');
  // 兴趣中心:💡 + 簇的 tag 词直接标在图上(匹配 tag 词汇,一眼读懂这簇是什么)
  const labByCluster = Object.fromEntries((d.clusters || []).map(cl => [cl.cluster, cl.label]));
  const ctEls = (d.centers || []).map(ct => {
    const c = sc(ct), col = CLUSTER_COLORS[ct.cluster % CLUSTER_COLORS.length];
    const lab = labByCluster[ct.cluster] || `簇 ${ct.cluster}`;
    return `<g class="sc-center">
      <circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="11" fill="${col}" fill-opacity="0.14" stroke="${col}" stroke-width="1.5"/>
      <text x="${c.x.toFixed(1)}" y="${(c.y + 4.5).toFixed(1)}" font-size="12" text-anchor="middle">💡</text>
      <text x="${c.x.toFixed(1)}" y="${(c.y + 26).toFixed(1)}" font-size="10.5" font-weight="700" fill="${col}" text-anchor="middle">${esc(lab)}</text>
      <title>兴趣点 · ${esc(lab)}</title></g>`;
  }).join('');
  const skEls = (d.skills || []).map(s => {
    const c = sc(s);
    const short = s.name.length > 14 ? s.name.slice(0, 13) + '…' : s.name;
    const hub = s.source === 'skillhub';         // 三方 skill 区分:琥珀色 ▲ + tooltip 标"第三方"
    const fill = hub ? '#d97706' : '#0f172a';
    const tip = `${hub ? '第三方 ' : ''}SKILL:${esc(s.name)} · 触发 ${esc(s.use_count)} 次`;
    return `<g class="skill-jump cursor-pointer" data-skill="${esc(s.name)}">
      <path d="M${c.x.toFixed(1)} ${(c.y - 7).toFixed(1)} l6.2 11 h-12.4 z" fill="${fill}"><title>${tip}</title></path>
      <text x="${c.x.toFixed(1)}" y="${(c.y + 17).toFixed(1)}" font-size="9.5" font-weight="600" fill="#334155" text-anchor="middle">SKILL:${esc(short)}</text></g>`;
  }).join('');
  const legend = (d.clusters || []).map(cl =>
    `<span class="inline-flex items-center gap-1 text-[11px] font-medium" style="color:${CLUSTER_COLORS[cl.cluster % CLUSTER_COLORS.length]}">💡${esc(cl.label)}</span>`).join('');
  const cur = d.method || SCATTER_METHOD;
  const seg = ['tsne', 'umap'].map(m =>
    `<button class="scatter-method px-2 py-0.5 rounded-md text-[11px] font-medium ${m === cur ? 'bg-teal-600 text-white' : 'text-slate-500 hover:bg-slate-50'}" data-method="${m}">${METHOD_LABEL[m]}</button>`).join('');
  box.innerHTML = `<div class="bg-white rounded-2xl ring-1 ring-slate-200 p-5">
    <div class="flex items-baseline justify-between flex-wrap gap-2">
      <h2 class="font-semibold text-sm flex items-center gap-2">${avatar(uid)} ${esc(uid)} 的兴趣画像
        <span class="font-normal text-[11px] text-slate-400">${METHOD_LABEL[cur]} 2D 投影 · 悬停原子预览,点击跳详情</span></h2>
      <div class="flex gap-3 flex-wrap items-center">
        <span class="inline-flex items-center gap-1 ring-1 ring-slate-200 rounded-lg px-1 py-0.5">${seg}</span>
        ${legend}
        <span class="inline-flex items-center gap-1.5 text-[11px] text-slate-600"><svg width="11" height="11"><path d="M5.5 1 l4.5 8 h-9 z" fill="#0f172a"/></svg>SKILL:技能名</span></div>
    </div>
    <svg viewBox="0 0 ${W} ${H}" class="w-full mt-2" style="max-height:440px" id="scatter-svg">
      <rect x="0" y="0" width="${W}" height="${H}" rx="14" fill="#f8fafc"/>
      ${hullEls}${ptEls}${ctEls}${skEls}
    </svg>
    <div class="text-[10.5px] text-slate-400 mt-1.5">画像更新于 ${fdate(d.updated_at)} · ${d.sampled ? `显示 ${d.shown}/${d.total} 个原子（按兴趣中心分层抽样）` : `${d.points.length} 个原子点`}${(d.skills || []).length ? '' : ' · skill 向量索引缺失,不显示 ▲(不现算)'}</div>
  </div>`;
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
// 散点算法切换:t-SNE ↔ UMAP,同一份数据换降维算法重画
document.addEventListener('click', e => {
  const btn = e.target.closest && e.target.closest('.scatter-method');
  if (!btn || btn.dataset.method === SCATTER_METHOD) return;
  SCATTER_METHOD = btn.dataset.method;
  if (_lastProfileUid) openUserProfile(_lastProfileUid).catch(console.error);
});
// 散点 hover 预览卡(自定义 tooltip,跟随鼠标)
document.addEventListener('mousemove', e => {
  const tip = document.getElementById('scatter-tip');
  if (!tip) return;
  const pt = e.target.closest && e.target.closest('.sc-pt');
  if (!pt) { tip.classList.add('hidden'); return; }
  const [aid, summary, uxv] = (pt.dataset.tip || '').split('|');
  tip.innerHTML = `<div class="font-mono text-[10px] text-slate-400">${esc(aid)}</div>
    <div class="mt-0.5">${esc(summary) || '—'}</div>
    <div class="mt-0.5 text-slate-400">ux ${esc(uxv)}</div>`;
  tip.style.left = (e.clientX + 14) + 'px';
  tip.style.top = (e.clientY + 14) + 'px';
  tip.classList.remove('hidden');
});

// ── admin 画像聚类 graph(手写 force-directed,用户十的量级) ──────
function forceLayout(nodes, edges, W, H) {
  const n = nodes.length;
  nodes.forEach((nd, i) => {  // 确定性初始化:圆周排布(无随机,布局可复现)
    const a = i / n * 2 * Math.PI;
    nd.x = W / 2 + Math.cos(a) * Math.min(W, H) / 3.4;
    nd.y = H / 2 + Math.sin(a) * Math.min(W, H) / 3.4;
  });
  const at = Object.fromEntries(nodes.map((nd, i) => [nd.user, i]));
  const ITER = 260, K = 100;
  for (let it = 0; it < ITER; it++) {
    const t = 1 - it / ITER;
    const fx = new Array(n).fill(0), fy = new Array(n).fill(0);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {  // 斥力
      const dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
      const d2 = dx * dx + dy * dy || 1, d = Math.sqrt(d2);
      const f = K * K / d2 * 6;
      fx[i] += dx / d * f; fy[i] += dy / d * f;
      fx[j] -= dx / d * f; fy[j] -= dy / d * f;
    }
    edges.forEach(e2 => {  // 弹簧:相似度越高理想边越短
      const i = at[e2.source], j2 = at[e2.target];
      if (i == null || j2 == null) return;
      const dx = nodes[j2].x - nodes[i].x, dy = nodes[j2].y - nodes[i].y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const ideal = K * (1.7 - e2.sim);
      const f = (d - ideal) * 0.05;
      fx[i] += dx / d * f; fy[i] += dy / d * f;
      fx[j2] -= dx / d * f; fy[j2] -= dy / d * f;
    });
    nodes.forEach((nd, i) => {  // 向心 + 步长退火
      fx[i] += (W / 2 - nd.x) * 0.015; fy[i] += (H / 2 - nd.y) * 0.015;
      const cap = 8 * t + 0.5, m = Math.sqrt(fx[i] * fx[i] + fy[i] * fy[i]) || 1;
      nd.x += fx[i] / m * Math.min(m, cap);
      nd.y += fy[i] / m * Math.min(m, cap);
      nd.x = Math.max(40, Math.min(W - 40, nd.x));
      nd.y = Math.max(28, Math.min(H - 28, nd.y));
    });
  }
}
async function loadClusterGraph() {
  const el = document.getElementById('cluster-graph');
  if (!el || !IDENT || IDENT.role !== 'admin') return;
  let g;
  try { g = await j('/api/v1/dashboard/admin/cluster-graph'); }
  catch (e) { el.innerHTML = `<span class="text-slate-400 text-xs">${esc(e.message)}</span>`; return; }
  if (!(g.nodes || []).length) { el.innerHTML = '<span class="text-slate-400 text-xs">还没有任何用户画像</span>'; return; }
  const W = 680, H = 380;
  forceLayout(g.nodes, g.edges || [], W, H);
  const at = Object.fromEntries(g.nodes.map(nd => [nd.user, nd]));
  const edgeEls = (g.edges || []).map(e2 => {
    const a = at[e2.source], b = at[e2.target];
    const wpx = 1.5 + (e2.sim - g.threshold) / Math.max(0.001, 1 - g.threshold) * 6;
    const tipTxt = `相似度 ${e2.sim}${e2.common_tags.length ? ' · 共同标签:' + e2.common_tags.join('/') : ''}${e2.common_skills.length ? ' · 共同 skill:' + e2.common_skills.join('/') : ''}`;
    return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"
      stroke="#5eead4" stroke-width="${wpx.toFixed(1)}" stroke-linecap="round" class="cursor-help"><title>${esc(tipTxt)}</title></line>`;
  }).join('');
  const maxAtoms = Math.max(1, ...g.nodes.map(nd => nd.atoms));
  const nodeEls = g.nodes.map(nd => {
    const r = 10 + Math.sqrt(nd.atoms / maxAtoms) * 14;
    const fill = nd.isolated ? '#cbd5e1' : '#0d9488';
    const tipTxt = `${nd.user} · ${nd.atoms} 原子${nd.top_tags.length ? ' · ' + nd.top_tags.join('/') : ''}${nd.isolated ? ' · 冷启动(无相似用户)' : ''}`;
    return `<g class="cg-node cursor-pointer" data-user="${esc(nd.user)}">
      <circle cx="${nd.x.toFixed(1)}" cy="${nd.y.toFixed(1)}" r="${r.toFixed(1)}" fill="${fill}" fill-opacity="0.88"><title>${esc(tipTxt)}</title></circle>
      <text x="${nd.x.toFixed(1)}" y="${(nd.y + 4).toFixed(1)}" font-size="10" fill="#fff" text-anchor="middle" font-weight="600">${esc(String(nd.user).slice(0, 2))}</text>
      <text x="${nd.x.toFixed(1)}" y="${(nd.y + r + 12).toFixed(1)}" font-size="9.5" fill="${nd.isolated ? '#94a3b8' : '#475569'}" text-anchor="middle">${esc(nd.user)}${nd.isolated ? ' · 冷启动' : ''}</text>
    </g>`;
  }).join('');
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="w-full" style="max-height:420px">
    <rect x="0" y="0" width="${W}" height="${H}" rx="14" fill="#f8fafc"/>${edgeEls}${nodeEls}</svg>
    <div class="text-[10.5px] text-slate-400 mt-1.5">点节点看该用户画像散点 · 边阈值 ${g.threshold}</div>`;
}
document.addEventListener('click', e => {
  const nd = e.target.closest('.cg-node');
  if (!nd) return;
  location.hash = '#users';
  openUserProfile(nd.dataset.user).catch(console.error);
});
