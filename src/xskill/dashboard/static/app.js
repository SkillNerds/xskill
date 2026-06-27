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
const tok = n => {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return '' + n;
};
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
    `<tr><td><a href="#" class="skill-link" data-skill="${esc(s.name)}">${esc(s.name)}</a></td>`
    + `<td><span class="badge ${STATE_BADGE[s.state] || 'bg-secondary-lt'}">${esc(s.state)}</span></td>`
    + `<td class="text-secondary" style="max-width:520px">${esc(s.description) || '—'}</td>`
    + `<td class="text-end">v${esc(s.version)}</td>`
    + `<td class="text-end">${s.candidates || 0}</td>`
    + `<td class="text-end">${s.use_count || 0}</td></tr>`).join(''));
}

// ── 单 skill 详情 drill-in（子项目 D2）─────────────────────────────

// 确定性 SVG 折线（给定数据必出同图，不靠图表库；符合"骨架由确定性工具产出"）
function sparkline(points, w = 320, h = 60) {
  const vals = points.map(p => Number(p) || 0);
  if (!vals.length) return '<span class="text-secondary">无数据</span>';
  const max = Math.max(...vals), min = Math.min(...vals);
  const span = (max - min) || 1, n = vals.length;
  const dx = n > 1 ? (w - 8) / (n - 1) : 0;
  const pts = vals.map((v, i) =>
    `${(4 + i * dx).toFixed(1)},${(h - 4 - (v - min) / span * (h - 8)).toFixed(1)}`).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">`
    + `<polyline fill="none" stroke="#206bc4" stroke-width="2" points="${pts}"/>`
    + vals.map((v, i) => `<circle cx="${(4 + i * dx).toFixed(1)}" cy="${(h - 4 - (v - min) / span * (h - 8)).toFixed(1)}" r="2.5" fill="#206bc4"/>`).join('')
    + `</svg>`;
}

// diff 文本 → 红绿 HTML（+ 绿 / - 红）
function renderDiff(diff) {
  if (!diff) return '<span class="text-secondary">无 diff</span>';
  return '<pre class="diff-view" style="font-size:12px;line-height:1.4">' + diff.split('\n').map(line => {
    const e = esc(line);
    if (line.startsWith('+') && !line.startsWith('+++')) return `<span style="background:#e6ffed;color:#22863a">${e}</span>`;
    if (line.startsWith('-') && !line.startsWith('---')) return `<span style="background:#ffeef0;color:#b31d28">${e}</span>`;
    if (line.startsWith('@@')) return `<span style="color:#6f42c1">${e}</span>`;
    return e;
  }).join('\n') + '</pre>';
}

function detailBox() {
  let box = document.getElementById('skill-detail');
  if (!box) {
    box = document.createElement('div');
    box.id = 'skill-detail';
    box.className = 'card mt-3';
    const sec = document.getElementById('pg-skills') || document.body;
    sec.appendChild(box);
  }
  return box;
}

async function loadSkillDetail(name) {
  const box = detailBox();
  box.innerHTML = `<div class="card-body">加载 ${esc(name)} …</div>`;
  const [d, tree] = await Promise.all([
    j('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/detail'),
    j('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/tree'),
  ]);
  const vrows = (d.versions || []).map(v =>
    `<tr><td><code>${esc((v.sha || '').slice(0, 8))}</code></td><td class="text-end">${v.triggers}</td>`
    + `<td class="text-end">${ux(v.avg_ux)}</td><td class="text-end">${v.avg_tool_calls}</td>`
    + `<td class="text-end">${tok(v.avg_tokens)}</td></tr>`).join('')
    || '<tr><td colspan="5" class="text-secondary">还没有版本触发数据</td></tr>';
  const userRows = (d.by_user || []).map(u =>
    `<tr><td>${esc(u.user)}</td><td class="text-end">${u.triggers}</td><td class="text-end">${ux(u.avg_ux)}</td></tr>`).join('');
  const trend = (d.trend || []).map(p => p.ux);
  const fileItems = (tree.files || []).map(f =>
    `<a href="#" class="list-group-item list-group-item-action py-1 px-2 skf" data-skill="${esc(name)}" data-path="${esc(f.path)}">${esc(f.path)} <span class="text-secondary">(${f.size})</span></a>`).join('');
  const gitItems = (d.versions_git || []).map(g =>
    `<a href="#" class="list-group-item list-group-item-action py-1 px-2 skd" data-skill="${esc(name)}" data-sha="${esc(g.sha)}"><code>${esc(g.short)}</code> ${esc(g.subject)}</a>`).join('');

  box.innerHTML = `<div class="card-body">
    <div class="d-flex justify-content-between"><h3>${esc(name)}</h3>
      <div>总触发 <strong>${d.total_triggers}</strong> 次</div></div>
    <div class="row mt-2">
      <div class="col-md-7">
        <div class="subheader">版本统计（每版本触发 / UX / 平均工具调用 / 平均 token）</div>
        <table class="table table-sm"><thead><tr><th>版本</th><th class="text-end">触发</th><th class="text-end">UX</th><th class="text-end">工具/atom</th><th class="text-end">token/atom</th></tr></thead><tbody>${vrows}</tbody></table>
        <div class="subheader mt-2">跨版本 UX 进化趋势</div>${sparkline(trend)}
        <div class="subheader mt-3">按用户</div>
        <table class="table table-sm"><tbody>${userRows || '<tr><td class="text-secondary">无</td></tr>'}</tbody></table>
      </div>
      <div class="col-md-5">
        <div class="subheader">文件目录</div>
        <div class="list-group list-group-flush" style="max-height:160px;overflow:auto">${fileItems}</div>
        <div class="subheader mt-2">版本（点击看红绿 diff）</div>
        <div class="list-group list-group-flush" style="max-height:140px;overflow:auto">${gitItems}</div>
      </div>
    </div>
    <div id="skill-trigger" class="mt-3"><div class="text-secondary">加载触发率…</div></div>
    <div class="mt-2"><div class="subheader">预览 / diff</div><div id="skill-preview" class="border rounded p-2" style="max-height:320px;overflow:auto"><span class="text-secondary">点左侧文件或版本查看</span></div></div>
  </div>`;
  box.scrollIntoView({ behavior: 'smooth' });
  loadTriggerPanel(name).catch(console.error);
}

// 离线探针触发率面板（描述质量信号；区别于上方"总触发"的线上真实使用率）
function pctf(x) { return Math.round((Number(x) || 0) * 100) + '%'; }

function triggerHistoryRows(history) {
  return (history || []).map(h =>
    `<tr><td><code>${esc((h.version_sha || '—').slice(0, 8))}</code></td><td class="text-end">${pctf(h.test_score)}</td>`
    + `<td class="text-end">${pctf(h.train_score)}</td><td class="text-end">${h.n_cases}</td>`
    + `<td class="text-end">${h.catalog_size}</td><td class="text-secondary">${esc((h.ts || '').slice(0, 16))}</td></tr>`).join('')
    || '<tr><td colspan="6" class="text-secondary">还没有离线触发评测</td></tr>';
}

function triggerCaseRows(cases, name) {
  return (cases || []).map(c =>
    `<tr><td>${esc(c.query)}</td><td class="text-center">${c.should_trigger ? '是' : '否'}</td>`
    + `<td class="text-center">${c.did_trigger ? '触发' : '未触发'}</td>`
    + `<td class="text-center">${c.passed ? '✓' : '✗'}</td>`
    + `<td class="text-secondary small">${esc((c.catalog || []).join(', '))}</td>`
    + `<td><button class="btn btn-sm btn-outline-primary trig-rerun" data-skill="${esc(name)}" data-query="${esc(c.query)}">重跑</button></td></tr>`).join('')
    || '<tr><td colspan="6" class="text-secondary">无 case（该 skill 还没跑过触发优化）</td></tr>';
}

async function loadTriggerPanel(name) {
  const el = document.getElementById('skill-trigger');
  if (!el) return;
  let hist = { history: [] }, cases = { cases: [], exp: null };
  try { hist = await j('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/trigger'); } catch (e) { /* 空 */ }
  try { cases = await j('api/v1/dashboard/skill/' + encodeURIComponent(name) + '/trigger/cases'); } catch (e) { /* 空 */ }
  const hrows = triggerHistoryRows(hist.history);
  const crows = triggerCaseRows(cases.cases, name);
  el.innerHTML = `<div class="subheader">离线探针触发率 <span class="text-secondary">（描述质量信号——真跑代理在语义相关技能清单里抢触发；区别于上方"总触发"的线上真实使用）</span></div>
    <table class="table table-sm"><thead><tr><th>版本</th><th class="text-end">test 触发率</th><th class="text-end">train</th><th class="text-end">cases</th><th class="text-end">诱饵数</th><th>时间</th></tr></thead><tbody>${hrows}</tbody></table>
    <div class="subheader mt-2">逐 case <span class="text-secondary">（实验 ${esc(cases.exp || '—')}；点"重跑"用当前描述真跑一轮探针）</span></div>
    <table class="table table-sm"><thead><tr><th>query</th><th class="text-center">应触发</th><th class="text-center">实测</th><th class="text-center">通过</th><th>诱饵清单</th><th></th></tr></thead><tbody>${crows}</tbody></table>`;
}

// 点击：技能名 → 详情；文件 → 预览；版本 → diff
document.addEventListener('click', async e => {
  const sl = e.target.closest('.skill-link');
  if (sl) { e.preventDefault(); loadSkillDetail(sl.dataset.skill).catch(console.error); return; }
  const fl = e.target.closest('.skf');
  if (fl) {
    e.preventDefault();
    const r = await j('api/v1/dashboard/skill/' + encodeURIComponent(fl.dataset.skill) + '/file?path=' + encodeURIComponent(fl.dataset.path));
    document.getElementById('skill-preview').innerHTML = r.content != null
      ? `<pre style="font-size:12px">${esc(r.content)}</pre>` : `<span class="text-danger">${esc(r.error || 'error')}</span>`;
    return;
  }
  const dl = e.target.closest('.skd');
  if (dl) {
    e.preventDefault();
    const r = await j('api/v1/dashboard/skill/' + encodeURIComponent(dl.dataset.skill) + '/diff?sha=' + encodeURIComponent(dl.dataset.sha));
    document.getElementById('skill-preview').innerHTML = renderDiff(r.diff);
    return;
  }
  // 逐 case"重跑"：用当前描述真跑一轮探针（action 端点），结果回填按钮
  const rb = e.target.closest('.trig-rerun');
  if (rb) {
    e.preventDefault();
    rb.disabled = true;
    rb.textContent = '跑…';
    try {
      const resp = await fetch('api/v1/dashboard/skill/' + encodeURIComponent(rb.dataset.skill) + '/trigger/rerun',
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: rb.dataset.query }) });
      const data = await resp.json();
      rb.classList.remove('btn-outline-primary');
      if (data.error) { rb.textContent = '错误'; rb.classList.add('btn-outline-danger'); }
      else if (data.did_trigger) { rb.textContent = '触发 ✓'; rb.classList.add('btn-outline-success'); }
      else { rb.textContent = '未触发'; rb.classList.add('btn-outline-secondary'); }
      rb.title = '诱饵清单: ' + ((data.catalog || []).join(', ') || '空');
    } catch (err) {
      console.error(err);
      rb.textContent = '错误';
    }
    rb.disabled = false;
  }
});

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
