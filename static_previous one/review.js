/* ── State ───────────────────────────────────────────────────────────────── */
let currentRunId   = null;
let currentPage    = 1;
let totalPages     = 1;
let allRows        = [];          // current page rows
let selectedRows   = new Set();
let activeRowKey   = null;
let activeRow      = null;
let filterDebounce = null;
let dashboardData  = null;

const PER_PAGE = 50;

const TEST_IDS = ["AP1","AP2","AP3","AP4","AP5","AP6","AP7","AP8",
                  "AP9","AP11","AP12","AP15","AP19","AP21","AP22","AP23"];

/* ── Init ────────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', async () => {
  buildTestCheckboxes();
  document.addEventListener('keydown', handleKeyboard);
  await loadRuns();
});

async function loadRuns() {
  try {
    const res = await fetch('/api/runs?limit=50');
    if (!res.ok) return;
    const data = await res.json();
    const runs = Array.isArray(data) ? data : (data.runs || data);
    const sel = document.getElementById('runSelect');
    sel.innerHTML = '<option value="">Select a run…</option>';
    runs.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.run_id || r.id;
      const ts = r.created_at ? new Date(r.created_at).toLocaleString() : '';
      const file = r.uploaded_file || r.filename || r.run_id || r.id;
      opt.textContent = `${file}  —  ${r.total_rows || ''} rows  ${ts}`;
      sel.appendChild(opt);
    });
    if (runs.length >= 1) {
      const firstId = runs[0].run_id || runs[0].id;
      sel.value = firstId;
      await loadRun(firstId);
    } else {
      show('rvEmpty');
    }
  } catch(e) { console.error('loadRuns', e); }
}

function buildTestCheckboxes() {
  const container = document.getElementById('testCheckboxes');
  TEST_IDS.forEach(id => {
    const label = document.createElement('label');
    label.className = 'rv-chip-label';
    label.innerHTML = `<input type="checkbox" name="test" value="${id}" onchange="applyFilters()"> ${id}`;
    container.appendChild(label);
  });
}

/* ── Load run ─────────────────────────────────────────────────────────────── */
async function loadRun(runId) {
  if (!runId) { show('rvEmpty'); return; }
  currentRunId = runId;
  currentPage  = 1;
  selectedRows.clear();
  activeRowKey = null;
  clearDetail();

  hide('rvEmpty');
  hide('rvKpiStrip');
  hide('rvFilters');
  hide('rvListPanel');
  hide('rvDetailEmpty');
  show('rvLoading');

  try {
    await fetchPage();
  } catch(e) {
    console.error('loadRun error', e);
    hide('rvLoading');
    show('rvEmpty');
  }
}

/* ── Fetch page ──────────────────────────────────────────────────────────── */
async function fetchPage() {
  if (!currentRunId) return;

  const params = buildParams();
  params.set('page', currentPage);
  params.set('per_page', PER_PAGE);

  try {
    const res = await fetch(`/api/runs/${currentRunId}/review-data?${params}`);
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    allRows    = data.rows || [];
    totalPages = data.total_pages || 1;
    dashboardData = data.dashboard;

    hide('rvLoading');
    renderDashboard(data.dashboard);
    renderList(allRows, data.total_filtered);
    renderPagination(data.page, data.total_pages, data.total_filtered);
    updateBulkButtons();

    show('rvKpiStrip');
    show('rvFilters');
    show('rvListPanel');

    // re-render detail if same row is still in page
    if (activeRowKey) {
      const same = allRows.find(r => r.row_key === activeRowKey);
      if (same) { activeRow = same; renderDetail(same); }
    }
  } catch(e) {
    console.error('fetchPage', e);
    hide('rvLoading');
    throw e;   // bubble up to loadRun's catch
  }
}

/* ── Build query params from filters ─────────────────────────────────────── */
function buildParams() {
  const p = new URLSearchParams();

  const bands   = [...document.querySelectorAll('input[name="band"]:checked')].map(i => i.value);
  const statuses = [...document.querySelectorAll('input[name="status"]:checked')].map(i => i.value);
  const tests   = [...document.querySelectorAll('input[name="test"]:checked')].map(i => i.value);

  if (bands.length === 1)    p.set('band', bands[0]);
  if (statuses.length === 1) p.set('status', statuses[0]);
  if (tests.length > 0)      p.set('tests', tests.join(','));

  if (document.getElementById('overlapOnly').checked)  p.set('overlap_only', 'true');
  if (document.getElementById('redFlagOnly').checked)  p.set('red_flag_only', 'true');

  const vendor = document.getElementById('vendorSearch').value.trim();
  if (vendor) p.set('vendor', vendor);

  const minAmt = document.getElementById('minAmount').value;
  const maxAmt = document.getElementById('maxAmount').value;
  if (minAmt) p.set('min_amount', minAmt);
  if (maxAmt) p.set('max_amount', maxAmt);

  return p;
}

function applyFilters() {
  currentPage = 1;
  fetchPage();
}

function debounceFilters() {
  clearTimeout(filterDebounce);
  filterDebounce = setTimeout(applyFilters, 350);
}

function clearFilters() {
  document.querySelectorAll('input[name="band"]').forEach(i => i.checked = false);
  document.querySelectorAll('input[name="status"]').forEach(i => i.checked = false);
  document.querySelectorAll('input[name="test"]').forEach(i => i.checked = false);
  document.getElementById('overlapOnly').checked  = false;
  document.getElementById('redFlagOnly').checked  = false;
  document.getElementById('vendorSearch').value   = '';
  document.getElementById('minAmount').value      = '';
  document.getElementById('maxAmount').value      = '';
  applyFilters();
}

function filterBand(band) {
  // toggle just this band
  document.querySelectorAll('input[name="band"]').forEach(i => { i.checked = (i.value === band); });
  applyFilters();
}

/* ── Dashboard ───────────────────────────────────────────────────────────── */
function renderDashboard(d) {
  if (!d) return;
  document.getElementById('kpiTotal').textContent    = fmt(d.total_hit_rows);
  document.getElementById('kpiCritical').textContent = fmt(d.critical);
  document.getElementById('kpiHigh').textContent     = fmt(d.high);
  document.getElementById('kpiMedium').textContent   = fmt(d.medium);
  document.getElementById('kpiLow').textContent      = fmt(d.low);
  document.getElementById('kpiReviewedPct').textContent = (d.reviewed_pct || 0) + '%';
  document.getElementById('kpiConfirmed').textContent   = fmt(d.confirmed || 0);
  document.getElementById('kpiFp').textContent          = fmt(d.false_positive || 0);
  document.getElementById('kpiAmountAtRisk').textContent = fmtAmt(d.unreviewed_amount || 0);

  // progress bar
  const pct = d.reviewed_pct || 0;
  const pw = document.getElementById('rvProgress');
  if (pct > 0 || d.confirmed || d.false_positive || d.needs_info) {
    pw.style.display = 'flex';
    document.getElementById('rvProgressFill').style.width = pct + '%';
    document.getElementById('rvProgressLabel').textContent = pct + '% reviewed';
  }
}

/* ── Hit list ────────────────────────────────────────────────────────────── */
function renderList(rows, total) {
  const container = document.getElementById('rvHitList');
  const countEl   = document.getElementById('rvListCount');
  countEl.textContent = `${fmt(total)} invoice${total !== 1 ? 's' : ''}`;

  if (!rows.length) {
    container.innerHTML = '<div style="padding:40px;text-align:center;color:#475569;font-size:13px">No results match the current filters</div>';
    return;
  }

  container.innerHTML = '';
  rows.forEach(row => {
    const el = buildHitRow(row);
    container.appendChild(el);
  });
}

function buildHitRow(row) {
  const div = document.createElement('div');
  div.className = `rv-hit-row${row.row_key === activeRowKey ? ' active' : ''}${row.review_status !== 'pending' ? ' reviewed' : ''}`;
  div.dataset.rowKey = row.row_key;

  const vendor = row.vendor_name || row.vendor_number || 'Unknown Vendor';
  const invNo  = row.physical_invoice_no || row.system_invoice_no || '—';
  const date   = row.date_authorized || row.date_documented || '—';
  const curr   = row.reference_currency || '';

  const tagHtml = row.flagged_by.map(f =>
    `<span class="rv-tag${f.red_flag ? ' red-flag' : ''}">${f.test_id}${f.red_flag ? ' ⚑' : ''}</span>`
  ).join('');

  const overlapHtml = row.overlap_count >= 2
    ? `<span class="rv-overlap-badge">${row.overlap_count} tests</span>` : '';

  div.innerHTML = `
    <input type="checkbox" class="rv-hit-check" onclick="toggleRowSelect(event,'${row.row_key}')"
           ${selectedRows.has(row.row_key) ? 'checked' : ''}>
    <div class="rv-hit-score ${row.band_css}">${row.risk_score}</div>
    <div class="rv-hit-body">
      <div class="rv-hit-top">
        <div class="rv-hit-vendor">${esc(vendor)}</div>
        <div class="rv-hit-amount">${fmtAmt(row.amount)}${curr ? ' ' + curr : ''}</div>
      </div>
      <div class="rv-hit-meta-row">
        <div class="rv-hit-meta">${esc(invNo)} · ${esc(date)}</div>
        <div class="rv-hit-status ${row.review_status}">${statusLabel(row.review_status)}</div>
      </div>
      <div class="rv-hit-tags">${tagHtml} ${overlapHtml}</div>
    </div>
  `;

  div.addEventListener('click', (e) => {
    if (e.target.type === 'checkbox') return;
    selectRow(row);
  });

  return div;
}

function selectRow(row) {
  activeRowKey = row.row_key;
  activeRow    = row;
  // update active class
  document.querySelectorAll('.rv-hit-row').forEach(el => {
    el.classList.toggle('active', el.dataset.rowKey === activeRowKey);
  });
  renderDetail(row);
}

/* ── Detail panel ────────────────────────────────────────────────────────── */
function renderDetail(row) {
  hide('rvDetailEmpty');
  const dc = document.getElementById('rvDetailContent');
  dc.style.display = 'block';

  const vendor = row.vendor_name || row.vendor_number || 'Unknown Vendor';

  const testHitsHtml = row.flagged_by.map(f => `
    <div class="rv-test-hit">
      <span class="rv-test-hit-id">${f.test_id}</span>
      <span class="rv-test-hit-name">${esc(f.test_name)}</span>
      <span class="rv-test-hit-wt">wt ${f.weightage}</span>
      ${f.red_flag ? '<span class="rv-test-hit-rf">⚑ RED FLAG</span>' : ''}
    </div>
  `).join('');

  const invoiceFields = [
    ['Vendor Number',    row.vendor_number],
    ['Invoice No',       row.physical_invoice_no || row.system_invoice_no],
    ['Date Authorized',  row.date_authorized],
    ['Date Documented',  row.date_documented],
    ['Date Captured',    row.date_captured],
    ['Scheduled Pay',    row.scheduled_pay_date],
    ['Amount (Excl.)',   row.amount_excl_lc ? fmtAmt(parseFloat(row.amount_excl_lc)) : '—'],
    ['Amount (Incl.)',   row.amount_incl_lc ? fmtAmt(parseFloat(row.amount_incl_lc)) : '—'],
    ['Currency',         row.reference_currency],
    ['Company Code',     row.company_code],
    ['Payment Type',     row.payment_type],
    ['Fiscal Year',      row.fiscal_year],
  ].filter(([, v]) => v && v !== 'None' && v !== 'null');

  const kvHtml = invoiceFields.map(([k, v]) =>
    `<div class="rv-kv"><span class="rv-kv-label">${k}</span><span class="rv-kv-val">${esc(String(v))}</span></div>`
  ).join('');

  const descHtml = row.invoice_description
    ? `<div class="rv-detail-section">
        <div class="rv-detail-section-title">Description</div>
        <div style="font-size:12px;color:#94a3b8;line-height:1.5">${esc(row.invoice_description)}</div>
       </div>` : '';

  const note = row.review_note || '';
  const status = row.review_status || 'pending';

  dc.innerHTML = `
    <div class="rv-detail-head">
      <div class="rv-detail-score-row">
        <div class="rv-detail-score ${row.band_css}">${row.risk_score}</div>
        <div>
          <div class="rv-detail-vendor">${esc(vendor)}</div>
          <div class="rv-detail-sub">${row.band_label} Risk${row.overlap_count >= 2 ? ` &nbsp;·&nbsp; ${row.overlap_count} tests` : ''}</div>
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
        <button class="rv-action-btn confirm${status === 'confirmed' ? ' active' : ''}"
                onclick="setDecision('confirmed')">✓ Confirm</button>
        <button class="rv-action-btn fp${status === 'false_positive' ? ' active' : ''}"
                onclick="setDecision('false_positive')">✕ False Positive</button>
        <button class="rv-action-btn needs-info${status === 'needs_info' ? ' active' : ''}"
                onclick="setDecision('needs_info')">? Needs Info</button>
        <button class="rv-action-btn reset${status === 'pending' ? ' active' : ''}"
                onclick="setDecision('pending')">↺ Reset</button>
      </div>
      <textarea id="reviewNote" class="rv-note-input" placeholder="Add note (optional)…">${esc(note)}</textarea>
      <button class="rv-save-btn" onclick="saveReview()">Save Decision</button>
      <div class="rv-shortcut-hint">C = Confirm &nbsp;·&nbsp; F = False Positive &nbsp;·&nbsp; N = Needs Info &nbsp;·&nbsp; ↓ = Next</div>
    </div>
  `;

  // store pending decision state
  dc._pendingStatus = status;
}

function clearDetail() {
  activeRow = null;
  show('rvDetailEmpty');
  const dc = document.getElementById('rvDetailContent');
  dc.style.display = 'none';
}

function setDecision(status) {
  const dc = document.getElementById('rvDetailContent');
  dc._pendingStatus = status;
  // highlight the active button
  dc.querySelectorAll('.rv-action-btn').forEach(btn => {
    btn.classList.remove('active');
  });
  const map = { confirmed: 'confirm', false_positive: 'fp', needs_info: 'needs-info', pending: 'reset' };
  const btn = dc.querySelector(`.rv-action-btn.${map[status]}`);
  if (btn) btn.classList.add('active');
}

async function saveReview() {
  if (!activeRow || !currentRunId) return;
  const dc     = document.getElementById('rvDetailContent');
  const status = dc._pendingStatus || 'pending';
  const note   = document.getElementById('reviewNote').value.trim();

  try {
    const res = await fetch(
      `/api/runs/${currentRunId}/review/${encodeURIComponent(activeRow.row_key)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status, note, row_idx: activeRow.row_idx }),
      }
    );
    if (!res.ok) throw new Error(await res.text());

    // update local state
    activeRow.review_status = status;
    activeRow.review_note   = note;

    // update the list item visually without full reload
    const listEl = document.querySelector(`.rv-hit-row[data-row-key="${CSS.escape(activeRow.row_key)}"]`);
    if (listEl) {
      const statusEl = listEl.querySelector('.rv-hit-status');
      if (statusEl) { statusEl.className = `rv-hit-status ${status}`; statusEl.textContent = statusLabel(status); }
      listEl.classList.toggle('reviewed', status !== 'pending');
    }

    // refresh dashboard stats
    fetchDashboardOnly();

    // auto-advance to next row
    advanceToNext();
  } catch(e) { console.error('saveReview', e); }
}

async function fetchDashboardOnly() {
  try {
    const res = await fetch(`/api/runs/${currentRunId}/review-stats`);
    if (!res.ok) return;
    const d = await res.json();
    // merge with existing dashboard
    if (dashboardData) {
      Object.assign(dashboardData, d);
      renderDashboard(dashboardData);
    }
  } catch(e) {}
}

/* ── Bulk actions ─────────────────────────────────────────────────────────── */
function toggleRowSelect(event, rowKey) {
  event.stopPropagation();
  if (selectedRows.has(rowKey)) selectedRows.delete(rowKey);
  else selectedRows.add(rowKey);
  updateBulkButtons();
}

function toggleSelectAll(checked) {
  allRows.forEach(r => { if (checked) selectedRows.add(r.row_key); else selectedRows.delete(r.row_key); });
  document.querySelectorAll('.rv-hit-check').forEach(cb => cb.checked = checked);
  updateBulkButtons();
}

function updateBulkButtons() {
  const hasSel = selectedRows.size > 0;
  document.getElementById('bulkFpBtn').style.display      = hasSel ? '' : 'none';
  document.getElementById('bulkConfirmBtn').style.display = hasSel ? '' : 'none';
  if (hasSel) {
    document.getElementById('bulkConfirmBtn').textContent = `Confirm (${selectedRows.size})`;
    document.getElementById('bulkFpBtn').textContent      = `False Positive (${selectedRows.size})`;
  }
}

async function bulkAction(status) {
  if (!selectedRows.size || !currentRunId) return;
  const updates = [...selectedRows].map(rk => {
    const row = allRows.find(r => r.row_key === rk);
    return { row_key: rk, row_idx: row ? row.row_idx : 0, status, note: '' };
  });
  try {
    await fetch(`/api/runs/${currentRunId}/review/bulk`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates }),
    });
    selectedRows.clear();
    document.getElementById('selectAllVisible').checked = false;
    updateBulkButtons();
    fetchPage();
  } catch(e) { console.error('bulkAction', e); }
}

/* ── Pagination ───────────────────────────────────────────────────────────── */
function renderPagination(page, total, totalFiltered) {
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

/* ── Keyboard shortcuts ───────────────────────────────────────────────────── */
function handleKeyboard(e) {
  // don't fire when typing in inputs
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  if (!activeRow) return;

  const map = { c: 'confirmed', f: 'false_positive', n: 'needs_info' };
  if (map[e.key.toLowerCase()]) {
    setDecision(map[e.key.toLowerCase()]);
    saveReview();
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    advanceToNext();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    advanceToPrev();
  }
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

/* ── Helpers ─────────────────────────────────────────────────────────────── */
function fmt(n) { return n?.toLocaleString() ?? '—'; }
function fmtAmt(n) {
  if (!n) return '$0';
  const abs = Math.abs(n);
  if (abs >= 1e9) return `$${(n/1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `$${(n/1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `$${(n/1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

function statusLabel(s) {
  return { pending: 'Pending', confirmed: 'Confirmed', false_positive: 'False +', needs_info: 'Needs Info' }[s] || s;
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function show(id) { const el = document.getElementById(id); if (el) el.style.display = ''; }
function hide(id) { const el = document.getElementById(id); if (el) el.style.display = 'none'; }
