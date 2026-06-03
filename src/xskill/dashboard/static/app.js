// xskill 控制台前端取数：启动后并发 fetch 各只读端点，填进 DOM。
async function j(u) {
  const r = await fetch(u);
  if (!r.ok) throw new Error(u + ' ' + r.status);
  return r.json();
}
function put(sel, val) {
  document.querySelectorAll(`[data-m="${sel}"]`).forEach(e => { e.textContent = val; });
}
function rows(bodyId, html) {
  const tb = document.getElementById(bodyId);
  if (tb) tb.innerHTML = html || '<tr><td colspan="6" class="text-secondary">暂无数据</td></tr>';
}
const money = n => '$' + (Number(n) || 0).toFixed(4);
const tok = n => { n = Number(n) || 0; return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : '' + n; };
// 把任何要塞进 innerHTML 的值转义——否则 model 名如 `<synthetic>` 会被浏览器
// 当作未知标签吞掉(整行变空白),也堵住注入风险。
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
// ux 是 1–10 分；0 只可能是"还没有任何评分",显示 — 而不是误导性的 0。
const ux = v => (!v || Number(v) === 0) ? '—' : v;

async function loadOverview() {
  const o = await j('api/v1/dashboard/overview');
  put('overview.trajs', o.trajs);
  put('overview.atoms', o.atoms);
  put('overview.avg_ux', ux(o.avg_ux));
  put('overview.avg_atoms_per_traj', o.avg_atoms_per_traj);
  // skill_yield 恒 0% 实为"未通过 trajectory.skill_generated 跟踪"（skill 走
  // candidates/git 落地）→ 0 时显示 — 而非误导性的 0%。
  put('overview.skill_yield', o.skill_yield ? o.skill_yield + '%' : '—');
  put('overview.success_rate', o.success_rate + '%');
  put('overview.retry_rate', o.retry_rate + '%');
  const h = o.price_health, el = document.getElementById('price-warn');
  if (el && h && h.ok === false) {
    const reason = { schema_changed: '上游格式变更', source_moved: '上游地址失效', unreachable: '上游不可达' }[h.kind] || '刷新异常';
    el.innerHTML = `<div class="alert alert-warning mb-0 py-2 px-3 small">⚠ 价格表 ${h.stale_days != null ? h.stale_days + 'd' : '从未'} 未刷新 · ${reason}，沿用旧价</div>`;
  }
}

async function loadRates() {
  const r = await j('api/v1/dashboard/rates');
  // 没有底层事件时显示 — 而非 0%（分析式容错：无数据 ≠ 0）
  const pctOr = (rate, denom) => (denom > 0 ? rate + '%' : '—');
  const recsTotal = (r.trigger.by_skill || []).reduce((a, s) => a + (s.recommended || 0), 0);
  put('rates.trigger', pctOr(r.trigger.overall, recsTotal));
  put('rates.adoption', pctOr(r.adoption.rate, r.adoption.total));
  put('rates.promotion', pctOr(r.promotion.rate, r.promotion.decided));
  put('rates.promotion2', pctOr(r.promotion.rate, r.promotion.decided));
  put('promotion.detail', `${r.promotion.promoted}/${r.promotion.decided} 已裁决`);
  rows('trigger-body', r.trigger.by_skill.map(s =>
    `<tr><td>${esc(s.skill)}</td><td class="text-end">${s.recommended}</td><td class="text-end">${s.used}</td><td class="text-end">${s.rate}%</td></tr>`).join(''));
}

async function loadDomain() {
  const d = await j('api/v1/dashboard/by-domain');
  const mk = (arr, key) => arr.map(r =>
    `<tr><td>${esc(r[key])}</td><td class="text-end">${r.trajs}</td><td class="text-end">${r.avg_atoms}</td><td class="text-end">${r.skills}</td><td class="text-end">${ux(r.avg_ux)}</td></tr>`).join('');
  rows('eco-body', mk(d.by_ecosystem, 'ecosystem'));
  rows('model-body', mk(d.by_model, 'model'));
}

async function loadCost() {
  const c = await j('api/v1/dashboard/cost');
  put('cost.today', money(c.today_usd));
  put('cost.today2', money(c.today_usd));
  put('cost.total', money(c.total_usd));
  put('cost.tokens', tok(c.total_tokens));
  put('cost.calls', c.total_calls);
  rows('cost-model-body', (c.by_model || []).map(m =>
    `<tr><td>${esc(m.model)}</td><td class="text-end">${tok(m.tokens)}</td><td class="text-end">${m.calls}</td><td class="text-end">${money(m.cost)}</td></tr>`).join(''));
  rows('cost-step-body', (c.by_step || []).map(s =>
    `<tr><td>${esc(s.step)}</td><td class="text-end">${tok(s.tokens)}</td><td class="text-end">${money(s.cost)}</td></tr>`).join(''));
}

async function loadModels() {
  const m = await j('api/v1/dashboard/models');
  rows('profile-model-body', (m.models || []).map(x =>
    `<tr><td>${esc(x.model)}</td><td class="text-end">${x.trajs}</td><td class="text-end">${x.pct}%</td></tr>`).join(''));
  rows('profile-harness-body', (m.harnesses || []).map(x =>
    `<tr><td><span class="badge bg-teal-lt">${esc(x.harness)}</span></td><td class="text-end">${x.trajs}</td><td class="text-end">${x.pct}%</td></tr>`).join(''));
}

async function loadUsers() {
  const d = await j('api/v1/dashboard/users');
  put('users.summary', `共 ${d.total} 个用户 · 悬浮/点击行高亮其标签`);
  rows('users-body', (d.users || []).map(u =>
    `<tr data-uid="${esc(u.client_id)}"><td><code>${esc(u.client_id)}</code></td><td class="text-end">${u.trajs}</td>`
    + `<td class="text-end">${u.atoms}</td><td class="text-secondary">${esc(u.last_active) || '—'}</td></tr>`).join('')
    || '<tr><td colspan="4" class="text-secondary">暂无团队用户（非 team server 或尚无 client 上传）</td></tr>');
}

async function loadTags() {
  const d = await j('api/v1/dashboard/tags');
  const el = document.getElementById('tagcloud');
  const tags = d.tags || [];
  if (!el) return;
  if (!tags.length) { el.innerHTML = '<span class="text-secondary">暂无标签（轨迹还没拆出带 tags 的原子）</span>'; return; }
  const max = Math.max(...tags.map(t => t.count)), min = Math.min(...tags.map(t => t.count));
  el.innerHTML = tags.map(t => {
    const sz = (12 + (max > min ? (t.count - min) / (max - min) * 18 : 6)).toFixed(0);
    const users = (t.users || []).map(esc).join(' ');
    return `<span class="badge bg-teal-lt me-2 tagchip" data-users="${users}" title="${esc(t.count)} 次" style="font-size:${sz}px">${esc(t.tag)}</span>`;
  }).join(' ');
}

// 用户 ⇄ 标签联动：悬浮(或点击 pin)某用户行 → 高亮其贡献的标签、淡化其余。
let _pinnedUid = null;
function highlightUser(uid) {
  document.querySelectorAll('#tagcloud .tagchip').forEach(ch => {
    const us = (ch.dataset.users || '').split(' ').filter(Boolean);
    const on = uid && us.includes(uid);
    ch.classList.toggle('hot', !!on);
    ch.classList.toggle('dim', !!uid && !on);
  });
  document.querySelectorAll('#users-body tr[data-uid]').forEach(tr =>
    tr.classList.toggle('table-active', !!uid && tr.dataset.uid === uid));
}
document.addEventListener('mouseover', e => {
  const tr = e.target.closest('#users-body tr[data-uid]');
  if (tr && !_pinnedUid) highlightUser(tr.dataset.uid);
});
document.addEventListener('mouseout', e => {
  const tr = e.target.closest('#users-body tr[data-uid]');
  if (tr && !_pinnedUid) highlightUser(null);
});
document.addEventListener('click', e => {
  const tr = e.target.closest('#users-body tr[data-uid]');
  if (!tr) return;
  _pinnedUid = (_pinnedUid === tr.dataset.uid) ? null : tr.dataset.uid;
  highlightUser(_pinnedUid);
});

async function loadCanary() {
  const c = await j('api/v1/dashboard/canary');
  rows('canary-body', (c.sides || []).map(s =>
    `<tr><td>${esc(s.side)}</td><td class="text-end">${s.trajs}</td><td class="text-end">${ux(s.avg_ux)}</td></tr>`).join(''));
}

const STATE_BADGE = { main: 'bg-green-lt', staging: 'bg-yellow-lt', baby: 'bg-azure-lt', unknown: 'bg-secondary-lt' };
async function loadSkills() {
  const d = await j('api/v1/dashboard/skills');
  const bs = d.by_state || {};
  const parts = Object.keys(bs).sort().map(k => `${k} ${bs[k]}`).join(' · ');
  put('skills.summary', `共 ${d.total} 个 · ${parts}`);
  rows('skills-body', (d.skills || []).map(s =>
    `<tr><td>${esc(s.name)}</td>`
    + `<td><span class="badge ${STATE_BADGE[s.state] || 'bg-secondary-lt'}">${esc(s.state)}</span></td>`
    + `<td class="text-secondary" style="max-width:520px">${esc(s.description) || '—'}</td>`
    + `<td class="text-end">v${esc(s.version)}</td>`
    + `<td class="text-end">${s.candidates || 0}</td>`
    + `<td class="text-end">${s.use_count || 0}</td></tr>`).join(''));
}

async function loadDirs() {
  const d = await j('api/v1/dashboard/dirs');
  rows('dirs-body', (d.dirs || []).map(x =>
    `<tr><td><span class="badge bg-teal-lt">${esc(x.ecosystem || 'manual')}</span></td><td class="text-end">${x.traj_count}</td><td class="text-end">${x.indexed_count}</td><td class="text-secondary">${esc(x.path)}</td></tr>`).join(''));
}

// 分区切换（侧栏）
const NAMES = { overview: '总览', cost: '成本 & 用量', profile: '用户 & 画像', skills: '技能库', canary: '灰度 Canary', eco: '生态目录' };
document.body.addEventListener('click', e => {
  const a = e.target.closest('[data-pg]');
  if (!a) return;
  e.preventDefault();
  let pg = a.dataset.pg;
  if (!document.getElementById('pg-' + pg)) pg = 'overview';
  document.querySelectorAll('.sec-page').forEach(s => s.classList.remove('on'));
  document.getElementById('pg-' + pg).classList.add('on');
  document.querySelectorAll('#nav .nav-link').forEach(n => { n.classList.add('text-white-50'); n.classList.remove('text-white', 'active'); });
  const link = document.querySelector('#nav [data-pg="' + pg + '"]');
  if (link) { link.classList.add('text-white', 'active'); link.classList.remove('text-white-50'); }
  document.getElementById('pgname').textContent = NAMES[pg] || '总览';
  window.scrollTo(0, 0);
});

// 每个端点独立加载，互不阻塞——单个失败不拖垮整页
for (const f of [loadOverview, loadRates, loadDomain, loadCost, loadModels, loadCanary, loadDirs, loadSkills, loadUsers, loadTags]) {
  f().catch(e => console.error(e));
}
