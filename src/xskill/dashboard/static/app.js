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
let skillsQ = '';
let _skillsQTimer = null;
const SKILLS_PAGE_SIZE = 10;

function libRelationHtml(s) {
  if (s.pinned) {
    return `<span class="text-[10px] px-1.5 py-0.5 rounded ${BUCKET_CHIP.pinned}">${esc(bucketLabel({
      bucket: 'pinned', pin_scope: s.pin_scope, user_removable: s.user_removable,
    }))}</span>`;
  }
  if (s.in_push) {
    const cls = BUCKET_CHIP[s.bucket] || 'bg-slate-100 text-slate-500';
    return `<span class="text-[10px] px-1.5 py-0.5 rounded ${cls}">推给我</span>`;
  }
  return '';
}

function libStarHtml(s) {
  if (s.pinned && !s.user_removable) {
    return `<span class="inline-flex items-center justify-center w-7 h-7 text-amber-400 opacity-70" title="admin/全局 pin，不可取消">${_myStarSvg(true)}</span>`;
  }
  if (s.pinned) {
    return `<button type="button" class="lib-star inline-flex items-center justify-center w-7 h-7 rounded-md text-amber-400 hover:text-amber-500 hover:bg-slate-50"
      data-skill="${esc(s.name)}" data-act="clear" title="已 pin · 点击取消" aria-label="取消 pin">${_myStarSvg(true)}</button>`;
  }
  return `<button type="button" class="lib-star inline-flex items-center justify-center w-7 h-7 rounded-md text-slate-300 hover:text-amber-300 hover:bg-slate-50"
    data-skill="${esc(s.name)}" data-act="pin" title="未 pin · 点击加入推荐流" aria-label="pin">${_myStarSvg(false)}</button>`;
}

async function loadSkills() {
  const off = skillsPage * SKILLS_PAGE_SIZE;
  const qEl = document.getElementById('skills-q');
  if (qEl && qEl.value !== skillsQ) skillsQ = qEl.value;
  const q = (skillsQ || '').trim();
  const sp = new URLSearchParams({
    limit: String(SKILLS_PAGE_SIZE),
    offset: String(off),
  });
  if (q) sp.set('q', q);
  const d = await j(`api/v1/dashboard/skills?${sp}`);
  const bs = d.by_state || {};
  const parts = Object.keys(bs).sort().map(k => `${k} ${bs[k]}`).join(' · ');
  put('skills.summary', `${q ? '匹配' : '共'} ${d.total} 个${parts ? ' · ' + parts : ''}`);
  const canPin = !!(d.viewer && d.viewer.can_pin);
  rows('skills-body', (d.skills || []).map(s =>
    `<tr class="hover:bg-slate-50 cursor-pointer" data-skill-row="${esc(s.name)}">`
    + (canPin ? `<td class="py-2.5 w-8">${libStarHtml(s)}</td>` : '<td class="py-2.5 w-8"></td>')
    + `<td class="py-2.5 font-medium text-teal-700"><span class="inline-flex items-center gap-1.5 flex-wrap">${esc(s.name)}${sourceBadge(s)}${libRelationHtml(s)}</span></td>`
    + `<td>${stateBadge(s.state)}</td>`
    + `<td class="text-slate-500 max-w-[480px] truncate" title="${esc(s.description)}">${esc(s.description) || '—'}</td>`
    + `<td class="text-right tabular-nums">v${esc(s.version)}</td>`
    + `<td class="text-right tabular-nums">${s.candidates || 0}</td></tr>`).join(''),
    q ? '无匹配技能' : '技能库还是空的');
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
    return `<div class="h-12 flex items-center justify-between gap-2 rounded-lg px-2 -mx-2 cursor-pointer hover:bg-slate-50 ${rowCls}" data-gnode="${esc(n.sha)}" data-gside="${n.is_head_staging ? 'staging' : (n.is_head_main ? 'main' : '')}">
      <div class="min-w-0"><div class="font-medium truncate">${esc(n.subject) || '(无提交说明)'}${rej}</div>
        <div class="text-[11px] ${subCls}">${esc(sub)}${n.is_head_staging ? ' · 点击看推送给谁' : (n.is_head_main ? ' · 点击看推送给谁' : '')}</div></div>
      <code class="text-[11px] text-slate-400 shrink-0">${esc(n.sha.slice(0, 7))}</code></div>`;
  }).join('');
  const unloc = (g.decisions_unlocated || []).length;
  return `<div class="flex mt-3">${svg}<div class="flex-1 min-w-0" style="padding-top:2px">${rowsHtml}</div></div>
    <div class="flex gap-4 mt-3 pt-3 border-t border-slate-100 text-[11px] text-slate-500 flex-wrap">
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-emerald-500"></span>晋升</span>
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-rose-500"></span>回滚</span>
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-amber-400"></span>观察中（点黄点看推送对象）</span>
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
let _skillBackHash = null; // 从「我的」贡献图点入时回到 #my
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
  const back = _skillBackHash === 'my'
    ? `<a href="#my" class="text-teal-700 hover:underline">我的</a>`
    : '技能库';
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
  const sc = d.scripting || {};
  let scriptBtn = '';
  if (typeof IDENT !== 'undefined' && IDENT && IDENT.role === 'admin') {
    const disabled = !sc.enabled;
    const title = sc.reason || '把当前主干技能改得更偏可执行脚本';
    scriptBtn = `<button type="button" class="px-2.5 py-1 rounded-lg text-xs ring-1 ${disabled ? 'bg-slate-50 text-slate-400 ring-slate-200 cursor-not-allowed' : 'bg-white text-teal-800 ring-teal-200 hover:bg-teal-50'}" data-skill-scripting="${esc(name)}" ${disabled ? 'disabled' : ''} title="${esc(title)}">脚本化（实验性）</button>`;
  }

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
    <div class="text-xs text-slate-400 mb-1.5">${back} <span class="mx-1">/</span> <span class="text-slate-600">${esc(name)}</span></div>
    <div class="flex items-start justify-between gap-3 flex-wrap">
      <div>
        <h2 class="text-lg font-bold tracking-tight">${esc(name)}</h2>
        <div class="text-slate-500 text-xs mt-1">总触发 <b class="text-slate-800 tabular-nums">${d.total_triggers}</b> 次
          · 贡献原子 <b class="text-slate-800 tabular-nums">${(lin.atoms || []).length}</b> 个
          ${lin.avg_ux != null ? `· 血缘平均 ux <b class="text-slate-800 tabular-nums">${lin.avg_ux}</b>` : ''}</div>
      </div>
      <div class="flex gap-2 items-center flex-wrap">${headChips}${scriptBtn}</div>
    </div>

    <div class="grid grid-cols-12 gap-4 mt-4">
      <div class="col-span-12 lg:col-span-5 rounded-2xl ring-1 ring-slate-200 p-5">
        <div class="flex items-baseline justify-between">
          <h3 class="font-semibold text-sm">进化路径</h3>
          <span class="text-[11px] text-slate-400">点黄点 / main HEAD 看推送给谁；其它节点看 diff</span>
        </div>
        ${g ? renderGraph(g) : '<div class="text-slate-400 text-xs mt-3">非 git 仓，暂无进化路径</div>'}
      </div>
      <div class="col-span-12 lg:col-span-7 space-y-4">
        <div id="skill-routing" class="hidden"></div>
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
  loadSkillRouting(name).catch(console.error);
}

const ROUTE_PAGE_SIZE = 6;
function _routeColState() {
  return { page: 0, users: [], total: 0, seq: 0 };
}
let _routeState = {
  skill: null, meta: null, focusSide: null,
  cols: { staging: _routeColState(), main: _routeColState() },
  q: '', suggest: [], suggestSeq: 0,
};
let _routeSuggestTimer = null;

function _routeUsersUrl(extra) {
  const sp = new URLSearchParams(extra);
  return 'api/v1/dashboard/skill/' + encodeURIComponent(_routeState.skill) + '/routing/users?' + sp.toString();
}

function _routePushPill(u) {
  return u.in_manifest
    ? `<span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 ring-1 ring-emerald-100 shrink-0">推送</span>`
    : `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 ring-1 ring-slate-200 shrink-0">未推送</span>`;
}

function _routeCanEdit(user) {
  if (!IDENT) return false;
  if (IDENT.role === 'admin') return true;
  return IDENT.user === user;
}

function _routeUserRowHtml(u) {
  const skill = _routeState.skill || '';
  const hasStaging = !!(_routeState.meta && _routeState.meta.has_staging);
  const curSide = u.side || 'main';
  const canEdit = _routeCanEdit(u.user);
  let sideCtl;
  if (hasStaging && canEdit) {
    const sideBtn = (side, label) => {
      const on = curSide === side;
      return `<button type="button" class="route-row-side px-1.5 py-0.5 ${on ? (side === 'staging' ? 'bg-amber-400 text-white' : 'bg-teal-600 text-white') : 'bg-white text-slate-500 hover:bg-slate-50'}"
        data-user="${esc(u.user)}" data-side="${side}" data-skill="${esc(skill)}" title="pin 到 ${label}">${label}</button>`;
    };
    sideCtl = `<div class="inline-flex rounded-md ring-1 ring-slate-200 overflow-hidden text-[10px]">${sideBtn('main', 'main')}${sideBtn('staging', 'staging')}</div>`;
  } else if (hasStaging) {
    sideCtl = `<span class="text-[10px] px-1.5 py-0.5 rounded-md ${curSide === 'staging' ? 'bg-amber-50 text-amber-700 ring-1 ring-amber-200' : 'bg-slate-100 text-slate-500'}">${esc(curSide)}</span>`;
  } else {
    sideCtl = `<span class="text-[10px] text-slate-400 px-1">main</span>`;
  }
  const starCls = !u.pinned
    ? 'text-slate-300' + (canEdit ? ' hover:text-amber-300' : '')
    : (u.overridden ? 'text-violet-500' + (canEdit ? ' hover:text-violet-600' : '') : 'text-amber-400' + (canEdit ? ' hover:text-amber-500' : ''));
  const starSvg = u.pinned
    ? `<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 1.2l1.76 3.56 3.93.57-2.84 2.77.67 3.91L8 10.16l-3.52 1.85.67-3.91L2.3 5.33l3.93-.57L8 1.2z"/></svg>`
    : `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M8 1.2l1.76 3.56 3.93.57-2.84 2.77.67 3.91L8 10.16l-3.52 1.85.67-3.91L2.3 5.33l3.93-.57L8 1.2z"/></svg>`;
  let starBtn;
  if (!canEdit) {
    starBtn = `<span class="inline-flex items-center justify-center w-7 h-7 ${starCls} opacity-70" title="${u.pinned ? '已 pin（只读）' : '未 pin（只读）'}">${starSvg}</span>`;
  } else if (u.pinned) {
    starBtn = `<button type="button" class="route-row-unpin inline-flex items-center justify-center w-7 h-7 rounded-md hover:bg-slate-50 ${starCls}"
        data-user="${esc(u.user)}" data-skill="${esc(skill)}" title="已 pin · 点击取消" aria-label="取消 pin">${starSvg}</button>`;
  } else {
    starBtn = `<button type="button" class="route-row-pin inline-flex items-center justify-center w-7 h-7 rounded-md hover:bg-slate-50 ${starCls}"
        data-user="${esc(u.user)}" data-side="${esc(curSide)}" data-skill="${esc(skill)}" title="未 pin · 点击 pin 到当前 side" aria-label="pin">${starSvg}</button>`;
  }
  return `<div class="route-user-row flex items-center gap-2 py-2 border-b border-slate-50 last:border-0" data-user="${esc(u.user)}">
    ${avatar(u.user, 'sm')}
    <div class="min-w-0 flex-1">
      <div class="text-[12.5px] font-medium flex items-center gap-1.5 flex-wrap">
        <span>${esc(u.user)}</span>
        ${_routePushPill(u)}
        ${u.overridden ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-violet-100 text-violet-700">pinned</span>' : ''}
        ${u.auto_canary && !u.overridden ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-sky-50 text-sky-700 ring-1 ring-sky-100">自动灰度</span>' : ''}
      </div>
      <div class="text-[10.5px] text-slate-400 mt-0.5 flex items-center gap-1.5">
        ${u.sha ? `<code>${esc(String(u.sha).slice(0, 7))}</code>` : '<span>—</span>'}
        ${u.in_manifest ? '' : '<span>不在 manifest</span>'}
      </div>
    </div>
    <div class="shrink-0 flex items-center gap-1.5">
      ${sideCtl}
      ${starBtn}
    </div>
  </div>`;
}

async function _routePref(user, action, side) {
  const skill = _routeState.skill;
  if (!skill || !user) return;
  if (!_routeCanEdit(user)) {
    throw new Error('无权配置其他用户：仅管理员可代操作，普通用户只能改自己');
  }
  const body = { user_key: user, skill_name: skill, action };
  if (side) body.side = side;
  if (IDENT && IDENT.role === 'admin') {
    await jpost('/api/v1/dashboard/admin/prefs', body);
  } else {
    await jpost('/api/v1/dashboard/my/prefs', body);
  }
  const sg = document.getElementById('route-suggest');
  if (sg) { sg.classList.add('hidden'); sg.innerHTML = ''; }
  await loadSkillRouting(skill, _routeState.focusSide);
  if (IDENT && IDENT.role === 'admin' && typeof loadAdmin === 'function') {
    await loadAdmin().catch(() => {});
  }
}

function _paintColPager(col) {
  const st = _routeState.cols[col];
  if (!st) return;
  const pages = Math.max(1, Math.ceil(st.total / ROUTE_PAGE_SIZE));
  const atFirst = st.page <= 0;
  const atLast = st.page >= pages - 1;
  const box = document.getElementById('route-pager-' + col);
  if (!box) return;
  const mk = (dir, label, disabled, title) =>
    `<button type="button" data-route-col="${col}" data-route-page="${dir}" title="${title}" ${disabled ? 'disabled' : ''}
      class="route-page-btn w-6 h-6 inline-flex items-center justify-center rounded-md ring-1 ring-slate-200 text-[11px] leading-none
      ${disabled ? 'text-slate-300 cursor-not-allowed' : 'text-slate-700 hover:bg-white'}">${label}</button>`;
  box.innerHTML = `
    ${mk('up', '▲', atFirst, '上一页')}
    <span class="text-[10.5px] text-slate-500 tabular-nums px-1">${st.page + 1}/${pages}</span>
    ${mk('down', '▼', atLast, '下一页')}
    <span class="text-[10px] text-slate-400 ml-1">${st.total} 人</span>`;
  box.querySelectorAll('.route-page-btn').forEach(btn => {
    btn.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (btn.disabled) return;
      _routeGoColPage(col, btn.dataset.routePage === 'up' ? -1 : 1);
    };
  });
}

function _paintColList(col) {
  const st = _routeState.cols[col];
  const body = document.getElementById('route-list-' + col);
  if (!body || !st) return;
  body.innerHTML = (st.users || []).map(_routeUserRowHtml).join('')
    || `<div class="text-[11px] text-slate-400 py-3">当前无人被推送此${col === 'staging' ? '灰度' : '主干'}版本</div>`;
  _paintColPager(col);
}

function _routeGoColPage(col, delta) {
  const st = _routeState.cols[col];
  if (!st) return;
  const pages = Math.max(1, Math.ceil(st.total / ROUTE_PAGE_SIZE));
  const next = Math.max(0, Math.min(pages - 1, st.page + delta));
  if (next === st.page) return;
  st.page = next;
  _fetchRouteCol(col).catch(console.error);
}

async function _fetchRouteCol(col) {
  if (!_routeState.skill || !_routeState.cols[col]) return;
  const st = _routeState.cols[col];
  const seq = ++st.seq;
  const body = document.getElementById('route-list-' + col);
  if (body) body.innerHTML = `<div class="text-[11px] text-slate-400 py-3">加载中…</div>`;
  try {
    const d = await j(_routeUsersUrl({
      filter: col,
      offset: String(st.page * ROUTE_PAGE_SIZE),
      limit: String(ROUTE_PAGE_SIZE),
    }));
    if (seq !== st.seq) return;
    st.users = d.users || [];
    st.total = d.total || 0;
    const pages = Math.max(1, Math.ceil(st.total / ROUTE_PAGE_SIZE));
    if (st.page >= pages) {
      st.page = pages - 1;
      return _fetchRouteCol(col);
    }
    _paintColList(col);
  } catch (err) {
    if (seq !== st.seq) return;
    if (body) body.innerHTML = `<div class="text-[11px] text-rose-600 py-3">${esc(err.message)}</div>`;
  }
}

async function _fetchRouteCols() {
  const jobs = [_fetchRouteCol('main')];
  if (_routeState.meta && _routeState.meta.has_staging) jobs.unshift(_fetchRouteCol('staging'));
  await Promise.all(jobs);
}

async function _fetchRouteSuggest(q) {
  const box = document.getElementById('route-suggest');
  if (!box || !_routeState.skill) return;
  q = (q || '').trim();
  if (q.length < 1) {
    box.classList.add('hidden');
    box.innerHTML = '';
    _routeState.suggest = [];
    return;
  }
  const seq = ++_routeState.suggestSeq;
  box.classList.remove('hidden');
  box.innerHTML = `<div class="px-3 py-2 text-[11px] text-slate-400">检索中…</div>`;
  try {
    const d = await j(_routeUsersUrl({ q, limit: '8' }));
    if (seq !== _routeState.suggestSeq) return;
    _routeState.suggest = d.users || [];
    if (!_routeState.suggest.length) {
      box.innerHTML = `<div class="px-3 py-2 text-[11px] text-slate-400">无匹配（最多返回 8 条）</div>`;
      return;
    }
    box.innerHTML = `<div class="px-2">${_routeState.suggest.map(_routeUserRowHtml).join('')}</div>`;
  } catch (err) {
    if (seq !== _routeState.suggestSeq) return;
    box.innerHTML = `<div class="px-3 py-2 text-[11px] text-rose-600">${esc(err.message)}</div>`;
  }
}

async function loadSkillRouting(name, focusSide) {
  const box = document.getElementById('skill-routing');
  if (!box) return;
  let meta;
  try {
    meta = await j('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/routing');
  } catch (_e) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  const keep = _routeState.skill === name;
  _routeState.skill = name;
  _routeState.meta = meta;
  _routeState.suggest = [];
  _routeState.focusSide = (focusSide === 'staging' || focusSide === 'main') ? focusSide : null;
  if (!keep) {
    _routeState.q = '';
    _routeState.cols = { staging: _routeColState(), main: _routeColState() };
  }
  const c = meta.counts || {};
  const focusCls = (side) => (_routeState.focusSide === side
    ? (side === 'staging' ? 'ring-2 ring-amber-200 bg-amber-50' : 'ring-2 ring-slate-300 bg-slate-50')
    : '');
  box.classList.remove('hidden');
  box.innerHTML = `
    <div class="rounded-2xl ring-1 ring-slate-200 p-5" id="skill-routing-card" data-skill="${esc(name)}">
      <div class="flex items-baseline justify-between flex-wrap gap-2">
        <h3 class="font-semibold text-sm">当前推送对象</h3>
        <span class="text-[11px] text-slate-400">${meta.has_staging
          ? `staging ${c.staging || 0} · main ${c.main || 0} · manifest ${c.in_manifest || 0}/${c.users || 0}`
          : `无 staging · 仅 main ${c.main || 0}`}</span>
      </div>

      <div class="mt-3 relative max-w-md">
        <div class="text-[11px] text-slate-400 mb-1">检索用户 <span class="text-slate-300">· ${
          IDENT && IDENT.role === 'admin'
            ? '管理员可代 pin / 配 side · ≤8'
            : '仅可配置自己 · 他人只读 · ≤8'
        }</span></div>
        <input id="route-user-q" value="${esc(_routeState.q)}" autocomplete="off" placeholder="输入关键字，如 alice / user-0"
          class="w-full ring-1 ring-slate-200 rounded-lg px-2.5 py-1.5 text-[12.5px] outline-none focus:ring-teal-500 bg-white">
        <div id="route-suggest" class="hidden absolute z-20 left-0 right-0 mt-1 bg-white rounded-xl ring-1 ring-slate-200 shadow-lg max-h-72 overflow-y-auto"></div>
      </div>

      <div class="grid grid-cols-1 ${meta.has_staging ? 'md:grid-cols-2' : ''} gap-4 mt-4">
        ${meta.has_staging ? `<div id="route-panel-staging" class="rounded-xl p-1 ${focusCls('staging')}">
          <div class="flex items-center justify-between gap-2 px-2 mb-1">
            <div class="text-[11px] font-medium text-slate-600 flex items-center gap-1.5">
              <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-amber-50 text-amber-700 ring-1 ring-amber-200">
                <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>staging
              </span>
              <span>灰度版推送给</span>
            </div>
            <div id="route-pager-staging" class="flex items-center"></div>
          </div>
          <div id="route-list-staging" class="rounded-xl ring-1 ring-amber-200 bg-amber-50 px-3 min-h-[8rem]"></div>
        </div>` : ''}
        <div id="route-panel-main" class="rounded-xl p-1 ${focusCls('main')}">
          <div class="flex items-center justify-between gap-2 px-2 mb-1">
            <div class="text-[11px] font-medium text-slate-600 flex items-center gap-1.5">
              <span class="inline-flex items-center px-1.5 py-0.5 rounded-md bg-slate-100 text-slate-600 ring-1 ring-slate-200">main</span>
              <span>主干版推送给</span>
            </div>
            <div id="route-pager-main" class="flex items-center"></div>
          </div>
          <div id="route-list-main" class="rounded-xl ring-1 ring-slate-200 bg-white px-3 min-h-[8rem]"></div>
        </div>
      </div>
    </div>`;
  await _fetchRouteCols();
  if (_routeState.focusSide) {
    const panel = document.getElementById('route-panel-' + _routeState.focusSide);
    if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  const qEl = document.getElementById('route-user-q');
  if (qEl) {
    qEl.oninput = () => {
      _routeState.q = qEl.value;
      clearTimeout(_routeSuggestTimer);
      _routeSuggestTimer = setTimeout(() => _fetchRouteSuggest(_routeState.q), 180);
    };
    qEl.onkeydown = (ev) => {
      if (ev.key === 'Escape') {
        const sg = document.getElementById('route-suggest');
        if (sg) { sg.classList.add('hidden'); sg.innerHTML = ''; }
      }
    };
  }
}

function focusSkillRouting(side) {
  if (!_curSkill) return;
  loadSkillRouting(_curSkill, side).catch(console.error);
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

// ═════════════ 流水线 Monitor（四栏固定席位：后两栏共用 edit 池） ═════════════
// 数据：pipeline/live（agent-worker 状态文件只读整形，~5s 粒度），3s 轮询；
// 点选席位/任务后 pipeline/log 轮询日志尾巴。禁止 fallback：状态文件缺失、
// 日志不存在、席位已腾空一律显式空态，绝不编造。
const PM_POOLS = [
  { key: 'split', title: 'Split', subtitle: '轨迹', weight: true, batch: false },
  { key: 'cluster', title: 'Cluster', subtitle: 'atom 批', weight: true, batch: true },
  { key: 'edit', title: 'SkillEditAgent', subtitle: 'A→B 转移', weight: true, batch: true },
  { key: 'generate', title: 'GenerateAgent', subtitle: '与 SkillEdit 共用席位', weight: true, batch: false, shared: 'edit' },
];
const PM_XFER = {
  baby_main: { from: 'baby', to: 'main', tip: '冷启动消化后 graduate' },
  main_staging: { from: 'main', to: 'staging', tip: '产灰度候选' },
  staging_main: { from: 'staging', to: 'main', tip: 'Jam 强砍回 main' },
  main_scripting: { from: 'main', to: 'scripting', tip: '把主干技能脚本化（实验性）', label: 'main scripting' },
};
const PM_POLL_MS = 3000, PM_LOG_MS = 2500;
let pmState = null;      // 最近一次 live 响应
let pmFetchAt = 0;       // 响应到达的本地时刻（席位龄期在两次轮询间本地走秒）
let pmSelected = null;   // {pool, seat}：席位号才是稳定身份（任务完成只清该坑）
let pmPollTimer = null, pmLogTimer = null, pmLogKey = null;

function pmAgeText(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + 's';
  if (sec < 3600) return (sec / 60).toFixed(1) + 'm';
  return (sec / 3600).toFixed(1) + 'h';
}
// 席位色：浅=刚起，深=已久（约 0→120s 饱和）：#99f6e4 → #0f766e → #042f2e
function pmSeatColor(ageSec) {
  const t = Math.max(0, Math.min(1, ageSec / 120));
  const stops = [[153, 246, 228], [15, 118, 110], [4, 47, 46]];
  const [a, b, local] = t < 0.5
    ? [stops[0], stops[1], t / 0.5]
    : [stops[1], stops[2], (t - 0.5) / 0.5];
  const rgb = a.map((v, i) => Math.round(v + (b[i] - v) * local));
  return 'rgb(' + rgb.join(',') + ')';
}
function pmSeatAge(seat) {
  if (!pmState || !seat || !seat.started_at) return 0;
  return Math.max(0, (pmState.heartbeat_at || 0) - seat.started_at)
    + (Date.now() - pmFetchAt) / 1000;
}
function pmOccupied(pool) { return (pool.seats || []).filter(Boolean); }

function pmRenderBubbles() {
  const d = pmState, el = document.getElementById('pm-bubbles');
  if (!d) { el.innerHTML = ''; return; }
  if (!d.pools) {  // running:false 的显式空态（无状态文件 / 空上报）
    el.innerHTML = `<div class="pm-bubble bad ring-1 ring-slate-200"><span class="pm-dot"></span><span class="k">心跳</span><span class="v">已停</span></div>`
      + `<div class="pm-bubble ring-1 ring-slate-200"><span class="k">${esc(d.message || 'agent-worker 未在运行')}</span></div>`;
    return;
  }
  const llm = d.llm || {};
  const quotaWait = (llm.rate_limit_waiting || 0) + (llm.retry_waiting || 0);
  const failed = PM_POOLS.filter(p => !p.shared).reduce(
    (n, p) => n + ((d.pools[p.key] || {}).failed || 0), 0);
  const hbAge = Math.max(0, Date.now() / 1000 - (d.heartbeat_at || 0));
  let html = `<div class="pm-bubble ${d.running && d.ok !== false ? '' : 'bad'} ring-1 ring-slate-200">`
    + `<span class="pm-dot"></span><span class="k">心跳</span>`
    + `<span class="v">${d.running ? pmAgeText(hbAge) + '前' : '已停'}</span></div>`;
  html += `<div class="pm-bubble ring-1 ring-slate-200"><span class="k">模型请求</span><span class="v">${llm.inflight || 0}</span></div>`;
  if (quotaWait > 0) html += `<div class="pm-bubble warn ring-1 ring-slate-200"><span class="pm-dot"></span><span class="k">配额排队</span><span class="v">${quotaWait}</span></div>`;
  html += `<div class="pm-bubble ring-1 ring-slate-200"><span class="k">未归类原子</span><span class="v">${d.pending_atoms || 0}</span></div>`;
  if (failed > 0) html += `<div class="pm-bubble bad ring-1 ring-slate-200"><span class="pm-dot"></span><span class="k">异常</span><span class="v">${failed}</span></div>`;
  for (const p of PM_POOLS) {
    const pool = d.pools[p.key] || {};
    html += `<div class="pm-bubble ring-1 ring-slate-200"><span class="k">${p.title}</span><span class="v">${pmOccupied(pool).length}/${pool.workers || 0}</span></div>`;
  }
  el.innerHTML = html;
}

function pmTaskCard(task, poolKey, selKey) {
  const sel = selKey ? ' sel' : '';
  // 排队预览不可点（无席位、无日志）：不发 data-pm-open、用默认光标
  const queued = String(task._seat).startsWith('q');
  const open = queued ? '' : ` data-pm-open="${poolKey}:${task._seat}"`;
  const style = queued ? ' style="cursor:default"' : '';
  if (task.kind === 'skill') {
    const x = PM_XFER[task.xfer];
    const xferHtml = x
      ? (x.label
        ? `<div class="pm-xfer pm-xfer-${task.xfer}">${esc(x.label)}</div>`
        : `<div class="pm-xfer pm-xfer-${task.xfer}">${x.from} <span class="arrow">→</span> ${x.to}</div>`)
      : '';
    return `<div class="pm-card${sel}"${open}${style}>
      <div class="nm font-mono">${esc(task.skill_name)}</div>
      ${xferHtml}
      <div class="inf">${task.candidates != null ? `cand <b>${esc(task.candidates)}</b> · ` : ''}${task.weightscore != null ? `ws <b>${esc(task.weightscore)}</b> · ` : ''}${task._age != null ? pmAgeText(task._age) : '排队中'}</div>
    </div>`;
  }
  if (task.kind === 'atom_batch') {
    const ids = task.atom_ids || [];
    const preview = ids.slice(0, 4).map(a => `<code class="font-mono" style="font-size:10px;color:#0f766e">${esc(a)}</code>`).join(' ');
    return `<div class="pm-card${sel}"${open}${style}>
      <div class="nm">本批 ${ids.length} 个原子</div>
      <div class="inf">${task._age != null ? pmAgeText(task._age) : '排队中'}</div>
      <div class="mt-1 flex flex-wrap gap-1">${preview}${ids.length > 4 ? `<span class="inf">+${ids.length - 4}</span>` : ''}</div>
    </div>`;
  }
  if (task.kind === 'generate') {
    const preview = task.instruction || '';
    return `<div class="pm-card${sel}"${open}${style}>
      <div class="nm font-mono">${esc(task.user_id || task.job_id || '?')}</div>
      <div class="inf">${preview ? esc(preview) + ' · ' : ''}${task._age != null ? pmAgeText(task._age) : '排队中'}</div>
    </div>`;
  }
  // traj（拆分代理没有任务名，只有轨迹 id）
  return `<div class="pm-card${sel}"${open}${style}>
    <div class="nm font-mono">${esc(task.traj_id || '?')}</div>
    <div class="inf">${esc(task.watch_dir || '')}${task._age != null ? ' · ' + pmAgeText(task._age) : ' · 排队中'}</div>
  </div>`;
}

function pmRenderStages() {
  const wrap = document.getElementById('pm-stages');
  const d = pmState;
  if (!d || !d.pools) {
    wrap.innerHTML = `<div class="pm-stage ring-1 ring-slate-200" style="align-items:center;justify-content:center;min-height:160px">
      <span class="text-slate-400 text-xs">${esc((d && d.message) || 'agent-worker 未在运行')}</span></div>`;
    return;
  }
  wrap.innerHTML = PM_POOLS.map(def => {
    const pool = d.pools[def.key] || {};
    const seats = pool.seats || [];
    const queue = pool.queue || [];
    const chips = [];
    const admin = typeof IDENT !== 'undefined' && IDENT && IDENT.role === 'admin' && !def.shared;
    const seatOn = pmChipEdit && pmChipEdit.pool === def.key && pmChipEdit.field === 'workers' ? ' on' : '';
    const seatChip = admin
      ? `<button type="button" class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 hover:bg-slate-100${seatOn}" data-pm-pool="${def.key}" data-pm-field="workers" title="点击修改席位">席位 ${pool.workers ?? '—'}</button>`
      : `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600" title="${def.shared ? '与 SkillEdit 共用座位' : 'pools.' + def.key + '.workers'}">席位 ${pool.workers ?? '—'}</span>`;
    chips.push(seatChip);
    if (def.shared) {
      chips.push(`<span class="text-[10px] px-1.5 py-0.5 rounded bg-teal-50 text-teal-700" title="有 generate 在等时，大模型名额先给它">大模型优先</span>`);
    } else if (def.weight && pool.llm_weight != null) {
      const weightOn = pmChipEdit && pmChipEdit.pool === def.key && pmChipEdit.field === 'llm_weight' ? ' on' : '';
      const weightChip = admin
        ? `<button type="button" class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 hover:bg-slate-100${weightOn}" data-pm-pool="${def.key}" data-pm-field="llm_weight" title="点击修改配额比">配额比 ${esc(pool.llm_weight)}</button>`
        : `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600" title="pools.${def.key}.llm_weight">配额比 ${esc(pool.llm_weight)}</span>`;
      chips.push(weightChip);
    }
    if (def.batch && pool.batch_size != null) chips.push(`<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600" title="pools.${def.key}.batch_size（暂只读）">批量 ${esc(pool.batch_size)}</span>`);
    const seatHtml = seats.map((seat, i) => {
      if (!seat) return `<button type="button" class="pm-seat" title="席位 ${i + 1} · 空闲"></button>`;
      const task = { ...(seat.task || {}), _seat: i, _age: pmSeatAge(seat) };
      const sel = pmSelected && pmSelected.pool === def.key && pmSelected.seat === i;
      const color = pmSeatColor(task._age);
      return `<button type="button" class="pm-seat busy${sel ? ' sel' : ''}" data-pm-open="${def.key}:${i}"
        title="席位 ${i + 1} · 已跑 ${pmAgeText(task._age)}" style="background:${color};border-color:${color}"></button>`;
    }).join('');
    const activeCards = seats.map((seat, i) => seat && { ...(seat.task || {}), _seat: i, _age: pmSeatAge(seat) })
      .filter(Boolean).map(t => pmTaskCard(t, def.key,
        pmSelected && pmSelected.pool === def.key && pmSelected.seat === t._seat)).join('');
    const queueCards = queue.map((t, i) => pmTaskCard({ ...t, _seat: 'q' + i, _age: null }, def.key, false)).join('');
    const qlabel = def.key === 'cluster'
      ? `未归类 ${d.pending_atoms || 0} · 排队 ${pool.queued || queue.length}`
      : `排队 ${pool.queued || queue.length}`;
    return `<section class="pm-stage ring-1 ring-slate-200">
      <div class="flex items-baseline justify-between gap-1.5">
        <div><div class="font-semibold text-[13px]">${def.title}</div>
        <div class="text-[10.5px] text-slate-400">${def.subtitle}</div></div>
        <div class="flex flex-wrap gap-1 justify-end">${chips.join('')}</div>
      </div>
      <div class="pm-seats">${seatHtml}</div>
      <div class="text-[10.5px] text-slate-400 font-medium">进行中 ${pmOccupied(pool).length}</div>
      <div class="pm-lane">${activeCards || '<div class="text-[11px] text-slate-400">—</div>'}</div>
      <div class="text-[10.5px] text-slate-400 font-medium">${qlabel}</div>
      <div class="pm-lane">${queueCards || '<div class="text-[11px] text-slate-400">—</div>'}</div>
      <div class="pm-foot"><span>完成 ${pool.completed || 0}</span><span>失败 ${pool.failed || 0}</span></div>
    </section>`;
  }).join('');
}

function pmSelectedSeat() {
  if (!pmSelected || !pmState || !pmState.pools) return null;
  const pool = (pmState.pools[pmSelected.pool] || {});
  return (pool.seats || [])[pmSelected.seat] || null;
}

// 日志区是否贴底：仅贴底时跟随新行，用户上滚后不被轮询抢走位置
function pmLogNearBottom(el, pad) {
  if (!el) return true;
  pad = pad == null ? 48 : pad;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= pad;
}

function pmRenderDrawer() {
  const dr = document.getElementById('pm-drawer');
  const seat = pmSelectedSeat();
  if (!pmSelected || !seat) {
    pmStopLog();
    dr.className = 'pm-drawer empty ring-1 ring-slate-200';
    dr.innerHTML = pmSelected
      ? '该席位任务已结束 <button type="button" class="pm-btn ghost" data-pm-close>关闭</button>'
      : '点选进行中的任务看实时日志';
    if (pmSelected) pmSelected = null;
    return;
  }
  const task = seat.task || {};
  const age = pmAgeText(pmSeatAge(seat));
  const def = PM_POOLS.find(p => p.key === pmSelected.pool);
  // 抽屉随 live 轮询重渲染：同一任务的日志内容与滚动位置要保住，否则每 3s 闪回/跳底
  const hasStream = task.kind === 'skill' || task.kind === 'traj' || task.kind === 'generate';
  const existingEl = (hasStream && pmLogKey) ? document.getElementById('pm-log') : null;
  const existingLog = existingEl ? existingEl.innerHTML : '';
  const stickBottom = pmLogNearBottom(existingEl);
  const savedScroll = existingEl ? existingEl.scrollTop : 0;
  const logPane = `<div class="pm-log" id="pm-log">${existingLog || '<div class="dim">日志加载中…</div>'}</div>`;
  let head = '', logHtml = '';
  if (task.kind === 'skill') {
    const x = PM_XFER[task.xfer];
    const xferHead = x
      ? (x.label
        ? `<span class="pm-xfer pm-xfer-${task.xfer}" style="margin-top:0">${esc(x.label)}</span>`
        : `<span class="pm-xfer pm-xfer-${task.xfer}" style="margin-top:0">${x.from} <span class="arrow">→</span> ${x.to}</span>`)
      : '';
    head = `<h2 class="font-mono text-sm font-semibold break-all">${esc(task.skill_name)}</h2>
      <div class="text-[11px] text-slate-400 mt-0.5">${def.title} · 席位 ${pmSelected.seat + 1} · 已跑 ${age}</div>
      ${x ? `<div class="flex items-center gap-2 flex-wrap mt-1">${xferHead}<span class="text-xs text-slate-500">${x.tip}</span>
      <span class="text-[11px] text-slate-400">${task.candidates != null ? `cand ${esc(task.candidates)} · ` : ''}${task.weightscore != null ? `ws ${esc(task.weightscore)}` : ''}</span></div>` : ''}`;
    logHtml = logPane;
  } else if (task.kind === 'traj') {
    head = `<h2 class="font-mono text-sm font-semibold break-all">${esc(task.traj_id || '?')}</h2>
      <div class="text-[11px] text-slate-400 mt-0.5">${def.title} · ${esc(task.watch_dir || '')} · 席位 ${pmSelected.seat + 1} · 已跑 ${age}</div>`;
    logHtml = logPane;
  } else if (task.kind === 'generate') {
    head = `<h2 class="font-mono text-sm font-semibold break-all">${esc(task.user_id || task.job_id || '?')}</h2>
      <div class="text-[11px] text-slate-400 mt-0.5">${def.title} · 席位 ${pmSelected.seat + 1} · 已跑 ${age}</div>
      <div class="text-xs text-slate-600 mt-1">${esc(task.instruction || '')}</div>`;
    logHtml = logPane;
  } else {  // atom_batch：Cluster 批没有独立日志，展示本批原子名单（概念稿语义）
    const ids = (task.atom_ids || []).map(a => `<code class="font-mono text-[11px] text-teal-700">${esc(a)}</code>`).join(' · ');
    head = `<h2 class="text-sm font-semibold">本批 ${(task.atom_ids || []).length} 个原子</h2>
      <div class="text-[11px] text-slate-400 mt-0.5">${def.title} · 席位 ${pmSelected.seat + 1} · 已跑 ${age}</div>
      <div class="flex flex-wrap gap-1.5 mt-1">${ids || '<span class="text-[11px] text-slate-400">—</span>'}</div>`;
    logHtml = '<div class="pm-log" style="min-height:60px"><div class="dim">Cluster 批没有独立日志文件（逐轮 trace 在 split/edit 任务上）</div></div>';
  }
  dr.className = 'pm-drawer ring-1 ring-slate-200';
  dr.innerHTML = `<div class="flex items-start justify-between gap-2"><div class="min-w-0">${head}</div>
    <button type="button" class="pm-btn ghost shrink-0" data-pm-close>关闭</button></div>${logHtml}`;
  const newLog = document.getElementById('pm-log');
  if (newLog && existingLog) {
    newLog.scrollTop = stickBottom ? newLog.scrollHeight : savedScroll;
  }
  if (task.kind === 'skill' || task.kind === 'traj') pmStartLog(task.kind, task.kind === 'skill' ? task.skill_name : task.traj_id);
  else if (task.kind === 'generate') pmStartLog('generate', task.job_id);
  else pmStopLog();
}

function pmLogClassify(line) {
  if (/ERROR|失败|超时|Traceback|溢出|停滞/.test(line)) return 'err';
  if (/graduate|commit|split_done|clustered|完成|成功/.test(line)) return 'okl';
  if (/→|Candidates|staging|Jam|baby|main|TURN|ROUND/.test(line)) return 'hl';
  return '';
}
async function pmPollLog() {
  if (!pmLogKey) return;
  const { kind, name } = pmLogKey;
  try {
    const r = await j('api/v1/dashboard/pipeline/log?kind=' + encodeURIComponent(kind)
      + '&name=' + encodeURIComponent(name) + '&tail=300');
    const log = document.getElementById('pm-log');
    if (!log) return;
    if (!r.exists) { log.innerHTML = `<div class="dim">${esc(r.message || '该任务暂无日志文件')}</div>`; return; }
    const stickBottom = pmLogNearBottom(log);
    log.innerHTML = (r.lines || []).map(l => {
      const cls = pmLogClassify(l);
      return cls ? `<div class="${cls}">${esc(l)}</div>` : `<div>${esc(l)}</div>`;
    }).join('') || '<div class="dim">（日志为空）</div>';
    if (r.truncated) log.insertAdjacentHTML('afterbegin', '<div class="dim">…（仅显示日志尾部）</div>');
    if (stickBottom) log.scrollTop = log.scrollHeight;
  } catch (e) {
    const log = document.getElementById('pm-log');
    if (log) log.innerHTML = `<div class="err">日志读取失败：${esc(e.message)}</div>`;
  }
}
function pmStartLog(kind, name) {
  // pmLogKey 是 {kind,name}，不可与字符串 key 做 ===（否则每次抽屉重渲染都会重启轮询并强制贴底）
  if (pmLogKey && pmLogKey.kind === kind && pmLogKey.name === name) return;
  pmStopLog();
  pmLogKey = { kind, name };
  pmPollLog();
  pmLogTimer = setInterval(pmPollLog, PM_LOG_MS);
}
function pmStopLog() {
  pmLogKey = null;
  if (pmLogTimer) { clearInterval(pmLogTimer); pmLogTimer = null; }
}

const PM_CHIP_COPY = {
  workers: {
    title: '席位',
    hint: {
      split: '这一栏同时拆几条轨迹。占满后新轨迹先等。下一轮扫描生效，不用重启。',
      cluster: '这一栏同时归几批原子。占满后新批次先等。下一轮扫描生效，不用重启。',
      edit: 'SkillEdit 和 Generate 共用这些座位。占满后新任务先等。下一轮扫描生效，不用重启。',
    },
  },
  llm_weight: {
    title: '配额比',
    hint: {
      split: '拆分、归类、编辑共用大模型并发。数字越大越先拿到空闲名额。有 Generate 在等时仍先给它。',
      cluster: '拆分、归类、编辑共用大模型并发。数字越大越先拿到空闲名额。有 Generate 在等时仍先给它。',
      edit: '拆分、归类、编辑共用大模型并发。数字越大越先拿到空闲名额。有 Generate 在等时仍先给它。',
    },
  },
};
let pmChipEdit = null;  // {pool, field, saving}

function pmChipPopEl() { return document.getElementById('pm-chip-pop'); }
function pmChipInput() { return document.getElementById('pm-chip-pop-n'); }

function pmChipReadValue() {
  const raw = String((pmChipInput() || {}).value || '').trim();
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1) return null;
  return n;
}

function pmChipSetError(msg) {
  const el = document.getElementById('pm-chip-pop-err');
  if (el) el.textContent = msg || '';
}

function pmPlaceChipPop() {
  const pop = pmChipPopEl();
  if (!pop || pop.hidden || !pmChipEdit) return;
  const chip = document.querySelector(
    '[data-pm-pool="' + pmChipEdit.pool + '"][data-pm-field="' + pmChipEdit.field + '"]');
  if (!chip) return;
  const r = chip.getBoundingClientRect();
  const w = pop.offsetWidth || 248;
  const h = pop.offsetHeight || 160;
  let left = r.right - w;
  if (left < 12) left = 12;
  if (left + w > window.innerWidth - 12) left = Math.max(12, window.innerWidth - w - 12);
  let top = r.bottom + 8;
  if (top + h > window.innerHeight - 12) top = Math.max(12, r.top - h - 8);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}

function pmCloseChipPop() {
  if (pmChipEdit && pmChipEdit.saving) return;
  pmChipEdit = null;
  const pop = pmChipPopEl();
  if (pop) pop.hidden = true;
  const chipOn = document.querySelector('[data-pm-field].on');
  if (chipOn) chipOn.classList.remove('on');
}

function pmOpenChipPop(pool, field) {
  if (!IDENT || IDENT.role !== 'admin') return;
  if (pool === 'generate') return;
  const pop = pmChipPopEl();
  const input = pmChipInput();
  if (!pop || !input) return;
  if (pmChipEdit && pmChipEdit.pool === pool && pmChipEdit.field === field && !pop.hidden) {
    pmCloseChipPop();
    return;
  }
  const live = (pmState && pmState.pools && pmState.pools[pool]) || {};
  const current = field === 'workers' ? live.workers : live.llm_weight;
  const copy = PM_CHIP_COPY[field] || PM_CHIP_COPY.workers;
  pmChipEdit = { pool, field, saving: false };
  document.getElementById('pm-chip-pop-title').textContent = copy.title;
  document.getElementById('pm-chip-pop-hint').textContent =
    (copy.hint && copy.hint[pool]) || '';
  pmChipSetError('');
  const saveBtn = pop.querySelector('[data-pm-chip-save]');
  if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = '保存'; }
  input.disabled = false;
  input.value = current == null ? '' : String(current);
  pop.hidden = false;
  pmRenderStages();
  pmPlaceChipPop();
  requestAnimationFrame(() => { input.focus(); input.select(); pmPlaceChipPop(); });
}

function pmChipNudge(delta) {
  if (!pmChipEdit || pmChipEdit.saving) return;
  const input = pmChipInput();
  const cur = pmChipReadValue() || 1;
  input.value = String(Math.max(1, cur + delta));
  pmChipSetError('');
}

async function pmSaveChipPop() {
  if (!pmChipEdit || pmChipEdit.saving) return;
  const n = pmChipReadValue();
  if (n == null) {
    pmChipSetError('请填写大于 0 的整数');
    const input = pmChipInput();
    if (input) input.focus();
    return;
  }
  const { pool, field } = pmChipEdit;
  const live = (pmState && pmState.pools && pmState.pools[pool]) || {};
  const current = field === 'workers' ? live.workers : live.llm_weight;
  if (n === Number(current)) { pmCloseChipPop(); pmRenderStages(); return; }
  pmChipEdit.saving = true;
  pmChipSetError('');
  const pop = pmChipPopEl();
  const input = pmChipInput();
  const saveBtn = pop && pop.querySelector('[data-pm-chip-save]');
  if (input) input.disabled = true;
  pop.querySelectorAll('button').forEach(b => { b.disabled = true; });
  if (saveBtn) saveBtn.textContent = '保存中…';
  try {
    const r = await fetch('api/v1/dashboard/admin/pipeline/pools', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pool, [field]: n }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = body.detail;
      throw new Error(typeof detail === 'string' ? detail : (r.status + ''));
    }
    pmChipEdit.saving = false;
    pmCloseChipPop();
    await pmFetchLive();
  } catch (err) {
    pmChipEdit.saving = false;
    if (input) input.disabled = false;
    pop.querySelectorAll('button').forEach(b => { b.disabled = false; });
    if (saveBtn) saveBtn.textContent = '保存';
    pmChipSetError('没保存上：' + (err && err.message ? err.message : err));
  }
}

async function pmFetchLive() {
  try {
    pmState = await j('api/v1/dashboard/pipeline/live');  // 不走 jc：每轮都要新数据
    pmFetchAt = Date.now();
  } catch (e) {
    pmState = { running: false, message: '流水线状态读取失败：' + e.message };
  }
  pmRenderBubbles();
  if (pmChipEdit) {
    pmPlaceChipPop();
    return;
  }
  pmRenderStages();
  pmRenderDrawer();
  pmPlaceChipPop();
}
function pmSetActive(on) {
  if (on) {
    if (pmPollTimer) return;
    pmFetchLive();
    pmPollTimer = setInterval(pmFetchLive, PM_POLL_MS);
  } else {
    if (pmPollTimer) { clearInterval(pmPollTimer); pmPollTimer = null; }
    pmStopLog();
    pmCloseChipPop();
  }
}
// 席位/任务点选 + 抽屉关闭（事件委托，重渲染不丢）
document.addEventListener('click', e => {
  if (e.target.closest('[data-pm-chip-save]')) {
    e.preventDefault();
    pmSaveChipPop();
    return;
  }
  if (e.target.closest('[data-pm-chip-cancel]')) {
    e.preventDefault();
    if (pmChipEdit) pmChipEdit.saving = false;
    pmCloseChipPop();
    pmRenderStages();
    return;
  }
  const step = e.target.closest('[data-pm-step]');
  if (step) {
    e.preventDefault();
    pmChipNudge(Number(step.dataset.pmStep) || 0);
    return;
  }
  if (e.target.closest('#pm-chip-pop')) return;
  const chip = e.target.closest('[data-pm-field]');
  if (chip) {
    e.preventDefault();
    e.stopPropagation();
    pmOpenChipPop(chip.dataset.pmPool, chip.dataset.pmField);
    return;
  }
  if (pmChipEdit && !pmChipEdit.saving) {
    pmCloseChipPop();
    pmRenderStages();
  }
  const open = e.target.closest('[data-pm-open]');
  if (open) {
    const [pool, seat] = open.dataset.pmOpen.split(':');
    if (seat.startsWith('q')) return;   // 排队预览不可点（无日志/详情）
    const key = { pool, seat: Number(seat) };
    const same = pmSelected && pmSelected.pool === key.pool && pmSelected.seat === key.seat;
    pmSelected = same ? null : key;
    pmStopLog();
    pmRenderStages();
    pmRenderDrawer();
    return;
  }
  if (e.target.closest('[data-pm-close]')) {
    pmSelected = null;
    pmStopLog();
    pmRenderStages();
    pmRenderDrawer();
  }
});
document.addEventListener('keydown', e => {
  if (!pmChipEdit) return;
  if (e.key === 'Escape') {
    if (pmChipEdit.saving) return;
    pmCloseChipPop();
    pmRenderStages();
  } else if (e.key === 'Enter' && e.target && e.target.id === 'pm-chip-pop-n') {
    e.preventDefault();
    pmSaveChipPop();
  }
});
window.addEventListener('resize', pmPlaceChipPop);

// ── SPA-lite 路由（hash）─────────────────────────────────────────
const NAMES = { overview: '总览', skills: '技能库', pipeline: '流水线', traj: '轨迹 & 原子', users: '用户 & 画像', canary: '灰度 Canary', my: '我的', admin: '管理', settings: '设置' };
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
  pmSetActive(pg === 'pipeline');   // 只在流水线页轮询，离开即停
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
  // #174：仅「无 hash」时普通用户默认进「我的」；显式 #overview / 点总览应可打开，不得再劫持
  let pg = parts[0] || '';
  if (!pg) {
    pg = (IDENT && IDENT.role !== 'admin') ? 'my' : 'overview';
  }
  showPage(pg);
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
  const libStar = e.target.closest('.lib-star');
  if (libStar) {
    const name = libStar.dataset.skill;
    const act = libStar.dataset.act;
    if (!name || (act !== 'pin' && act !== 'clear')) return;
    try {
      await jpost('/api/v1/dashboard/my/prefs', { skill_name: name, action: act });
      await loadSkills();
      if (IDENT) loadMy().catch(console.error);
    } catch (err) { alert(err.message); }
    return;
  }
  const row = e.target.closest('[data-skill-row]');
  if (row) { _skillBackHash = null; location.hash = 'skill/' + encodeURIComponent(row.dataset.skillRow); return; }
  const sj = e.target.closest('.skill-jump');
  if (sj) { _skillBackHash = null; location.hash = 'skill/' + encodeURIComponent(sj.dataset.skill); return; }
  const aj = e.target.closest('[data-atom-jump]');
  if (aj) { location.hash = 'traj/' + aj.dataset.atomJump; return; }
  const step = e.target.closest('.atom-step');
  if (step && _curTraj) { openAtom(_curTraj, step.dataset.atom).catch(console.error); return; }
  const gn = e.target.closest('[data-gnode]');
  if (gn && _curSkill) {
    const side = gn.dataset.gside;
    if (side === 'staging' || side === 'main') {
      focusSkillRouting(side);
    }
    const pv = document.getElementById('skill-preview');
    if (pv) {
      pv.innerHTML = '<span class="text-slate-400 text-xs">加载 diff…</span>';
      try {
        const r = await j('api/v1/dashboard/skill/' + encodeURIComponent(_curSkill) + '/diff?sha=' + encodeURIComponent(gn.dataset.gnode));
        pv.innerHTML = renderDiff(r.diff);
      } catch (err) { pv.innerHTML = `<span class="text-rose-600 text-xs">${esc(err.message)}</span>`; }
      if (!(side === 'staging' || side === 'main')) {
        pv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
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
  const scriptBtn = e.target.closest('[data-skill-scripting]');
  if (scriptBtn && !scriptBtn.disabled) {
    scriptBtn.disabled = true;
    scriptBtn.textContent = '已排队…';
    jpost('api/v1/dashboard/skill/' + encodeURIComponent(scriptBtn.dataset.skillScripting) + '/scripting', {})
      .then(() => { scriptBtn.textContent = '脚本化进行中'; })
      .catch(err => {
        scriptBtn.disabled = false;
        scriptBtn.textContent = '脚本化（实验性）';
        scriptBtn.title = String(err && err.message ? err.message : err);
      });
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

// ═════════════ P2:登录/角色 + 我的/管理/设置 ═════════════
// IDENT 必须在首次 route() 之前完成初始化：route() 会读它决定默认落地页
// （普通用户 → 我的）。若放在 route() 之后，开机即 ReferenceError，后续
// load* 与 initIdent 全部中断，表现为未登录空骨架，仅流水线页仍可用。
let IDENT = null;   // {user, role} | null

// ── 启动：各端点独立加载，单个失败不拖垮整页 ───────────────────
route();
for (const f of [loadOverview, loadRates, loadPipeline, loadDomain, loadCost,
  loadSkills, loadDirs, loadUsersStatus, loadTags, loadCanary]) {
  f().catch(e => console.error(e));
}

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
  document.getElementById('settings-guard').classList.toggle('hidden', admin);
  document.getElementById('settings-body').classList.toggle('hidden', !admin);
}

async function initIdent() {
  try { IDENT = await j('/api/v1/dashboard/me'); } catch { IDENT = null; }
  applyIdent();
  if (IDENT) { loadMy().catch(console.error); initEvents(); }
  if (IDENT && IDENT.role === 'admin') { loadAdmin().catch(console.error); loadSettings().catch(console.error); }
  // 登录后若仍无落地 hash，普通用户默认进「我的」（不抢显式 #overview）
  if (IDENT && IDENT.role !== 'admin') {
    const h = (location.hash || '').replace(/^#/, '');
    if (!h) location.hash = '#my';
  }
  loadSkills().catch(console.error);
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
    loadSkills().catch(console.error);
    initEvents();
    if (IDENT.role === 'admin') { loadAdmin().catch(console.error); loadSettings().catch(console.error); }
    else location.hash = '#my';
  } catch (e) { err.textContent = e.message; }
});
document.getElementById('login-secret').addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('login-submit').click(); });
document.getElementById('btn-logout').addEventListener('click', async () => {
  await jpost('/api/v1/dashboard/logout').catch(() => {});
  IDENT = null; applyIdent(); location.hash = '#overview';
  loadSkills().catch(console.error);
});

// ── 我的 ────────────────────────────────────────────────────────
const BUCKET_CHIP = {
  pinned: 'bg-violet-100 text-violet-700',
  ranked: 'bg-teal-50 text-teal-700 ring-1 ring-teal-100',
  recommended: 'bg-sky-100 text-sky-700',
};
const TRAJ_PAGE = 5;
const RT_PAGE = 10;
const FEED_PAGE = 5;
let _rtRows = [];
let _rtPage = 0;
let _rtOpen = false;
let _contribOpen = true;
let _trajPage = 0;
let _contribTraj = null;
let _contribPayload = null; // {total, trajs, skill_meta}
let _contribPayloadOffset = null; // 与 _contribPayload 对应的 offset；同页切换不重请求
let _feedPages = []; // cached pages of events
let _feedPage = 0;
let _feedBefore = null;
let _feedExhausted = false;

function bucketLabel(s) {
  if (s.bucket !== 'pinned') return s.bucket;
  return s.pin_scope === 'global' ? 'pinned·全局' : (s.user_removable ? 'pinned·自己' : 'pinned·admin');
}
function mySourceBadge(s, withDetail) {
  const src = s.source || 'native';
  let b = `<span class="src-badge src-native">自产</span>`;
  if (src === 'skillhub') b = `<span class="src-badge src-hub">第三方</span>`;
  else if (src === 'upload') b = `<span class="src-badge src-upload">上传</span>`;
  if (src === 'native' && s.producer) {
    b += ` <span class="text-[10px] text-slate-500">主要贡献人 <b class="font-medium text-slate-700">${esc(s.producer)}</b></span>`;
    if (withDetail && s.producer_trajs != null) b += ` <span class="src-path">${s.producer_trajs} 条轨迹</span>`;
  } else if (withDetail) {
    if (s.source_path) b += ` <span class="src-path">${esc(s.source_path)}</span>`;
  }
  return b;
}
function sparkline(vals, w, h) {
  vals = vals && vals.length ? vals : [0];
  w = w || 48; h = h || 18;
  const pad = 1;
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const pts = vals.map((v, i) => {
    const x = pad + i * (w - 2 * pad) / Math.max(1, vals.length - 1);
    const y = h - pad - ((v - min) / span) * (h - 2 * pad);
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  const up = vals[vals.length - 1] >= vals[0];
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline fill="none" stroke="${up ? '#0d9488' : '#e11d48'}" stroke-width="1.5" points="${pts}"/></svg>`;
}

function contribRelationGraph(trajId, atoms) {
  if (!atoms.length) return { svg: '<span class="text-slate-400 text-xs">暂无原子</span>', skills: [], H: 40, top: 18, rowH: 78, chipH: 68, atomN: 0, skillN: 0 };
  const skills = [];
  atoms.forEach(a => (a.destinations || []).forEach(d => {
    if (d.skill && !skills.includes(d.skill)) skills.push(d.skill);
  }));
  const rowH = 78, chipH = 68, top = 18;
  const H = Math.max(atoms.length, skills.length || 1) * rowH + 12;
  const ay = i => top + i * rowH + (Math.max(0, skills.length - atoms.length) * rowH) / 2;
  const sy = i => top + i * rowH + (Math.max(0, atoms.length - skills.length) * rowH) / 2;
  const midY = 8 + (H - 16) / 2;
  const W = 300;
  const edges = atoms.map((a, i) =>
    `<path d="M90 ${midY} C 132 ${midY} 132 ${ay(i)} 158 ${ay(i)}" fill="none" stroke="#cbd5e1" stroke-width="1.5"/>`).join('')
    + atoms.flatMap((a, i) => (a.destinations || []).map(d => {
      const si = skills.indexOf(d.skill);
      return `<path d="M176 ${ay(i)} C 228 ${ay(i)} 228 ${sy(si)} 280 ${sy(si)}" fill="none" stroke="#14b8a6" stroke-width="2"/>
        <text x="226" y="${(ay(i) + sy(si)) / 2 - 4}" font-size="9" fill="#94a3b8" text-anchor="middle">${d.weightscore != null ? 'ws ' + esc(d.weightscore) : ''}</text>`;
    })).join('');
  const atomNodes = atoms.map((a, i) => {
    const hit = (a.destinations || []).length;
    return `<g>
      <circle cx="167" cy="${ay(i)}" r="${hit ? 10 : 8}" fill="${hit ? '#0d9488' : '#e2e8f0'}"/>
      <text x="167" y="${ay(i) + 3.2}" font-size="9.5" font-family="ui-monospace,monospace" text-anchor="middle" fill="${hit ? '#fff' : '#64748b'}">${esc(String(a.atom_id || '').slice(-2))}</text>
      <title>${esc(a.atom_id)}</title>
    </g>`;
  }).join('');
  const skillAnchors = skills.map((sk, i) =>
    `<circle cx="288" cy="${sy(i)}" r="4.5" fill="#0d9488"/>`).join('');
  const label = trajId.length > 12 ? trajId.slice(0, 11) + '…' : trajId;
  const svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="display:block">
      <rect x="8" y="${midY - 13}" width="80" height="26" rx="7" fill="#134e4a"/>
      <text x="48" y="${midY + 3.5}" font-size="9.5" fill="#fff" text-anchor="middle" font-family="ui-monospace,monospace">${esc(label)}</text>
      ${edges}${atomNodes}${skillAnchors}
    </svg>`;
  return { svg, skills, H, top, rowH, chipH, atomN: atoms.length, skillN: skills.length };
}
function contribSkillChips(skills, layout, metaMap) {
  if (!skills.length) return '<span class="text-[11px] text-slate-400 px-1">暂无关联 skill</span>';
  const top = layout.top + (Math.max(0, layout.atomN - layout.skillN) * layout.rowH) / 2;
  const chipH = layout.chipH || 68;
  return `<div style="position:relative;height:${layout.H}px;width:268px">${skills.map((sk, i) => {
    const m = metaMap[sk] || {};
    const y = top + i * layout.rowH - chipH / 2;
    const recent = (m.recent || []).slice(0, 2).map(u => esc(u.user)).join(' · ');
    const topU = m.top || {};
    return `<button type="button" class="skill-chip graph-skill" data-skill="${esc(sk)}" style="position:absolute;left:0;top:${y}px">
      <div class="min-w-0 flex-1">
        <div class="nm">${esc(sk)}</div>
        <div class="meta">${mySourceBadge(m)}<span class="text-[10px] text-slate-500">ux <b class="tabular-nums text-slate-700">${m.ux != null ? esc(m.ux) : '—'}</b></span></div>
        <div class="text-[10px] text-slate-400 truncate leading-tight">最近 ${recent || '—'} · 最多 ${esc(topU.user || '—')}×${topU.count != null ? topU.count : 0}</div>
      </div>
      <div class="shrink-0 opacity-90">${sparkline(m.trend || [0])}</div>
    </button>`;
  }).join('')}</div>`;
}

async function renderContribDetail() {
  const listEl = document.getElementById('contrib-traj-list');
  const graphEl = document.getElementById('contrib-graph');
  const skillsEl = document.getElementById('contrib-skills');
  if (!listEl || !_contribOpen) return;
  const offset = _trajPage * TRAJ_PAGE;
  let d;
  if (_contribPayload && _contribPayloadOffset === offset) {
    d = _contribPayload;
  } else {
    try {
      d = await j(`/api/v1/dashboard/my/contributions/trajs?offset=${offset}&limit=${TRAJ_PAGE}`);
    } catch (e) {
      listEl.innerHTML = `<span class="text-[11px] text-rose-600">${esc(e.message)}</span>`;
      return;
    }
    _contribPayload = d;
    _contribPayloadOffset = offset;
  }
  const total = d.total || 0;
  const pages = Math.max(1, Math.ceil(total / TRAJ_PAGE));
  if (_trajPage >= pages) _trajPage = pages - 1;
  const trajs = d.trajs || [];
  if (!trajs.find(t => t.traj_id === _contribTraj) && trajs[0]) _contribTraj = trajs[0].traj_id;
  document.getElementById('traj-page-sum').textContent = `${total} 条`;
  document.getElementById('traj-page-label').textContent = `${_trajPage + 1}/${pages}`;
  document.getElementById('traj-up').disabled = _trajPage === 0;
  document.getElementById('traj-down').disabled = _trajPage >= pages - 1;
  listEl.innerHTML = trajs.map(t => {
    const adopted = t.atoms.filter(a => (a.destinations || []).length).length;
    const on = t.traj_id === _contribTraj;
    return `<a href="javascript:void(0)" class="contrib-traj ${on ? 'on' : ''}" data-traj="${esc(t.traj_id)}">
      <code class="text-[11px]">${esc(t.traj_id)}</code>
      <span class="text-slate-400 ml-1">${t.atoms.length} 原子 · ${adopted} 采纳</span>
    </a>`;
  }).join('') || '<span class="text-[11px] text-slate-400">暂无轨迹</span>';
  const cur = trajs.find(t => t.traj_id === _contribTraj) || trajs[0];
  if (!cur) {
    graphEl.innerHTML = '';
    skillsEl.innerHTML = '';
    return;
  }
  const g = contribRelationGraph(cur.traj_id, cur.atoms || []);
  graphEl.innerHTML = g.svg;
  skillsEl.innerHTML = contribSkillChips(g.skills, g, d.skill_meta || {});
}
let _myUploads = [];
let _myUploadSelected = null;

function renderMyUploadsList() {
  const box = document.getElementById('my-uploads-list');
  const sum = document.getElementById('my-uploads-sum');
  if (sum) sum.textContent = `${_myUploads.length} 个`;
  if (!box) return;
  if (!_myUploads.length) {
    box.innerHTML = '<div class="text-[11px] text-slate-400 py-2">还没有上传过 skill</div>';
    return;
  }
  box.innerHTML = _myUploads.map(s => {
    const on = _myUploadSelected === s.name;
    return `<div class="flex items-stretch gap-1 rounded-lg ring-1 ${on ? 'ring-teal-200 bg-teal-50' : 'ring-slate-100'}">
      <button type="button" class="skill-jump min-w-0 flex-1 text-left px-2.5 py-2 rounded-lg hover:bg-white/60" data-skill="${esc(s.name)}" title="打开技能详情">
        <div class="text-[12.5px] font-medium text-teal-700 truncate">${esc(s.name)}</div>
        <div class="text-[10.5px] text-slate-400 mt-0.5 flex items-center gap-1.5 flex-wrap">
          <span class="src-badge src-upload">上传</span>
          <span>${s.uses_30d ?? 0} 次 · ${s.users_30d ?? 0} 人</span>
          <span>ux <b class="text-slate-600 tabular-nums">${s.avg_ux != null ? esc(s.avg_ux) : '—'}</b></span>
        </div>
      </button>
      <button type="button" class="my-upload-usage shrink-0 px-2 text-[10.5px] text-slate-500 hover:text-teal-700 hover:bg-white/70 rounded-r-lg" data-skill="${esc(s.name)}" title="查看使用情况">使用</button>
    </div>`;
  }).join('');
}

async function loadMyUploadUsage(name) {
  const box = document.getElementById('my-uploads-usage');
  if (!box) return;
  if (!name) {
    box.innerHTML = '<div class="text-[11px] text-slate-400">点左侧旁的「使用」查看谁在用、评分原子</div>';
    return;
  }
  _myUploadSelected = name;
  renderMyUploadsList();
  box.innerHTML = `<div class="text-[11px] text-slate-400">加载 ${esc(name)} 使用情况…</div>`;
  try {
    const d = await j('/api/v1/dashboard/my/uploads/' + encodeURIComponent(name) + '/usage');
    if (_myUploadSelected !== name) return;
    const s = d.summary || {};
    const rowsHtml = (d.recent || []).map(u => {
      const atoms = (u.atoms || []).map(a => {
        const jump = `${esc(a.traj_id)}/${esc(a.atom_id)}`;
        return `<button type="button" class="atom-score" data-atom-jump="${jump}" title="${esc(a.intent || a.atom_id)}">
          <span class="font-mono font-normal text-[10px] opacity-70">${esc(String(a.atom_id || '').slice(-4))}</span>
          <span>${a.score != null ? esc(a.score) : '—'}</span>
        </button>`;
      }).join(' ') || '<span class="text-[10.5px] text-slate-300">无原子明细</span>';
      return `<div class="py-2 border-b border-slate-100 last:border-0">
        <div class="flex items-center justify-between gap-2">
          <span class="flex items-center gap-2 min-w-0">${avatar(u.user, 'sm')}<span class="font-medium truncate">${esc(u.user)}</span></span>
          <span class="text-[11px] text-slate-400 shrink-0 tabular-nums">${u.uses} 次 · 均 ${u.avg_ux != null ? esc(u.avg_ux) : '—'}</span>
        </div>
        <div class="mt-1.5 flex flex-wrap gap-1.5">${atoms}</div>
        <div class="mt-1 text-[10.5px] text-slate-400">${fdate(u.last_used).slice(0, 10)}</div>
      </div>`;
    }).join('') || '<div class="py-3 text-slate-400 text-[11px]">近 30 天暂无使用</div>';
    box.innerHTML = `
      <div class="flex items-baseline justify-between flex-wrap gap-2">
        <div>
          <button type="button" class="skill-jump text-[13px] font-semibold text-teal-700 underline decoration-teal-200 underline-offset-2" data-skill="${esc(name)}">${esc(name)}</button>
          <div class="text-[11px] text-slate-400 mt-0.5">近 ${s.days || 30} 天 · 点评分徽章进原子页</div>
        </div>
        <div class="flex gap-3 text-[11px] text-slate-500">
          <span>使用 <b class="text-slate-800 tabular-nums">${s.uses ?? 0}</b></span>
          <span>用户 <b class="text-slate-800 tabular-nums">${s.users ?? 0}</b></span>
          <span>均分 <b class="text-slate-800 tabular-nums">${s.avg_ux != null ? esc(s.avg_ux) : '—'}</b></span>
        </div>
      </div>
      <div class="mt-2 max-h-56 overflow-y-auto">${rowsHtml}</div>`;
  } catch (err) {
    box.innerHTML = `<div class="text-[11px] text-rose-600">${esc(err.message)}</div>`;
  }
}

async function loadMyUploads() {
  try {
    const d = await j('/api/v1/dashboard/my/uploads');
    _myUploads = d.skills || [];
  } catch {
    _myUploads = [];
  }
  if (_myUploadSelected && !_myUploads.some(s => s.name === _myUploadSelected)) {
    _myUploadSelected = null;
  }
  if (!_myUploadSelected && _myUploads[0]) _myUploadSelected = _myUploads[0].name;
  renderMyUploadsList();
  await loadMyUploadUsage(_myUploadSelected);
}

function commitStatusPill(c) {
  const st = c.status || 'live';
  let label = c.status_label;
  if (!label) {
    if (st === 'canary') label = '灰测中';
    else if (st === 'absorbed') {
      const into = (c.absorbed_into && c.absorbed_into.label) || ('main@' + String(c.sha || '').slice(0, 7));
      label = '被吸收到 ' + into;
    } else label = '已上线';
  }
  const cls = st === 'canary' ? 'canary' : (st === 'absorbed' ? 'absorbed' : 'live');
  return `<span class="commit-pill ${cls}">${esc(label)}</span>`;
}

async function loadMyCommits() {
  const box = document.getElementById('my-commits-list');
  const sum = document.getElementById('my-commits-sum');
  if (!box) return;
  box.innerHTML = '<div class="text-[11px] text-slate-400">加载中…</div>';
  try {
    const d = await j('/api/v1/dashboard/my/commits');
    const list = d.commits || [];
    if (sum) sum.textContent = `${list.length} 条`;
    if (!list.length) {
      box.innerHTML = '<div class="text-[11px] text-slate-400 py-2">还没有线上提交的 skill commit</div>';
      return;
    }
    box.innerHTML = list.map(c => `
      <div class="commit-row">
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2 flex-wrap">
            <button type="button" class="skill-jump font-medium text-teal-700 underline decoration-teal-200 underline-offset-2" data-skill="${esc(c.skill)}">${esc(c.skill)}</button>
            ${commitStatusPill(c)}
            <button type="button" class="skill-jump font-mono text-[11px] text-slate-500 hover:text-teal-700" data-skill="${esc(c.skill)}" title="打开技能详情">${esc(String(c.sha || '').slice(0, 7))}</button>
          </div>
          <div class="mt-1 text-[12.5px] text-slate-700 truncate" title="${esc(c.subject)}">${esc(c.subject || '—')}</div>
          <div class="mt-1 text-[10.5px] text-slate-400">${fdate(c.ts)} · 本地改 → 线上提交</div>
        </div>
      </div>`).join('');
  } catch (err) {
    if (sum) sum.textContent = '—';
    box.innerHTML = `<div class="text-[11px] text-rose-600">${esc(err.message)}</div>`;
  }
}

function setContribOpen(on) {
  _contribOpen = on;
  const detail = document.getElementById('contrib-detail');
  const btn = document.getElementById('contrib-toggle');
  if (detail) detail.classList.toggle('hidden', !on);
  if (btn) btn.textContent = on ? '收起' : '展开';
  if (on) {
    renderContribDetail().catch(console.error);
    loadMyUploads().catch(console.error);
    loadMyCommits().catch(console.error);
  }
}

function renderRT() {
  const total = _rtRows.length;
  const pages = Math.max(1, Math.ceil(total / RT_PAGE));
  if (_rtPage >= pages) _rtPage = pages - 1;
  const start = _rtPage * RT_PAGE;
  const slice = _rtRows.slice(start, start + RT_PAGE);
  const VC = { '高价值': 'bg-emerald-100 text-emerald-700', '正常': 'bg-slate-100 text-slate-600' };
  const sum = document.getElementById('rt-sum');
  if (!_rtOpen) {
    if (sum) sum.textContent = `${total} 条`;
    return;
  }
  rows('my-rt-body', slice.map(r => `<tr>
    <td class="py-2"><span class="skill-jump cursor-pointer text-teal-700" data-skill="${esc(r.skill)}">${esc(r.skill)}</span> ${mySourceBadge(r)}</td>
    <td class="text-right tabular-nums">${r.exposures}</td><td class="text-right tabular-nums">${r.triggers}</td>
    <td class="text-right tabular-nums">${pctf(r.rate)}</td>
    <td class="pl-6"><span class="text-[10px] px-1.5 py-0.5 rounded ${VC[r.verdict] || 'bg-rose-100 text-rose-700'}">${esc(r.verdict)}</span></td></tr>`).join(''),
    '暂无推荐记录');
  if (sum) sum.textContent = total ? `${start + 1}–${start + slice.length} / ${total}` : '0 条';
  const lab = document.getElementById('rt-page-label');
  if (lab) lab.textContent = `${_rtPage + 1} / ${pages}`;
  const prev = document.getElementById('rt-prev');
  const next = document.getElementById('rt-next');
  if (prev) prev.disabled = _rtPage === 0;
  if (next) next.disabled = _rtPage >= pages - 1;
}
function setRtOpen(on) {
  _rtOpen = on;
  const body = document.getElementById('rt-body');
  const btn = document.getElementById('rt-toggle');
  if (body) body.classList.toggle('hidden', !on);
  if (btn) btn.textContent = on ? '收起' : '展开';
  renderRT();
}

let _mySlotsAll = [];
let _mySlotsQ = '';
let _mySlotsQTimer = null;
let _myBlocked = [];
let _myTotalSlots = 0;
let _myServerPushed = 0;
let _myServerQueue = [];
let _myServerPushDefault = 100;
let _myPrefSaving = false;
let _myPrefSaveTimer = null;

function _myStarSvg(filled) {
  return filled
    ? `<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 1.2l1.76 3.56 3.93.57-2.84 2.77.67 3.91L8 10.16l-3.52 1.85.67-3.91L2.3 5.33l3.93-.57L8 1.2z"/></svg>`
    : `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M8 1.2l1.76 3.56 3.93.57-2.84 2.77.67 3.91L8 10.16l-3.52 1.85.67-3.91L2.3 5.33l3.93-.57L8 1.2z"/></svg>`;
}

function _mySlotRowHtml(s) {
  const curSide = s.side || 'main';
  const pinned = s.bucket === 'pinned';
  const locked = pinned && !s.user_removable;
  const mutable = !!s.side_mutable;
  let sideCtl;
  if (mutable && !locked) {
    const sideBtn = (side, label) => {
      const on = curSide === side;
      return `<button type="button" class="my-row-side px-1.5 py-0.5 ${on ? (side === 'staging' ? 'bg-amber-400 text-white' : 'bg-teal-600 text-white') : 'bg-white text-slate-500 hover:bg-slate-50'}"
        data-skill="${esc(s.skill_name)}" data-side="${side}" title="pin 到 ${label}">${label}</button>`;
    };
    sideCtl = `<div class="inline-flex rounded-md ring-1 ring-slate-200 overflow-hidden text-[10px]">${sideBtn('main', 'main')}${sideBtn('staging', 'staging')}</div>`;
  } else {
    sideCtl = `<span class="text-[10px] px-1.5 py-0.5 rounded-md ${curSide === 'staging' ? 'bg-amber-50 text-amber-700 ring-1 ring-amber-200' : 'bg-slate-100 text-slate-500'}">${esc(curSide)}</span>`;
  }
  let starBtn;
  if (locked) {
    starBtn = `<span class="inline-flex items-center justify-center w-7 h-7 text-amber-400 opacity-70" title="admin/全局 pin，不可取消">${_myStarSvg(true)}</span>`;
  } else if (pinned) {
    starBtn = `<button type="button" class="my-row-unpin inline-flex items-center justify-center w-7 h-7 rounded-md text-amber-400 hover:text-amber-500 hover:bg-slate-50"
      data-skill="${esc(s.skill_name)}" title="已 pin · 点击取消" aria-label="取消 pin">${_myStarSvg(true)}</button>`;
  } else {
    starBtn = `<button type="button" class="my-row-pin inline-flex items-center justify-center w-7 h-7 rounded-md text-slate-300 hover:text-amber-300 hover:bg-slate-50"
      data-skill="${esc(s.skill_name)}" data-side="${esc(curSide)}" title="未 pin · 点击 pin 到当前 side" aria-label="pin">${_myStarSvg(false)}</button>`;
  }
  const rank = s.rank != null
    ? `<span class="text-[10px] text-slate-400 tabular-nums shrink-0">#${s.rank}</span>`
    : '';
  return `<div class="flex items-center gap-2.5 px-3 py-2.5 rounded-xl ring-1 ring-slate-100 hover:bg-slate-50">
    ${rank}
    <div class="min-w-0 flex-1">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="skill-jump cursor-pointer font-medium text-teal-700 underline decoration-teal-200 underline-offset-2" data-skill="${esc(s.skill_name)}">${esc(s.skill_name)}</span>
        <span class="text-[10px] px-1.5 py-0.5 rounded ${BUCKET_CHIP[s.bucket] || 'bg-slate-100 text-slate-500'}">${bucketLabel(s)}</span>
        ${s.sha ? `<code class="text-[10px] text-slate-400">${esc(String(s.sha).slice(0, 7))}</code>` : ''}
      </div>
      <div class="mt-1 flex items-center gap-1.5 flex-wrap">${mySourceBadge(s, true)}</div>
    </div>
    <span class="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-teal-50 text-teal-700 tabular-nums">我触发 ${s.my_triggers ?? 0} 次</span>
    <div class="shrink-0 flex items-center gap-1.5">
      ${sideCtl}
      ${starBtn}
      ${!pinned ? `<button type="button" class="my-pref text-[11px] px-1.5 py-0.5 rounded ring-1 ring-slate-200 hover:bg-slate-50 text-rose-600" data-skill="${esc(s.skill_name)}" data-act="block" title="不再推送">✕</button>` : ''}
    </div>
  </div>`;
}

function applyTakeNToSlots(take) {
  const n = Math.max(0, Math.min(_myServerPushDefault, Math.floor(Number(take) || 0)));
  _myTotalSlots = n;
  _mySlotsAll = (_myServerQueue || []).slice(0, n).map((s, i) => ({
    ...s, rank: i + 1, installed: true,
  }));
  renderMySlots();
  return n;
}

function renderMySlots() {
  const q = (_mySlotsQ || '').trim().toLowerCase();
  const list = q
    ? _mySlotsAll.filter(s => String(s.skill_name || '').toLowerCase().includes(q)
      || String(s.display_name || '').toLowerCase().includes(q))
    : _mySlotsAll;
  const sum = document.getElementById('my-slot-sum');
  if (sum) {
    const srv = _myServerPushed || _myServerQueue.length || _myServerPushDefault;
    sum.textContent = q
      ? `匹配 ${list.length} / ${_mySlotsAll.length} · 已装 ${_mySlotsAll.length} / 服务器 ${srv}`
      : `已装 ${_mySlotsAll.length} · 服务器推送 ${srv}`;
  }
  const box = document.getElementById('my-slots');
  if (box) {
    let empty = '暂无已安装 skill';
    if (q) empty = '无匹配技能';
    else if (_myTotalSlots === 0) empty = '安装个数为 0：服务器仍可能推送，但本机不装 harness';
    box.innerHTML = list.map(_mySlotRowHtml).join('')
      || `<span class="text-slate-400">${empty}</span>`;
  }
}

function syncPushStepper(push) {
  const n = Math.max(0, Math.min(_myServerPushDefault, Math.floor(Number(push) || 0)));
  const hidden = document.getElementById('my-push-count');
  const val = document.getElementById('my-push-val');
  const unit = document.getElementById('my-push-unit');
  const up = document.getElementById('my-push-up');
  const down = document.getElementById('my-push-down');
  if (hidden) hidden.value = String(n);
  if (val) {
    val.max = String(_myServerPushDefault);
    val.min = '0';
    if (document.activeElement !== val) val.value = String(n);
  }
  if (unit) unit.textContent = n === 0 ? '个（不安装）' : '个 SKILL';
  if (up) up.disabled = n >= _myServerPushDefault;
  if (down) down.disabled = n <= 0;
  return n;
}

function commitPushValEdit() {
  const val = document.getElementById('my-push-val');
  let n = Number(val && val.value);
  if (!Number.isFinite(n) || n < 0) n = 0;
  n = syncPushStepper(n);
  applyTakeNToSlots(n);
  scheduleSaveMyPrefs({ take_n: n });
}

function applyMyPrefForm(st) {
  const max = st.server_slots != null ? st.server_slots
    : (st.max != null ? st.max : (st.server_default != null ? st.server_default : 100));
  _myServerPushDefault = max;
  if (st.server_pushed != null) _myServerPushed = st.server_pushed;
  const maxLabel = document.getElementById('my-push-max-label');
  const take = st.take_n != null ? st.take_n : (st.push_count != null ? st.push_count : max);
  syncPushStepper(take);
  if (maxLabel) maxLabel.textContent = `服务器 skill_slots=${max} · client 截取安装`;
  const step = document.getElementById('my-push-step');
  if (step) {
    step.title = `client 从服务器推送队列截取前 N 个安装到 harness（上限=服务器 skill_slots ${max}；0=不安装）`;
  }
}

async function saveMyPrefs(partial) {
  const nEl = document.getElementById('my-push-count');
  let n = Number(partial && partial.take_n != null ? partial.take_n
    : (partial && partial.push_count != null ? partial.push_count : (nEl && nEl.value)));
  if (!Number.isFinite(n) || n < 0) n = 0;
  n = Math.min(_myServerPushDefault, Math.floor(n));
  const msg = document.getElementById('my-pref-msg');
  if (_myPrefSaving) return;
  _myPrefSaving = true;
  try {
    const saved = await jpost('/api/v1/dashboard/my/settings', { take_n: n });
    applyMyPrefForm(saved);
    applyTakeNToSlots(n);
    if (msg) {
      msg.textContent = n === 0 ? '已保存：不安装' : '已保存';
      msg.className = 'text-[11px] text-emerald-600';
    }
  } catch (err) {
    if (msg) { msg.textContent = err.message || '保存失败'; msg.className = 'text-[11px] text-rose-600'; }
  } finally {
    _myPrefSaving = false;
  }
}

function scheduleSaveMyPrefs(partial) {
  clearTimeout(_myPrefSaveTimer);
  _myPrefSaveTimer = setTimeout(() => saveMyPrefs(partial).catch(console.error), 180);
}

async function loadMy() {
  if (!IDENT) return;
  const [m, ct, rt, pref] = await Promise.all([
    j('/api/v1/dashboard/my/manifest'),
    j('/api/v1/dashboard/my/contributions'),
    j('/api/v1/dashboard/my/reco-trigger'),
    j('/api/v1/dashboard/my/settings').catch(() => null),
  ]);
  _myServerQueue = (m.server_push && m.server_push.length)
    ? m.server_push.slice()
    : (m.slots || []).slice();
  _myBlocked = m.blocked || [];
  _myServerPushed = m.server_pushed != null ? m.server_pushed : _myServerQueue.length;
  _myServerPushDefault = m.server_slots != null ? m.server_slots : _myServerPushDefault;
  const take = (pref && pref.take_n != null) ? pref.take_n
    : (m.settings && m.settings.take_n != null) ? m.settings.take_n
    : (m.total_slots != null ? m.total_slots : _myServerPushDefault);
  const qEl = document.getElementById('my-slots-q');
  if (qEl) _mySlotsQ = qEl.value;
  applyMyPrefForm(pref || m.settings || {
    take_n: take,
    server_slots: _myServerPushDefault,
    server_pushed: _myServerPushed,
    max: _myServerPushDefault,
  });
  applyTakeNToSlots(take);
  document.getElementById('my-blocked').innerHTML = _myBlocked.map(b => `
    <span class="inline-flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-lg bg-rose-50 text-rose-700 ring-1 ring-rose-200">${esc(b.skill_name)}
      <button class="my-pref font-medium" data-skill="${esc(b.skill_name)}" data-act="clear">恢复</button></span>`).join('')
    || '<span class="text-[11px] text-slate-400">无</span>';
  const st = ct.steps;
  document.getElementById('my-steps').innerHTML =
    [['轨迹', st.trajs], ['原子', st.atoms], ['被采纳', st.adopted_atoms], ['进入 skill', st.skills]]
      .map(([k, v], i) => `${i ? '<span class="text-slate-300 text-xs">→</span>' : ''}
        <div class="px-3 py-1.5 rounded-lg bg-slate-50 ring-1 ring-slate-100 text-center min-w-[4.5rem]">
          <div class="text-base font-semibold tabular-nums leading-tight">${v}</div><div class="text-[10px] text-slate-400">${k}</div></div>`).join('');

  _rtRows = (rt.rows || []).slice().sort((a, b) =>
    (b.triggers - a.triggers) || (b.exposures - a.exposures) || String(a.skill).localeCompare(String(b.skill)));
  const slotSrc = Object.fromEntries((_mySlotsAll || []).map(s => [s.skill_name, s]));
  _rtRows.forEach(r => {
    const s = slotSrc[r.skill];
    if (s) { r.source = s.source; r.source_path = s.source_path; r.producer = s.producer; r.producer_trajs = s.producer_trajs; }
  });
  _rtPage = 0;
  setRtOpen(false);
  _trajPage = 0;
  _contribTraj = null;
  _contribPayload = null;
  _contribPayloadOffset = null;
  setContribOpen(true);
}
document.addEventListener('click', async e => {
  const side = e.target.closest('.my-row-side');
  if (side) {
    try {
      await jpost('/api/v1/dashboard/my/prefs', {
        skill_name: side.dataset.skill, action: 'pin', side: side.dataset.side || 'main',
      });
      await loadMy();
    } catch (err) { alert(err.message); }
    return;
  }
  const pin = e.target.closest('.my-row-pin');
  if (pin) {
    try {
      await jpost('/api/v1/dashboard/my/prefs', {
        skill_name: pin.dataset.skill, action: 'pin', side: pin.dataset.side || 'main',
      });
      await loadMy();
    } catch (err) { alert(err.message); }
    return;
  }
  const unpin = e.target.closest('.my-row-unpin');
  if (unpin) {
    try {
      await jpost('/api/v1/dashboard/my/prefs', { skill_name: unpin.dataset.skill, action: 'clear' });
      await loadMy();
    } catch (err) { alert(err.message); }
    return;
  }
  const b = e.target.closest('.my-pref');
  if (!b) return;
  try { await jpost('/api/v1/dashboard/my/prefs', { skill_name: b.dataset.skill, action: b.dataset.act }); await loadMy(); }
  catch (err) { alert(err.message); }
});
document.getElementById('contrib-toggle')?.addEventListener('click', () => setContribOpen(!_contribOpen));
document.getElementById('trigger-rate-toggle')?.addEventListener('click', () => {
  const body = document.getElementById('trigger-rate-body');
  const chev = document.getElementById('trigger-rate-chevron');
  const btn = document.getElementById('trigger-rate-toggle');
  if (!body) return;
  const open = body.classList.toggle('hidden') === false;
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (chev) chev.textContent = open ? '▼ 收起' : '▶ 展开';
});
document.getElementById('skills-q')?.addEventListener('input', e => {
  skillsQ = e.target.value;
  clearTimeout(_skillsQTimer);
  _skillsQTimer = setTimeout(() => {
    skillsPage = 0;
    loadSkills().catch(console.error);
  }, 180);
});
document.getElementById('contrib-traj-list')?.addEventListener('click', e => {
  const a = e.target.closest('.contrib-traj');
  if (!a) return;
  _contribTraj = a.dataset.traj;
  renderContribDetail().catch(console.error);
});
document.getElementById('my-uploads-list')?.addEventListener('click', e => {
  const usage = e.target.closest('.my-upload-usage');
  if (usage) {
    e.preventDefault();
    e.stopPropagation();
    loadMyUploadUsage(usage.dataset.skill).catch(console.error);
  }
});
function bumpPushCount(delta) {
  const cur = Number(document.getElementById('my-push-count')?.value) || 0;
  const next = syncPushStepper(cur + delta);
  applyTakeNToSlots(next);
  scheduleSaveMyPrefs({ take_n: next });
}
document.getElementById('my-push-up')?.addEventListener('click', () => bumpPushCount(1));
document.getElementById('my-push-down')?.addEventListener('click', () => bumpPushCount(-1));
document.getElementById('my-push-val')?.addEventListener('change', () => commitPushValEdit());
document.getElementById('my-push-val')?.addEventListener('focus', e => e.target.select());
document.getElementById('my-push-val')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); }
  if (e.key === 'Escape') {
    e.preventDefault();
    syncPushStepper(document.getElementById('my-push-count')?.value);
    e.target.blur();
  }
});
document.getElementById('my-slots-q')?.addEventListener('input', e => {
  _mySlotsQ = e.target.value || '';
  clearTimeout(_mySlotsQTimer);
  _mySlotsQTimer = setTimeout(() => renderMySlots(), 80);
});
document.getElementById('contrib-skills')?.addEventListener('click', e => {
  const g = e.target.closest('.graph-skill');
  if (!g) return;
  _skillBackHash = 'my';
  location.hash = 'skill/' + encodeURIComponent(g.getAttribute('data-skill'));
});
document.getElementById('traj-up')?.addEventListener('click', () => {
  _trajPage--;
  _contribTraj = null;
  renderContribDetail().catch(console.error);
});
document.getElementById('traj-down')?.addEventListener('click', () => {
  _trajPage++;
  _contribTraj = null;
  renderContribDetail().catch(console.error);
});
document.getElementById('rt-toggle')?.addEventListener('click', () => setRtOpen(!_rtOpen));
document.getElementById('rt-prev')?.addEventListener('click', () => { _rtPage--; renderRT(); });
document.getElementById('rt-next')?.addEventListener('click', () => { _rtPage++; renderRT(); });

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
    const cur = u.current_slots != null ? u.current_slots : '—';
    const stg = u.staging_slots != null ? u.staging_slots : '—';
    return `<tr>
      <td class="py-2 font-medium">${esc(u.user)}</td>
      <td>${u.client_version ? esc(u.client_version) : '<span class="text-slate-300">未上报</span>'}</td>
      <td class="text-right tabular-nums">${cur}</td>
      <td class="text-right tabular-nums ${u.staging_slots ? 'text-amber-700' : ''}">${stg}</td>
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
  let assign = { slots: [] };
  try {
    assign = await j('/api/v1/dashboard/admin/user/' + encodeURIComponent(user) + '/assignment');
  } catch (_e) { /* 后端未上线时仅展示偏好 */ }
  const slotRows = (assign.slots || []).map(s => `
    <div class="flex items-center gap-2 px-2.5 py-2 rounded-lg ring-1 ring-slate-100 bg-white">
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="skill-jump cursor-pointer font-medium text-teal-700 text-[12px]" data-skill="${esc(s.skill_name)}">${esc(s.skill_name)}</span>
          <span class="text-[10px] px-1.5 py-0.5 rounded ${BUCKET_CHIP[s.bucket] || 'bg-slate-100 text-slate-500'}">${esc(s.bucket || '')}</span>
          <span class="text-[10px] px-1.5 py-0.5 rounded ${s.side === 'staging' ? 'bg-amber-50 text-amber-700 ring-1 ring-amber-100' : 'bg-slate-100 text-slate-500'}">${esc(s.side || 'main')}</span>
          ${s.sha ? `<code class="text-[10px] text-slate-400">${esc(String(s.sha).slice(0, 7))}</code>` : ''}
        </div>
      </div>
      ${s.side_mutable ? `<div class="shrink-0 inline-flex rounded-lg ring-1 ring-slate-200 overflow-hidden text-[10px]">
          <button class="adm-side px-2 py-0.5 ${s.side === 'main' ? 'bg-teal-600 text-white' : 'bg-white text-slate-500 hover:bg-slate-50'}" data-user="${esc(user)}" data-skill="${esc(s.skill_name)}" data-side="main">main</button>
          <button class="adm-side px-2 py-0.5 ${s.side === 'staging' ? 'bg-teal-600 text-white' : 'bg-white text-slate-500 hover:bg-slate-50'}" data-user="${esc(user)}" data-skill="${esc(s.skill_name)}" data-side="staging">staging</button>
        </div>` : ''}
    </div>`).join('')
    || '<span class="text-[11px] text-slate-400">暂无当前推送记录</span>';
  d.classList.remove('hidden');
  d.innerHTML = `<div class="flex items-baseline justify-between">
      <h3 class="font-medium text-[12.5px]">${esc(user)} 的当前推送 <span class="text-[10.5px] text-slate-400 font-normal ml-1">${(assign.slots || []).length} 槽 · pinned=${p.effective.pinned.length} blocked=${p.effective.blocked.length}</span></h3>
      <div class="flex items-center gap-2">
        <button class="adm-history-toggle text-[11px] text-teal-700 hover:bg-teal-50 px-1.5 rounded" data-user="${esc(user)}" aria-expanded="false">历史曝光</button>
        <button id="adm-drawer-x" class="text-[11px] text-slate-400 hover:bg-slate-100 px-1.5 rounded">收起</button>
      </div></div>
    <div class="mt-2 space-y-1.5 max-h-72 overflow-y-auto">${slotRows}</div>
    <div id="adm-history-panel" data-user="${esc(user)}" class="hidden mt-3 pt-3 border-t border-slate-200"></div>
    <div class="mt-3 pt-3 border-t border-slate-200">
      <div class="text-[11px] text-slate-400 mb-1.5">偏好（pin / 屏蔽）</div>
      <div class="flex flex-wrap gap-1.5">${p.prefs.map(r => `
      <span class="inline-flex items-center gap-1 text-[10.5px] px-2 py-1 rounded-lg ${r.pref === 'pinned' ? 'bg-violet-100 text-violet-700' : 'bg-rose-50 text-rose-700'} ring-1 ring-slate-200">
        ${esc(r.skill_name)} <span class="opacity-60">${esc(r.pref)}·${esc(r.set_by)}</span>
        <button class="adm-pref font-bold" data-user="${esc(user)}" data-skill="${esc(r.skill_name)}" data-act="clear">✕</button></span>`).join('') || '<span class="text-[11px] text-slate-400">无</span>'}</div>
      <div class="mt-3 flex gap-2">
        <input id="adm-skill-in" class="ring-1 ring-slate-200 rounded-lg px-2 py-1 outline-none focus:ring-teal-500 font-mono text-[11px] w-36" placeholder="skill 名">
        <button class="adm-pref px-2 py-1 rounded-lg bg-teal-600 hover:bg-teal-700 text-white text-[11px]" data-user="${esc(user)}" data-act="pin">代 pin</button>
        <button class="adm-pref px-2 py-1 rounded-lg ring-1 ring-rose-200 text-rose-700 hover:bg-rose-50 text-[11px]" data-user="${esc(user)}" data-act="block">代屏蔽</button>
      </div>
    </div>`;
}

async function loadAdminRecommendationHistory(user, offset = 0) {
  const panel = document.getElementById('adm-history-panel');
  if (!panel || panel.dataset.user !== user) return;
  const limit = 20;
  panel.classList.remove('hidden');
  panel.innerHTML = '<span class="text-[11px] text-slate-400">历史曝光加载中…</span>';
  const history = await j(
    '/api/v1/dashboard/admin/user/' + encodeURIComponent(user)
      + '/recommendations?offset=' + offset + '&limit=' + limit,
  );
  if (!panel.isConnected || panel.dataset.user !== user) return;
  const exposureRows = (history.exposures || []).map(row => `
    <div class="flex items-center gap-2 px-2.5 py-2 rounded-lg ring-1 ring-slate-100 bg-white">
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="skill-jump cursor-pointer font-medium text-teal-700 text-[12px]" data-skill="${esc(row.skill)}">${esc(row.skill)}</span>
          <span class="text-[10px] px-1.5 py-0.5 rounded ${BUCKET_CHIP[row.bucket] || 'bg-slate-100 text-slate-500'}">${esc(row.bucket)}</span>
          <span class="text-[10px] px-1.5 py-0.5 rounded ${row.side === 'staging' ? 'bg-amber-50 text-amber-700 ring-1 ring-amber-100' : 'bg-slate-100 text-slate-500'}">${esc(row.side || 'main')}</span>
          ${row.sha ? `<code class="text-[10px] text-slate-400">${esc(String(row.sha).slice(0, 7))}</code>` : ''}
        </div>
      </div>
      <time class="shrink-0 text-[10px] text-slate-400" title="${esc(row.ts)} UTC">${fdate(row.ts)}</time>
    </div>`).join('') || '<span class="text-[11px] text-slate-400">暂无历史曝光</span>';
  const exposureCount = (history.exposures || []).length;
  const start = exposureCount ? history.offset + 1 : 0;
  const end = exposureCount ? history.offset + exposureCount : 0;
  panel.innerHTML = `<div class="flex items-baseline justify-between gap-2">
      <div><span class="text-[11px] text-slate-500">历史曝光</span><span class="text-[10px] text-slate-400 ml-1">按首次曝光时间倒序 · ${start}-${end}/${history.total}</span></div>
      <div class="flex gap-1">
        <button class="adm-history-page text-[10px] px-1.5 py-0.5 rounded ring-1 ring-slate-200 ${history.offset > 0 ? 'hover:bg-slate-50' : 'text-slate-300 cursor-not-allowed'}" data-user="${esc(user)}" data-offset="${Math.max(0, history.offset - history.limit)}" ${history.offset > 0 ? '' : 'disabled'}>上一页</button>
        <button class="adm-history-page text-[10px] px-1.5 py-0.5 rounded ring-1 ring-slate-200 ${history.has_more ? 'hover:bg-slate-50' : 'text-slate-300 cursor-not-allowed'}" data-user="${esc(user)}" data-offset="${history.offset + history.limit}" ${history.has_more ? '' : 'disabled'}>下一页</button>
      </div>
    </div>
    <div class="mt-2 space-y-1.5 max-h-72 overflow-y-auto">${exposureRows}</div>`;
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
  const historyToggle = e.target.closest('.adm-history-toggle');
  if (historyToggle) {
    const panel = document.getElementById('adm-history-panel');
    if (!panel) return;
    const opening = panel.classList.contains('hidden');
    historyToggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
    historyToggle.textContent = opening ? '收起历史' : '历史曝光';
    if (opening) loadAdminRecommendationHistory(historyToggle.dataset.user).catch(err => alert(err.message));
    else panel.classList.add('hidden');
    return;
  }
  const historyPage = e.target.closest('.adm-history-page');
  if (historyPage && !historyPage.disabled) {
    loadAdminRecommendationHistory(
      historyPage.dataset.user,
      Number(historyPage.dataset.offset || 0),
    ).catch(err => alert(err.message));
    return;
  }
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
  const aside = e.target.closest('.adm-side');
  if (aside) {
    try {
      await jpost('/api/v1/dashboard/admin/prefs', {
        user_key: aside.dataset.user, skill_name: aside.dataset.skill,
        action: 'pin', side: aside.dataset.side,
      });
      await openAdminDrawer(aside.dataset.user); await loadAdmin();
    } catch (err) { alert(err.message); }
    return;
  }
  const rside = e.target.closest('.route-row-side');
  if (rside) {
    try { await _routePref(rside.dataset.user, 'pin', rside.dataset.side || 'main'); }
    catch (err) { alert(err.message); }
    return;
  }
  const rpin = e.target.closest('.route-row-pin');
  if (rpin) {
    try { await _routePref(rpin.dataset.user, 'pin', rpin.dataset.side || 'main'); }
    catch (err) { alert(err.message); }
    return;
  }
  const runpin = e.target.closest('.route-row-unpin');
  if (runpin) {
    try { await _routePref(runpin.dataset.user, 'clear'); }
    catch (err) { alert(err.message); }
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

// ── 设置 ────────────────────────────────────────────────────────
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

// ── 世界消息 feed（三角翻页,每页 5;区分自己/他人） ──────────────
function dayLabel(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  return diff <= 0 ? '今天' : diff === 1 ? '昨天' : `${day.getMonth() + 1}-${String(day.getDate()).padStart(2, '0')}`;
}
function paintFeedPage() {
  const el = document.getElementById('world-feed');
  if (!el) return;
  const pages = _feedPages.length;
  const sum = document.getElementById('feed-sum');
  const lab = document.getElementById('feed-page-label');
  const up = document.getElementById('feed-up');
  const down = document.getElementById('feed-down');
  if (!pages) {
    el.innerHTML = '<span class="text-slate-400 text-xs">还没有团队动态</span>';
    if (sum) sum.textContent = '';
    if (lab) lab.textContent = '0/0';
    if (up) up.disabled = true;
    if (down) down.disabled = true;
    return;
  }
  if (_feedPage >= pages) _feedPage = pages - 1;
  if (_feedPage < 0) _feedPage = 0;
  const evs = _feedPages[_feedPage] || [];
  let lastDay = null;
  el.innerHTML = '';
  evs.forEach(ev => {
    const dl = dayLabel(evDate(ev));
    if (dl !== lastDay) {
      el.insertAdjacentHTML('beforeend', `<div class="text-[10.5px] text-slate-400 font-medium mt-2.5 mb-1 first:mt-0">${esc(dl)}</div>`);
      lastDay = dl;
    }
    const mine = IDENT && ev.actor === IDENT.user;
    const ring = mine ? 'ring-teal-200 bg-teal-50/40' : 'ring-slate-100';
    el.insertAdjacentHTML('beforeend', `
      <div class="flex items-start gap-2.5 px-3 py-2 rounded-xl ring-1 ${ring} hover:bg-slate-50 mb-1 text-xs">
        ${avatar(ev.actor || 'xs', 'sm')}
        <div class="min-w-0 flex-1">${evParts(ev).html}</div>
        <span class="text-[10.5px] text-slate-400 shrink-0" title="${esc(ev.ts)} UTC">${relTime(ev)}</span>
      </div>`);
  });
  const shownFrom = _feedPage * FEED_PAGE + 1;
  const shownTo = shownFrom + evs.length - 1;
  if (sum) sum.textContent = evs.length ? `${shownFrom}–${shownTo}` : '';
  if (lab) lab.textContent = `${_feedPage + 1}/${_feedExhausted ? pages : (pages + '+')}`;
  if (up) up.disabled = _feedPage === 0;
  if (down) down.disabled = _feedPage >= pages - 1 && _feedExhausted;
}
async function ensureFeedPage(idx) {
  while (_feedPages.length <= idx && !_feedExhausted) {
    const q = '/api/v1/dashboard/events?scope=world&limit=' + FEED_PAGE
      + (_feedBefore ? '&before_id=' + _feedBefore : '');
    const d = await j(q);
    const evs = d.events || [];
    if (!evs.length) { _feedExhausted = true; break; }
    _feedPages.push(evs);
    _feedBefore = evs[evs.length - 1].id;
    if (evs.length < FEED_PAGE) _feedExhausted = true;
  }
}
async function loadWorldFeed() {
  const el = document.getElementById('world-feed');
  if (!el || !IDENT) return;
  _feedPages = [];
  _feedPage = 0;
  _feedBefore = null;
  _feedExhausted = false;
  try {
    await ensureFeedPage(0);
    paintFeedPage();
  } catch (e) {
    el.innerHTML = `<span class="text-[11px] text-rose-600">${esc(e.message)}</span>`;
  }
}
document.getElementById('feed-up')?.addEventListener('click', () => {
  _feedPage--;
  paintFeedPage();
});
document.getElementById('feed-down')?.addEventListener('click', async () => {
  try {
    await ensureFeedPage(_feedPage + 1);
    if (_feedPages[_feedPage + 1]) _feedPage++;
    paintFeedPage();
  } catch (e) { console.error(e); }
});

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
