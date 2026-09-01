// Auth
function getToken() { return localStorage.getItem('dia_token') || ''; }
const _origFetch = window.fetch;
window.fetch = function(url, opts = {}) {
  if (typeof url === 'string' && url.startsWith('/api/')) {
    opts.headers = { ...(opts.headers || {}), 'x-auth-token': getToken() };
  }
  return _origFetch(url, opts);
};

let currentRunId   = null;
let currentPage    = 1;
let totalPages     = 1;
let allRows        = [];
let selectedRows   = new Set();
let activeRowKey   = null;
let activeRow      = null;
let filterDebounce = null;
let dashboardData  = null;

const PER_PAGE = 50;

function rvLogout() {
  fetch('/api/auth/logout', { method: 'POST' });
  localStorage.removeItem('dia_token');
  localStorage.removeItem('dia_user');
  localStorage.removeItem('dia_last_run');
  window.location.href = '/login';
}

document.addEventListener('DOMContentLoaded', async () => {
  const token = getToken();
  if (!token) { window.location.href = '/login'; return; }
  const check = await fetch('/api/auth/me');
  if (!check.ok) { window.location.href = '/login'; return; }
  const user = await check.json();
  const displayName = user.display_name || user.username || '?';
  const unEl = document.getElementById('rvUsername');
  const avEl = document.getElementById('rvAvatar');
  if (unEl) unEl.textContent = displayName;
  if (avEl) avEl.textContent = displayName.charAt(0).toUpperCase();
  document.addEventListener('keydown', handleKeyboard);
  await loadRuns();
});

async function loadRuns() {
  try {
    const runs = await fetch('/api/runs?limit=50').then(r => r.json());
    const sel = document.getElementById('runSelect');
    sel.innerHTML = '<option value="">Select a run…</option>';
    if (!runs.length) { show('rvEmpty'); return; }

    runs.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.run_id;
      const ts = r.created_at ? new Date(r.created_at).toLocaleString() : '';
      opt.textContent = `${r.filename || r.run_id}  —  ${(r.total_rows||0).toLocaleString()} rows  ${ts}`;
      sel.appendChild(opt);
    });

    // auto-load from URL param or first run
    const urlRunId = new URLSearchParams(location.search).get('run_id');
    const firstId  = urlRunId || runs[0].run_id;
    sel.value = firstId;
    await loadRun(firstId);
  } catch(e) { console.error(e); show('rvEmpty'); }
}

let currentView   = 'clusters';
let clustersData  = null;
let flatLoaded    = false;
let currentGroupBy = 'signature';

function allClusters() {
  if (!clustersData) return [];
  return [...(clustersData.rule_clusters || []), ...(clustersData.emergent_clusters || [])];
}

async function loadRun(runId) {
  if (!runId) { show('rvEmpty'); return; }
  currentRunId = runId;
  currentPage  = 1;
  selectedRows.clear();
  activeRowKey = null;
  flatLoaded   = false;
  clustersData = null;
  clearDetail();

  hide('rvEmpty'); hide('rvKpiStrip'); hide('rvFilters'); hide('rvListPanel');
  hide('rvBody'); hide('rvClusterView'); hide('rvViewToggle');
  show('rvLoading');

  try {
    // KPI dashboard (shared by both views)
    const dRes = await fetch(`/api/runs/${currentRunId}/review?per_page=1`);
    if (dRes.ok) { const d = await dRes.json(); renderDashboard(d.dashboard); }

    await loadClusters();
    hide('rvLoading');
    show('rvKpiStrip'); show('rvViewToggle');
    setView('clusters');
  } catch(e) {
    console.error(e);
    hide('rvLoading');
    show('rvEmpty');
  }
}

function setView(view) {
  currentView = view;
  document.getElementById('viewClustersBtn').classList.toggle('active', view === 'clusters');
  document.getElementById('viewFlatBtn').classList.toggle('active', view === 'flat');
  if (view === 'clusters') {
    show('rvClusterView'); hide('rvBody'); hide('rvFilters'); hide('rvListPanel');
  } else {
    hide('rvClusterView'); show('rvBody');
    if (!flatLoaded) { flatLoaded = true; fetchPage(); }
    else { show('rvFilters'); show('rvListPanel'); }
  }
}

async function loadClusters() {
  const res = await fetch(`/api/runs/${currentRunId}/clusters?group_by=${currentGroupBy}`);
  if (!res.ok) throw new Error(await res.text());
  clustersData = await res.json();
  renderTestOverview(clustersData.test_overview || []);
  renderClusterBoard(clustersData.rule_clusters || []);
  renderEmergentBoard(clustersData.emergent_clusters || []);
  document.getElementById('rvSuggest').style.display =
    (clustersData.emergent_clusters || []).length ? 'block' : 'none';
  // restore cached AI triage if present
  if (clustersData.ai && clustersData.ai.executive_summary) {
    renderTriage(clustersData.ai);
  }
}

async function setGroupBy(gb) {
  if (gb === currentGroupBy) return;
  currentGroupBy = gb;
  document.querySelectorAll('#rvGroupBy .rv-gb-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.gb === gb));
  document.getElementById('rvBoardSub').textContent =
    gb === 'signature' ? 'grouped by anomaly pattern' : `grouped by ${gb}`;
  await loadClusters();
}

function renderTestOverview(tests) {
  const el = document.getElementById('rvTestOverview');
  if (!tests.length) { el.innerHTML = ''; return; }
  el.innerHTML = tests.map(t => `
    <div class="rv-test-ov-card${t.red_flag ? ' red-flag' : ''}">
      <div class="rv-test-ov-name">${t.red_flag ? '<span class="rv-rf">⚑</span> ' : ''}${esc(t.test_name)}</div>
      <div class="rv-test-ov-stats">
        <span class="rv-test-ov-hits">${fmt(t.hit_count)} hits</span>
        <span class="rv-test-ov-amt">${fmtAmt(t.amount_at_risk)}</span>
      </div>
    </div>
  `).join('');
}

function renderClusterBoard(clusters) {
  const board = document.getElementById('rvClusterBoard');
  document.getElementById('rvBoardSub').textContent =
    currentGroupBy === 'signature'
      ? `${clusters.length} patterns · most tests fired first`
      : `${clusters.length} clusters · grouped by ${currentGroupBy}`;
  if (!clusters.length) {
    board.innerHTML = '<div class="rv-board-empty">No clusters found</div>';
    return;
  }
  board.innerHTML = clusters.map(c => buildClusterCard(c)).join('');
}

function renderEmergentBoard(clusters) {
  const board = document.getElementById('rvEmergentBoard');
  document.getElementById('rvEmergentSub').textContent =
    clusters.length
      ? `${clusters.length} data-driven anomalies no rule flagged`
      : 'nothing statistically abnormal beyond your rules';
  if (!clusters.length) {
    board.innerHTML = '<div class="rv-board-empty">No emergent anomalies detected</div>';
    return;
  }
  board.innerHTML = clusters.map(c => buildEmergentCard(c)).join('');
}

function buildClusterCard(c) {
  const chips = (c.evidence_chips || []).map(ch => `<span class="rv-cl-chip">${esc(ch)}</span>`).join('');
  const aiLine = c.ai_summary ? `<div class="rv-cl-ai">✦ ${esc(c.ai_summary)}</div>` : '';
  const reviewedBadge = c.reviewed_pct > 0
    ? `<span class="rv-cl-reviewed">${c.reviewed_pct}% reviewed</span>` : '';
  const tcBadge = c.test_count > 1
    ? `<span class="rv-cl-tc">${c.test_count} tests</span>` : '';

  return `
    <div class="rv-cl-card band-${c.dominant_band}" id="clcard_${c.cluster_id}">
      <div class="rv-cl-head" onclick="toggleCluster('${c.cluster_id}')">
        <div class="rv-cl-rank">#${c.rank}</div>
        <div class="rv-cl-main">
          <div class="rv-cl-title">${c.red_flag ? '<span class="rv-rf">⚑</span> ' : ''}${esc(c.title)} ${tcBadge}</div>
          <div class="rv-cl-sub"><span class="rv-cl-test">${esc(c.subtitle || '')}</span></div>
          <div class="rv-cl-chips">${chips} ${reviewedBadge}</div>
          ${aiLine}
        </div>
        <div class="rv-cl-metrics">
          <div class="rv-cl-size">${fmt(c.size)}<span>invoices</span></div>
          <div class="rv-cl-amt">${c.amount_display}<span>at risk</span></div>
          <div class="rv-cl-band ${c.dominant_band}">${c.dominant_band}</div>
        </div>
        <div class="rv-cl-chevron" id="clchev_${c.cluster_id}">▾</div>
      </div>
      <div class="rv-cl-body" id="clbody_${c.cluster_id}" style="display:none">
        <div class="rv-cl-actions">
          <button class="rv-cl-btn confirm" onclick="clusterBulk('${c.cluster_id}','confirmed')">✓ Confirm all ${c.size}</button>
          <button class="rv-cl-btn fp" onclick="clusterBulk('${c.cluster_id}','false_positive')">✕ Dismiss all ${c.size}</button>
          <span class="rv-cl-samplenote">showing top ${Math.min(c.sample_rows.length, c.size)} of ${c.size}</span>
        </div>
        <div class="rv-cl-samples">${(c.sample_rows || []).map(buildSampleRow).join('')}</div>
      </div>
    </div>
  `;
}

function buildEmergentCard(c) {
  const chips = (c.evidence_chips || []).map(ch => `<span class="rv-cl-chip">${esc(ch)}</span>`).join('');
  const aiLine = c.ai_summary ? `<div class="rv-cl-ai">✦ ${esc(c.ai_summary)}</div>` : '';
  const suggest = c.suggested_test
    ? `<button class="rv-cl-btn suggest" onclick='genTestFromAnomaly(${JSON.stringify(c.suggested_test)})'>✦ Generate this test</button>`
    : '';
  return `
    <div class="rv-cl-card emergent band-${c.dominant_band}" id="clcard_${c.cluster_id}">
      <div class="rv-cl-head" onclick="toggleCluster('${c.cluster_id}')">
        <div class="rv-cl-rank emergent">${c.rank}</div>
        <div class="rv-cl-main">
          <div class="rv-cl-title">${esc(c.title)} <span class="rv-cl-sev ${c.severity}">${c.severity}</span></div>
          <div class="rv-cl-sub"><span class="rv-cl-desc">${esc(c.description || '')}</span></div>
          <div class="rv-cl-chips">${chips}</div>
          ${aiLine}
        </div>
        <div class="rv-cl-metrics">
          <div class="rv-cl-size">${fmt(c.size)}<span>invoices</span></div>
          <div class="rv-cl-amt">${c.amount_display}<span>at risk</span></div>
          <div class="rv-cl-band ${c.dominant_band}">${c.dominant_band}</div>
        </div>
        <div class="rv-cl-chevron" id="clchev_${c.cluster_id}">▾</div>
      </div>
      <div class="rv-cl-body" id="clbody_${c.cluster_id}" style="display:none">
        <div class="rv-cl-actions">
          <button class="rv-cl-btn confirm" onclick="clusterBulk('${c.cluster_id}','confirmed')">✓ Confirm all ${c.size}</button>
          <button class="rv-cl-btn fp" onclick="clusterBulk('${c.cluster_id}','false_positive')">✕ Dismiss all ${c.size}</button>
          ${suggest}
          <span class="rv-cl-samplenote">showing top ${Math.min((c.sample_rows||[]).length, c.size)} of ${c.size}</span>
        </div>
        <div class="rv-cl-samples">${(c.sample_rows || []).map(buildSampleRow).join('')}</div>
      </div>
    </div>
  `;
}

function genTestFromAnomaly(st) {
  if (!st || !st.prompt) return;
  localStorage.setItem('dia_prefill_test', st.prompt);
  window.location.href = '/#build';
}

async function loadSuggestions() {
  const btn = document.getElementById('suggestBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="rv-spinner-sm"></span> Thinking…';
  const body = document.getElementById('rvSuggestBody');
  try {
    const res = await fetch(`/api/runs/${currentRunId}/anomaly/suggest`, { method: 'POST' });
    const data = await res.json();
    const list = data.suggestions || [];
    if (!list.length) {
      body.innerHTML = '<div class="rv-suggest-empty">No test suggestions available.</div>';
    } else {
      body.innerHTML = list.map(s => `
        <div class="rv-suggest-card">
          <div class="rv-suggest-name">${esc(s.name || '')} <span class="rv-suggest-size">${fmt(s.size||0)} invoices</span></div>
          <div class="rv-suggest-desc">${esc(s.description || '')}</div>
          <div class="rv-suggest-prompt">${esc(s.prompt || '')}</div>
          <button class="rv-cl-btn suggest" onclick='genTestFromAnomaly(${JSON.stringify({prompt: s.prompt})})'>✦ Generate test</button>
        </div>
      `).join('');
    }
    if (data.disclaimer) body.innerHTML += `<div class="rv-disclaimer">${esc(data.disclaimer)}</div>`;
  } catch(e) {
    body.innerHTML = '<div class="rv-suggest-empty">Suggestion failed.</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Re-suggest';
  }
}

function buildSampleRow(s) {
  const evMap = {};
  (s.evidence || []).forEach(e => { evMap[e.field] = e; });

  function fld(label, field, val) {
    const ev = evMap[field];
    const v = val != null ? val : (s[field] || '—');
    if (ev) {
      return `<div class="rv-sr-fld highlighted"><span class="rv-sr-lbl">${label}</span>` +
             `<span class="rv-sr-val">${esc(String(v))}</span>` +
             `<span class="rv-sr-note">${esc(ev.note)}</span></div>`;
    }
    return `<div class="rv-sr-fld"><span class="rv-sr-lbl">${label}</span><span class="rv-sr-val">${esc(String(v))}</span></div>`;
  }

  const vendor = s.vendor_name || s.vendor_number || 'Unknown';
  const statusPill = s.review_status && s.review_status !== 'pending'
    ? `<span class="rv-sr-status ${s.review_status}">${statusLabel(s.review_status)}</span>` : '';

  return `
    <div class="rv-sr">
      <div class="rv-sr-score ${s.band_css}">${s.risk_score}</div>
      <div class="rv-sr-content">
        <div class="rv-sr-top">
          <span class="rv-sr-vendor">${esc(vendor)}</span>
          <span class="rv-sr-inv">${esc(s.physical_invoice_no || '—')}</span>
          ${statusPill}
        </div>
        <div class="rv-sr-flds">
          ${fld('Amount', 'amount_excl_lc', (s.amount_excl_lc || '—') + ' ' + (s.reference_currency || ''))}
          ${fld('Authorized', 'date_authorized')}
          ${fld('Captured', 'date_captured')}
          ${fld('Pay date', 'scheduled_pay_date')}
          ${fld('Description', 'invoice_description')}
        </div>
      </div>
    </div>
  `;
}

function toggleCluster(cid) {
  const body = document.getElementById(`clbody_${cid}`);
  const chev = document.getElementById(`clchev_${cid}`);
  const open = body.style.display === 'none';
  body.style.display = open ? 'block' : 'none';
  chev.textContent = open ? '▴' : '▾';
}

async function clusterBulk(cid, status) {
  const c = allClusters().find(x => x.cluster_id === cid);
  if (!c) return;
  const verb = status === 'confirmed' ? 'Confirm' : 'Dismiss';
  if (!confirm(`${verb} all ${c.size} invoices in "${c.title}"?`)) return;

  const res = await fetch(`/api/runs/${currentRunId}/review/bulk`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ row_indices: c.row_indices, status }),
  });
  if (!res.ok) { alert('Bulk action failed'); return; }

  // refresh dashboard + clusters
  const dRes = await fetch(`/api/runs/${currentRunId}/review?per_page=1`);
  if (dRes.ok) { const d = await dRes.json(); renderDashboard(d.dashboard); }
  await loadClusters();
  flatLoaded = false;
}

async function runTriage() {
  const btn = document.getElementById('triageBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="rv-spinner-sm"></span> Analyzing…';
  try {
    const res = await fetch(`/api/runs/${currentRunId}/ai/triage?group_by=${currentGroupBy}`, { method: 'POST' });
    const data = await res.json();
    if (data.error) {
      document.getElementById('rvTriageBody').style.display = 'block';
      document.getElementById('rvTriageBody').innerHTML =
        `<div class="rv-triage-error">${esc(data.error)}</div>`;
    } else {
      renderTriage(data);
      // merge cluster summaries into both boards
      if (data.cluster_summaries && clustersData) {
        allClusters().forEach(c => { c.ai_summary = data.cluster_summaries[c.cluster_id] || c.ai_summary; });
        renderClusterBoard(clustersData.rule_clusters || []);
        renderEmergentBoard(clustersData.emergent_clusters || []);
      }
    }
  } catch(e) {
    console.error(e);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Re-analyze';
  }
}

function renderTriage(ai) {
  const body = document.getElementById('rvTriageBody');
  body.style.display = 'block';
  const priorities = (ai.priorities || []).map(p => {
    const c = allClusters().find(x => x.cluster_id === p.cluster_id);
    const name = c ? c.title : p.cluster_id;
    return `<li><span class="rv-pri-name">${esc(name)}</span> — ${esc(p.reason)}</li>`;
  }).join('');
  body.innerHTML = `
    <div class="rv-triage-summary">${esc(ai.executive_summary || '')}</div>
    ${priorities ? `<div class="rv-triage-pri-title">Review these first:</div><ol class="rv-triage-pri">${priorities}</ol>` : ''}
  `;
  if (ai.disclaimer) {
    const d = document.getElementById('rvTriageDisclaimer');
    d.style.display = 'block';
    d.textContent = ai.disclaimer;
  }
  document.getElementById('triageBtn').textContent = 'Re-analyze';
}

async function fetchPage() {
  const params = buildParams();
  params.set('page', currentPage);
  params.set('per_page', PER_PAGE);

  const res = await fetch(`/api/runs/${currentRunId}/review?${params}`);
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();

  allRows       = data.rows || [];
  totalPages    = data.total_pages || 1;
  dashboardData = data.dashboard;

  // Build test filter checkboxes from available test IDs
  buildTestCheckboxes(data.test_ids || []);

  hide('rvLoading');
  renderDashboard(data.dashboard);
  renderList(allRows, data.total_filtered);
  renderPagination(data.page, data.total_pages);
  updateBulkButtons();
  show('rvKpiStrip'); show('rvFilters'); show('rvListPanel');

  if (activeRowKey) {
    const same = allRows.find(r => r.row_key === activeRowKey);
    if (same) { activeRow = same; renderDetail(same); }
  }
}

function buildTestCheckboxes(testIds) {
  const container = document.getElementById('testCheckboxes');
  // only rebuild if test list changed
  if (container.dataset.built === testIds.join(',')) return;
  container.dataset.built = testIds.join(',');
  container.innerHTML = testIds.map(id => `
    <label class="rv-chip-label">
      <input type="checkbox" name="test" value="${esc(id)}" onchange="applyFilters()"> ${esc(id)}
    </label>
  `).join('');
}

function buildParams() {
  const p = new URLSearchParams();
  const bands    = [...document.querySelectorAll('input[name="band"]:checked')].map(i => i.value);
  const tests    = [...document.querySelectorAll('input[name="test"]:checked')].map(i => i.value);
  const statuses = [...document.querySelectorAll('input[name="status"]:checked')].map(i => i.value);
  if (bands.length === 1) p.set('band', bands[0]);
  if (tests.length === 1) p.set('test_id', tests[0]);
  if (statuses.length === 1) p.set('status', statuses[0]);
  if (document.getElementById('overlapOnly').checked) p.set('overlap_only', 'true');
  if (document.getElementById('redFlagOnly').checked) p.set('red_flag_only', 'true');
  const vendor = document.getElementById('vendorSearch').value.trim();
  if (vendor) p.set('vendor', vendor);
  const minAmt = document.getElementById('minAmount').value;
  const maxAmt = document.getElementById('maxAmount').value;
  if (minAmt) p.set('min_amount', minAmt);
  if (maxAmt) p.set('max_amount', maxAmt);
  return p;
}

function applyFilters() { currentPage = 1; fetchPage(); }
function debounceFilters() { clearTimeout(filterDebounce); filterDebounce = setTimeout(applyFilters, 350); }
function clearFilters() {
  document.querySelectorAll('input[name="band"],input[name="test"]').forEach(i => i.checked = false);
  document.getElementById('overlapOnly').checked = false;
  document.getElementById('redFlagOnly').checked = false;
  document.getElementById('vendorSearch').value  = '';
  document.getElementById('minAmount').value     = '';
  document.getElementById('maxAmount').value     = '';
  applyFilters();
}
function filterBand(band) {
  if (currentView === 'clusters') setView('flat');
  document.querySelectorAll('input[name="band"]').forEach(i => i.checked = (i.value === band));
  applyFilters();
}

function renderDashboard(d) {
  if (!d) return;
  document.getElementById('kpiTotal').textContent         = fmt(d.total_hit_rows);
  document.getElementById('kpiCritical').textContent      = fmt(d.critical);
  document.getElementById('kpiHigh').textContent          = fmt(d.high);
  document.getElementById('kpiMedium').textContent        = fmt(d.medium);
  document.getElementById('kpiLow').textContent           = fmt(d.low);
  document.getElementById('kpiReviewedPct').textContent   = (d.reviewed_pct || 0) + '%';
  document.getElementById('kpiConfirmed').textContent     = fmt(d.confirmed || 0);
  document.getElementById('kpiFp').textContent            = fmt(d.false_positive || 0);
  document.getElementById('kpiAmountAtRisk').textContent  = fmtAmt(d.unreviewed_amount || 0);

  const pct = d.reviewed_pct || 0;
  if (pct > 0 || d.confirmed || d.false_positive) {
    show('rvProgress');
    document.getElementById('rvProgressFill').style.width  = pct + '%';
    document.getElementById('rvProgressLabel').textContent = pct + '% reviewed';
  }
}

function renderList(rows, total) {
  const container = document.getElementById('rvHitList');
  document.getElementById('rvListCount').textContent = `${fmt(total)} invoice${total !== 1 ? 's' : ''}`;

  if (!rows.length) {
    container.innerHTML = '<div style="padding:40px;text-align:center;color:#475569;font-size:13px">No results match the current filters</div>';
    return;
  }

  container.innerHTML = '';
  rows.forEach(row => container.appendChild(buildHitRow(row)));
}

function buildHitRow(row) {
  const div = document.createElement('div');
  div.className = `rv-hit-row${row.row_key === activeRowKey ? ' active' : ''}${row.review_status !== 'pending' ? ' reviewed' : ''}`;
  div.dataset.rowKey = row.row_key;

  const vendor = row.vendor_name || row.vendor_number || 'Unknown Vendor';
  const invNo  = row.physical_invoice_no || row.system_invoice_no || '—';
  const date   = row.date_authorized || row.date_documented || '—';

  const tagHtml = row.flagged_by.map(f => {
    const aiClass = f.type === 'generated' ? ' ai' : '';
    const rfClass = f.red_flag ? ' red-flag' : '';
    const label   = f.test_name || f.test_id;
    const rf      = f.red_flag ? ' ⚑' : '';
    const ai      = f.type === 'generated' ? ' ✦' : '';
    return `<span class="rv-tag${aiClass}${rfClass}" title="${esc(label)}">${esc(label.length > 18 ? label.substring(0,18)+'…' : label)}${rf}${ai}</span>`;
  }).join('');

  const overlapHtml = row.overlap_count >= 2
    ? `<span class="rv-overlap-badge">${row.overlap_count} tests</span>` : '';

  div.innerHTML = `
    <input type="checkbox" class="rv-hit-check" onclick="toggleRowSelect(event,'${row.row_key}')" ${selectedRows.has(row.row_key) ? 'checked' : ''}>
    <div class="rv-hit-score ${row.band_css}">${row.risk_score}</div>
    <div class="rv-hit-body">
      <div class="rv-hit-top">
        <div class="rv-hit-vendor">${esc(vendor)}</div>
        <div class="rv-hit-amount">${fmtAmt(row.amount)} <span style="font-size:10px;font-weight:400;color:#94a3b8">${esc(row.reference_currency||'')}</span></div>
      </div>
      <div class="rv-hit-meta-row">
        <div class="rv-hit-meta">${esc(invNo)} · ${esc(date)}</div>
        <div class="rv-hit-status ${row.review_status}">${statusLabel(row.review_status)}</div>
      </div>
      <div class="rv-hit-tags">${tagHtml} ${overlapHtml}</div>
    </div>
  `;

  div.addEventListener('click', e => { if (e.target.type === 'checkbox') return; selectRow(row); });
  return div;
}

function selectRow(row) {
  activeRowKey = row.row_key;
  activeRow    = row;
  document.querySelectorAll('.rv-hit-row').forEach(el => el.classList.toggle('active', el.dataset.rowKey === activeRowKey));
  renderDetail(row);
}

function renderDetail(row) {
  hide('rvDetailEmpty');
  const dc = document.getElementById('rvDetailContent');
  dc.style.display = 'block';

  const vendor = row.vendor_name || row.vendor_number || 'Unknown Vendor';

  const testHitsHtml = row.flagged_by.map(f => `
    <div class="rv-test-hit">
      <span class="rv-test-hit-id" title="${esc(f.test_id)}">${esc(f.test_id)}</span>
      <span class="rv-test-hit-name" title="${esc(f.test_name)}">${esc(f.test_name)}</span>
      <span class="rv-test-hit-wt">wt ${f.weightage}</span>
      ${f.red_flag ? '<span class="rv-test-hit-rf">⚑ RED FLAG</span>' : ''}
      ${f.type === 'generated' ? '<span class="rv-test-hit-ai">✦ AI</span>' : ''}
    </div>
  `).join('');

  // build evidence map: udm_field → {note}
  const evMap = {};
  (row.evidence || []).forEach(e => { evMap[e.field] = e; });

  const fieldDefs = [
    ['Vendor Number',   'vendor_number',        row.vendor_number],
    ['Invoice No',      'physical_invoice_no',   row.physical_invoice_no || row.system_invoice_no],
    ['Date Authorized', 'date_authorized',       row.date_authorized],
    ['Date Documented', 'date_documented',       row.date_documented],
    ['Date Captured',   'date_captured',         row.date_captured],
    ['Scheduled Pay',   'scheduled_pay_date',    row.scheduled_pay_date],
    ['Captured By',     'captured_by',           row.captured_by],
    ['Authorized By',   'authorized_by',         row.authorized_by],
    ['Amount (Excl.)',  'amount_excl_lc',        row.amount_excl_lc],
    ['Amount (Incl.)',  'amount_incl_lc',        row.amount_incl_lc],
    ['Currency',        'reference_currency',    row.reference_currency],
    ['Company Code',    'company_code',          row.company_code],
    ['Payment Type',    'payment_type',          row.payment_type],
    ['Fiscal Year',     'fiscal_year',           row.fiscal_year],
  ].filter(([, , v]) => v && String(v).toLowerCase() !== 'none' && String(v).toLowerCase() !== 'null');

  const kvHtml = fieldDefs.map(([label, field, rawVal]) => {
    const ev = evMap[field];
    const display = (field === 'amount_excl_lc' || field === 'amount_incl_lc')
      ? fmtAmt(parseFloat(String(rawVal).replace(',', '')))
      : esc(String(rawVal));
    if (ev) {
      return `<div class="rv-kv rv-kv-highlighted">
        <span class="rv-kv-label">${label}</span>
        <div class="rv-kv-right">
          <span class="rv-kv-val">${display}</span>
          <span class="rv-kv-note">${esc(ev.note)}</span>
        </div>
      </div>`;
    }
    return `<div class="rv-kv"><span class="rv-kv-label">${label}</span><div class="rv-kv-right"><span class="rv-kv-val">${display}</span></div></div>`;
  }).join('');

  const descEv = evMap['invoice_description'];
  const descHtml = row.invoice_description
    ? `<div class="rv-detail-section">
        <div class="rv-detail-section-title">Description${descEv ? ` <span class="rv-kv-note" style="margin-left:6px">${esc(descEv.note)}</span>` : ''}</div>
        <div style="font-size:12px;line-height:1.6;${descEv ? 'background:#fffbeb;border:1px solid #fde68a;border-radius:7px;padding:8px 10px;' : 'color:var(--text-secondary)'}">${esc(row.invoice_description)}</div>
       </div>` : '';

  const status = row.review_status || 'pending';
  const note   = row.review_note || '';

  dc.innerHTML = `
    <div class="rv-detail-head">
      <div class="rv-detail-score-row">
        <div class="rv-detail-score ${row.band_css}">${row.risk_score}</div>
        <div>
          <div class="rv-detail-vendor" style="cursor:pointer;color:var(--blue)" onclick="openVendorDrill('${esc(row.vendor_number||vendor)}')" title="View all invoices for this vendor">${esc(vendor)} ↗</div>
          <div class="rv-detail-sub">${row.band_label} Risk${row.overlap_count >= 2 ? ` · ${row.overlap_count} tests` : ''}</div>
        </div>
      </div>
    </div>

    <div class="rv-detail-section">
      <div class="rv-detail-section-title">Flagged By (${row.flagged_by.length} test${row.flagged_by.length !== 1 ? 's' : ''})</div>
      ${testHitsHtml}
    </div>

    <div class="rv-detail-section">
      <div class="rv-detail-section-title">Invoice Details</div>
      ${kvHtml}
    </div>

    ${descHtml}

    <div class="rv-action-strip">
      <div class="rv-action-title">Review Decision</div>
      <div class="rv-action-btns">
        <button class="rv-action-btn confirm${status==='confirmed'?' active':''}" onclick="setDecision('confirmed')">✓ Confirm</button>
        <button class="rv-action-btn fp${status==='false_positive'?' active':''}" onclick="setDecision('false_positive')">✕ False +</button>
        <button class="rv-action-btn needs-info${status==='needs_info'?' active':''}" onclick="setDecision('needs_info')">? Info</button>
        <button class="rv-action-btn reset${status==='pending'?' active':''}" onclick="setDecision('pending')">↺ Reset</button>
      </div>
      <textarea id="reviewNote" class="rv-note-input" placeholder="Add note (optional)…">${esc(note)}</textarea>
      <button class="rv-save-btn" onclick="saveReview()">Save Decision</button>
      <div class="rv-shortcut-hint">C = Confirm · F = False Positive · N = Needs Info · ↓ = Next</div>
    </div>
  `;
  dc._pendingStatus = status;
}

function clearDetail() {
  activeRow = null; show('rvDetailEmpty');
  const dc = document.getElementById('rvDetailContent');
  dc.style.display = 'none';
}

function setDecision(status) {
  const dc = document.getElementById('rvDetailContent');
  dc._pendingStatus = status;
  dc.querySelectorAll('.rv-action-btn').forEach(b => b.classList.remove('active'));
  const map = { confirmed:'confirm', false_positive:'fp', needs_info:'needs-info', pending:'reset' };
  dc.querySelector(`.rv-action-btn.${map[status]}`)?.classList.add('active');
}

async function saveReview() {
  if (!activeRow || !currentRunId) return;
  const dc     = document.getElementById('rvDetailContent');
  const status = dc._pendingStatus || 'pending';
  const note   = document.getElementById('reviewNote').value.trim();

  await fetch(`/api/runs/${currentRunId}/review/${activeRow.row_idx}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status, note }),
  });

  activeRow.review_status = status;
  activeRow.review_note   = note;

  const listEl = document.querySelector(`.rv-hit-row[data-row-key="${CSS.escape(activeRow.row_key)}"]`);
  if (listEl) {
    const statusEl = listEl.querySelector('.rv-hit-status');
    if (statusEl) { statusEl.className = `rv-hit-status ${status}`; statusEl.textContent = statusLabel(status); }
    listEl.classList.toggle('reviewed', status !== 'pending');
  }

  // refresh KPI strip
  const res = await fetch(`/api/runs/${currentRunId}/review?per_page=1`);
  if (res.ok) { const d = await res.json(); renderDashboard(d.dashboard); }

  advanceToNext();
}

function toggleRowSelect(event, rowKey) {
  event.stopPropagation();
  if (selectedRows.has(rowKey)) selectedRows.delete(rowKey); else selectedRows.add(rowKey);
  updateBulkButtons();
}

function toggleSelectAll(checked) {
  allRows.forEach(r => { if (checked) selectedRows.add(r.row_key); else selectedRows.delete(r.row_key); });
  document.querySelectorAll('.rv-hit-check').forEach(cb => cb.checked = checked);
  updateBulkButtons();
}

function updateBulkButtons() {
  const has = selectedRows.size > 0;
  document.getElementById('bulkFpBtn').style.display      = has ? '' : 'none';
  document.getElementById('bulkConfirmBtn').style.display = has ? '' : 'none';
  if (has) {
    document.getElementById('bulkConfirmBtn').textContent = `Confirm (${selectedRows.size})`;
    document.getElementById('bulkFpBtn').textContent      = `Mark FP (${selectedRows.size})`;
  }
}

async function bulkAction(status) {
  if (!selectedRows.size || !currentRunId) return;
  await Promise.all([...selectedRows].map(rk => {
    const row = allRows.find(r => r.row_key === rk);
    return fetch(`/api/runs/${currentRunId}/review/${row?.row_idx ?? rk}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, note: '' }),
    });
  }));
  selectedRows.clear();
  document.getElementById('selectAllVisible').checked = false;
  updateBulkButtons();
  fetchPage();
}

function renderPagination(page, total) {
  const wrap = document.getElementById('rvPagination');
  if (total <= 1) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'flex';
  document.getElementById('pageInfo').textContent = `Page ${page} of ${total}`;
  document.getElementById('prevBtn').disabled = page <= 1;
  document.getElementById('nextBtn').disabled = page >= total;
}

function changePage(delta) {
  const next = currentPage + delta;
  if (next < 1 || next > totalPages) return;
  currentPage = next;
  clearDetail();
  fetchPage();
}

function handleKeyboard(e) {
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  if (!activeRow) return;
  const map = { c:'confirmed', f:'false_positive', n:'needs_info' };
  if (map[e.key?.toLowerCase()]) { setDecision(map[e.key.toLowerCase()]); saveReview(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); advanceToNext(); }
  else if (e.key === 'ArrowUp')   { e.preventDefault(); advanceToPrev(); }
}

function advanceToNext() {
  if (!activeRowKey || !allRows.length) return;
  const idx = allRows.findIndex(r => r.row_key === activeRowKey);
  if (idx < allRows.length - 1) selectRow(allRows[idx + 1]);
  else if (currentPage < totalPages) { currentPage++; fetchPage(); }
}
function advanceToPrev() {
  if (!activeRowKey || !allRows.length) return;
  const idx = allRows.findIndex(r => r.row_key === activeRowKey);
  if (idx > 0) selectRow(allRows[idx - 1]);
}

// ── Feature 4: Vendor drill-down ──────────────────────────────────────────
async function openVendorDrill(vendorNumber) {
  if (!currentRunId || !vendorNumber) return;
  const res = await fetch(`/api/runs/${currentRunId}/vendor/${encodeURIComponent(vendorNumber)}`);
  if (!res.ok) return;
  const d = await res.json();

  const existing = document.getElementById('vendorModal');
  if (existing) existing.remove();

  const rows = (d.invoices || []).map(r => `
    <tr style="${r.flagged ? 'background:#fff5f5' : ''}">
      <td>${esc(r.physical_invoice_no || r.system_invoice_no || '—')}</td>
      <td>${esc(r.date_authorized || r.date_documented || '—')}</td>
      <td style="text-align:right;font-weight:600">${esc(r.amount_excl_lc || '—')}</td>
      <td>${esc(r.invoice_description?.substring(0,60) || '—')}</td>
      <td>${r.flagged ? '<span style="color:var(--red);font-weight:700">⚑ Flagged</span>' : '<span style="color:var(--green)">✓ Clean</span>'}</td>
    </tr>
  `).join('');

  const modal = document.createElement('div');
  modal.id = 'vendorModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;display:flex;align-items:center;justify-content:center;padding:20px';
  modal.innerHTML = `
    <div style="background:#fff;border-radius:14px;width:100%;max-width:820px;max-height:85vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.2)">
      <div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px">
        <div style="flex:1">
          <div style="font-size:15px;font-weight:800;color:var(--text-primary)">${esc(d.vendor_name || d.vendor_number)}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:2px">${d.invoice_count} invoices · ${fmtAmt(d.total_amount)} total · <span style="color:var(--red)">${d.flagged_count} flagged</span></div>
        </div>
        <button onclick="document.getElementById('vendorModal').remove()" style="background:#f1f5f9;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px;font-weight:600">✕ Close</button>
      </div>
      <div style="overflow-y:auto;padding:0">
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr style="background:#f8fafc;position:sticky;top:0">
            <th style="padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)">Invoice No</th>
            <th style="padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)">Date</th>
            <th style="padding:8px 12px;text-align:right;border-bottom:1px solid var(--border)">Amount</th>
            <th style="padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)">Description</th>
            <th style="padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)">Status</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

// ── Feature 5: Export ──────────────────────────────────────────────────────
function exportFindings(status = 'confirmed') {
  if (!currentRunId) return;
  window.location.href = `/api/runs/${currentRunId}/export?status=${status}`;
}

function fmt(n) { return n?.toLocaleString() ?? '—'; }
function fmtAmt(n) {
  if (!n) return '$0';
  const a = Math.abs(n);
  if (a >= 1e9) return `$${(n/1e9).toFixed(1)}B`;
  if (a >= 1e6) return `$${(n/1e6).toFixed(1)}M`;
  if (a >= 1e3) return `$${(n/1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}
function statusLabel(s) {
  return { pending:'Pending', confirmed:'Confirmed', false_positive:'False +', needs_info:'Needs Info' }[s] || s;
}
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function show(id) { const el = document.getElementById(id); if (el) el.style.display = ''; }
function hide(id) { const el = document.getElementById(id); if (el) el.style.display = 'none'; }
