let llmOn = false;

// ── Sidebar nav ──────────────────────────────────────────────────────────────
function sbNav(el) {
  document.querySelectorAll('.sb-link').forEach(l => l.classList.remove('active'));
  el.classList.add('active');
}

// ── Drag & drop ──────────────────────────────────────────────────────────────
const zone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');

zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('dragover');
  const f = e.dataTransfer.files[0];
  if (f) setFile(f);
});
fileInput.addEventListener('change', () => { if (fileInput.files[0]) setFile(fileInput.files[0]); });

function setFile(f) {
  document.getElementById('dropHint').textContent = 'File selected:';
  document.getElementById('dropName').textContent = f.name;
  zone.style.borderColor = '#2563eb';
}

function toggleLLM() {
  llmOn = !llmOn;
  document.getElementById('llmToggle').classList.toggle('on', llmOn);
}

// ── Run validation ───────────────────────────────────────────────────────────
async function runValidation() {
  const file = fileInput.files[0];
  if (!file) { flash('Please choose a CSV file first.'); return; }

  const btn = document.getElementById('runBtn');
  const msg = document.getElementById('statusMsg');
  btn.disabled = true;
  msg.innerHTML = '<span class="spinner"></span> Validating...';

  try {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('use_llm', llmOn ? 'true' : 'false');
    fd.append('erp_type', document.getElementById('erpType').value);
    const bgMode = document.getElementById('bgMode')?.checked;
    let data;
    if (bgMode) {
      const queued = await fetch('/api/validate/async', { method: 'POST', body: fd }).then(r => r.json());
      if (!queued.job_id) throw new Error(queued.error || 'Failed to queue job');
      data = await waitForJobResult(queued.job_id, msg);
    } else {
      const res = await fetch('/api/validate', { method: 'POST', body: fd });
      data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Validation failed');
    }

    render(data);
    msg.textContent = 'Done.';
    document.getElementById('results').style.display = 'block';
    document.getElementById('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
    updatePendingFromResult(data);
    updateSidebarRunInfo(data, file.name);
    document.getElementById('topbarTitle').textContent = file.name;
    document.getElementById('newRunBtn').style.display = 'block';
    // persist so Back from review page restores this run
    if (data.run_id) {
      localStorage.setItem('dia_last_run_id', data.run_id);
      localStorage.setItem('dia_last_run_file', file.name);
    }
  } catch (e) {
    msg.textContent = '✕ ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

function updateSidebarRunInfo(data, fileName) {
  const el = document.getElementById('sbRunInfo');
  if (!el) return;
  el.style.display = 'block';
  document.getElementById('sbRunId').textContent = data.run_id || '—';
  document.getElementById('sbRunFile').textContent = fileName || '—';
  const rows = (data.summary || {}).total_rows;
  const rowsEl = document.getElementById('sbRunRows');
  if (rowsEl && rows !== undefined) rowsEl.textContent = rows.toLocaleString() + ' rows';
}

function flash(msg) {
  const el = document.getElementById('statusMsg');
  el.textContent = msg;
  setTimeout(() => el.textContent = '', 3000);
}

function startNewRun() {
  localStorage.removeItem('dia_last_run_id');
  localStorage.removeItem('dia_last_run_file');
  // hide results and reset UI to clean upload state
  document.getElementById('results').style.display = 'none';
  document.getElementById('sbRunInfo').style.display = 'none';
  document.getElementById('topbarTitle').textContent = 'Invoice Validation & Analytics';
  document.getElementById('newRunBtn').style.display = 'none';
  const reviewLink = document.getElementById('reviewLink');
  if (reviewLink) reviewLink.style.display = 'none';
  // reset file input
  document.getElementById('fileInput').value = '';
  document.getElementById('dropHint').textContent = 'Click to choose a CSV file, or drag & drop';
  document.getElementById('dropName').textContent = '';
  document.getElementById('dropZone').style.borderColor = '';
  document.getElementById('statusMsg').textContent = '';
  window.scrollTo(0, 0);
}

async function waitForJobResult(jobId, msgEl) {
  for (let i = 0; i < 600; i++) {
    const job = await fetch(`/api/jobs/${jobId}`).then(r => r.json());
    if (job.error) throw new Error(job.error);
    const p = Number(job.progress_pct || 0);
    msgEl.innerHTML = `<span class="spinner"></span> ${job.message || 'Processing'} (${p}%)`;
    if (job.status === 'completed') return job.result;
    if (job.status === 'failed') throw new Error(job.error || 'Job failed');
    await new Promise(resolve => setTimeout(resolve, 800));
  }
  throw new Error('Job timed out');
}

// ── Collapse/expand ──────────────────────────────────────────────────────────
function toggle(head) {
  const body = head.nextElementSibling;
  const isOpen = head.classList.contains('open');
  if (isOpen) {
    body.style.display = 'none';
    head.classList.remove('open');
  } else {
    body.style.display = '';
    head.classList.add('open');
  }
}

// ── Render all ───────────────────────────────────────────────────────────────
function render(data) {
  renderSummary(data);
  renderMapping(data);
  renderCompleteness(data);
  renderIssues(data);
  renderAnalyticsTests(data);
  renderOpsDashboards();
}

async function renderOpsDashboards() {
  try {
    const [ops, trends] = await Promise.all([
      fetch('/api/analytics/ops-metrics?limit_runs=30').then(r => r.json()),
      fetch('/api/analytics/vendor-trends?limit_runs=20&top_n=12').then(r => r.json())
    ]);

    document.getElementById('opsSection').style.display = 'block';
    document.getElementById('opsMeta').textContent = `${ops.runs_analyzed || 0} runs analyzed`;
    document.getElementById('opsAvgRuntime').textContent = `${Math.round(ops.avg_duration_ms || 0)} ms`;
    document.getElementById('opsParseRate').textContent = `${ops.parse_error_rate_pct || 0}%`;
    document.getElementById('opsCoverage').textContent = `${ops.avg_mapping_coverage_pct || 0}%`;

    const alerts = ops.alerts || [];
    document.getElementById('opsAlerts').innerHTML = alerts.length
      ? alerts.map(a => `<div style="background:#fff7ed;border:1px solid #fdba74;padding:8px 10px;border-radius:8px;font-size:12px;color:#9a3412"><strong>${a.code}</strong> · ${a.message}</div>`).join('')
      : '<div style="background:#f0fdf4;border:1px solid #86efac;padding:8px 10px;border-radius:8px;font-size:12px;color:#166534">No operational alerts in recent runs.</div>';

    const phaseRows = Object.entries(ops.phase_avg_ms || {});
    document.getElementById('opsPhaseBody').innerHTML = phaseRows.length
      ? phaseRows.map(([k,v]) => `<tr><td><code>${k}</code></td><td>${Math.round(v)}</td></tr>`).join('')
      : '<tr><td colspan="2" class="empty">No phase data yet</td></tr>';

    document.getElementById('opsVendorBody').innerHTML = (trends.top_vendors || []).length
      ? trends.top_vendors.map(v => `<tr><td><code>${v.vendor_number}</code></td><td>${v.invoice_count}</td><td>${fmtNum(v.amount_total)}</td><td>${v.run_count}</td></tr>`).join('')
      : '<tr><td colspan="4" class="empty">No vendor data yet</td></tr>';

    document.getElementById('opsSpikeBody').innerHTML = (trends.month_spikes || []).length
      ? trends.month_spikes.map(s => `<tr><td>${s.month}</td><td><code>${s.vendor_number}</code></td><td>${fmtNum(s.amount)}</td><td>${s.spike_ratio}x</td></tr>`).join('')
      : '<tr><td colspan="4" class="empty">No spikes detected</td></tr>';
  } catch (e) {
    console.error(e);
  }
}

function renderSummary(data) {
  const s = data.summary || {};
  const sm = data.schema_mapping || {};

  document.getElementById('kTotal').textContent = s.total_rows ?? 0;
  document.getElementById('kClean').textContent = s.rows_clean ?? 0;
  document.getElementById('kErrRows').textContent = s.rows_with_errors ?? 0;
  document.getElementById('kErrors').textContent = s.errors ?? 0;
  document.getElementById('kWarn').textContent = s.warnings ?? 0;

  const pct = s.pass_rate_pct ?? 0;
  document.getElementById('ringPct').textContent = pct + '%';
  const circ = 2 * Math.PI * 46;
  document.getElementById('ringFill').setAttribute('stroke-dashoffset', circ - (circ * pct / 100));
  document.getElementById('ringFill').setAttribute('stroke', pct >= 90 ? '#059669' : pct >= 70 ? '#d97706' : '#dc2626');

  document.getElementById('summaryMeta').textContent =
    `${s.total_rows} rows · ${s.errors} errors · ${s.warnings} warnings`;

  const erp = sm.erp_detected;
  const banner = document.getElementById('erpBanner');
  if (erp) {
    banner.style.display = 'flex';
    banner.innerHTML =
      `<span class="tag">${erp}</span>` +
      `<span>${(sm.erp_confidence*100).toFixed(0)}% match</span>` +
      `<span>·</span>` +
      `<span>${Object.keys(sm.mapped||{}).length} mapped · ${(sm.unmapped||[]).length} unmapped</span>` +
      (sm.unmapped?.length ? `<span>· Unmapped: <code>${sm.unmapped.slice(0,5).join(', ')}</code></span>` : '');
  } else {
    banner.style.display = 'flex';
    banner.innerHTML =
      `<span>${Object.keys(sm.mapped||{}).length} columns mapped · ${(sm.unmapped||[]).length} unmapped</span>` +
      (sm.unmapped?.length ? ` · <code>${sm.unmapped.slice(0,5).join(', ')}</code>` : '');
  }
}

function methodBadge(m) {
  if (!m || m === 'unmapped') return '<span class="badge b-unmapped">unmapped</span>';
  if (m.startsWith('erp_profile')) return '<span class="badge b-erp">ERP profile</span>';
  if (m.startsWith('registry'))   return '<span class="badge b-registry">registry</span>';
  if (m.startsWith('vector'))     return '<span class="badge b-vector">vector</span>';
  if (m === 'alias')              return '<span class="badge b-alias">alias</span>';
  if (m.startsWith('fuzzy'))      return '<span class="badge b-fuzzy">fuzzy</span>';
  if (m.startsWith('llm'))        return '<span class="badge b-llm">AI</span>';
  return `<span class="badge b-alias">${m}</span>`;
}

function statusBadge(s) {
  const map = { confirmed: 'b-confirmed', inferred: 'b-inferred', rejected: 'b-rejected', unmapped: 'b-unmapped' };
  return `<span class="badge ${map[s]||'b-unmapped'}">${s||'unmapped'}</span>`;
}

function renderMapping(data) {
  const sm = data.schema_mapping || {};
  const methods = sm.method || {};
  const mapped = sm.mapped || {};
  const statuses = sm.status || {};
  const confirmed = Object.values(statuses).filter(s => s === 'confirmed').length;
  const inferred  = Object.values(statuses).filter(s => s === 'inferred').length;
  const unmapped  = (sm.unmapped || []).length;

  document.getElementById('mappingMeta').textContent =
    `${confirmed} confirmed · ${inferred} inferred · ${unmapped} unmapped`;

  document.getElementById('mappingBody').innerHTML = Object.entries(methods).map(([k, m]) => {
    const udm = mapped[k];
    const st = statuses[k] || 'unmapped';
    return `<tr>
      <td><code>${k}</code></td>
      <td>${udm ? `<code>${udm}</code>` : '<span style="color:#94a3b8">—</span>'}</td>
      <td>${methodBadge(m)}</td>
      <td>${statusBadge(st)}</td>
    </tr>`;
  }).join('');
}

function renderCompleteness(data) {
  const comp = data.completeness || {};
  const entries = Object.entries(comp).sort((a,b) => a[1].fill_rate_pct - b[1].fill_rate_pct);
  const perfect = entries.filter(([,v]) => v.fill_rate_pct === 100).length;
  const low     = entries.filter(([,v]) => v.fill_rate_pct < 70).length;
  document.getElementById('compMeta').textContent = `${perfect} complete · ${low} low fill rate`;

  document.getElementById('compGrid').innerHTML = entries.map(([field, c]) => {
    const pct = c.fill_rate_pct || 0;
    const color = pct === 100 ? '#059669' : pct >= 80 ? '#d97706' : '#dc2626';
    return `<div class="comp-item">
      <div class="ci-name" title="${field}">${field}</div>
      <div class="comp-bar"><div class="comp-fill" style="width:${pct}%;background:${color}"></div></div>
      <div class="comp-meta">
        <span class="comp-pct" style="color:${color}">${pct}%</span>
        <span>${c.null_count} null</span>
      </div>
    </div>`;
  }).join('');
}

let _allIssues = [];
let _activeRule = null;

function renderIssues(data) {
  const rules = data.rule_summary || {};
  const fields = data.field_summary || {};
  const summary = data.summary || {};
  _allIssues = [...(data.sample_errors||[]), ...(data.sample_warnings||[])];
  _activeRule = null;
  document.getElementById('drillPanel').style.display = 'none';

  const totalIssues = Object.values(rules).reduce((a,v) => a + v.total, 0);
  const errCount = summary.errors || 0;
  const warnCount = summary.warnings || 0;
  const impactedFields = Object.keys(fields).length;

  document.getElementById('issuesMeta').textContent =
    `${totalIssues} total · ${errCount} errors · ${warnCount} warnings`;

  document.getElementById('severityStrip').innerHTML = `
    <div class="sev-pill"><div class="sev-dot" style="background:#dc2626"></div><span><strong>${errCount}</strong> Errors</span></div>
    <div class="sev-pill"><div class="sev-dot" style="background:#d97706"></div><span><strong>${warnCount}</strong> Warnings</span></div>
    <div class="sev-pill"><div class="sev-dot" style="background:#2563eb"></div><span><strong>${Object.keys(rules).length}</strong> Rule types</span></div>
    <div class="sev-pill"><div class="sev-dot" style="background:#7c3aed"></div><span><strong>${impactedFields}</strong> Fields affected</span></div>
  `;

  if (totalIssues === 0) {
    document.getElementById('ruleCardGrid').innerHTML =
      '<div class="empty" style="grid-column:1/-1">No validation issues found. Data looks clean.</div>';
    return;
  }

  const sortedRules = Object.entries(rules).sort((a,b) => b[1].total - a[1].total);
  document.getElementById('ruleCardGrid').innerHTML = sortedRules.map(([rule, v]) => {
    const pct = totalIssues ? Math.round((v.total / totalIssues) * 100) : 0;
    const isErr = v.error > 0;
    const barColor = isErr ? '#dc2626' : '#d97706';
    const ruleFields = [...new Set(_allIssues.filter(e => e.rule === rule).map(e => e.field))];
    const fieldTags = ruleFields.slice(0,3).map(f =>
      `<span style="background:#f1f5f9;padding:1px 6px;border-radius:4px;font-size:10px;color:#64748b">${f}</span>`
    ).join(' ') + (ruleFields.length > 3 ? ` <span style="font-size:10px;color:#94a3b8">+${ruleFields.length-3}</span>` : '');

    return `<div class="rule-card" id="rc-${rule}" onclick="drillDown('${rule}')">
      <div class="rc-name">${rule.replace(/_/g,' ')}</div>
      <div class="rc-counts">
        ${v.error   ? `<span class="badge b-error">${v.error} err</span>` : ''}
        ${v.warning ? `<span class="badge b-warning">${v.warning} warn</span>` : ''}
        <span style="margin-left:auto;font-size:11px;color:#94a3b8">${pct}%</span>
      </div>
      <div class="rc-bar"><div class="rc-fill" style="width:${pct}%;background:${barColor}"></div></div>
      ${ruleFields.length ? `<div style="margin-top:7px;display:flex;flex-wrap:wrap;gap:3px">${fieldTags}</div>` : ''}
      <div style="font-size:10px;color:#2563eb;margin-top:7px;font-weight:600">Click to inspect →</div>
    </div>`;
  }).join('');

  const impl = data.imputation_suggestions || [];
  if (impl.length) {
    document.getElementById('implWrap').style.display = 'block';
    document.getElementById('implBody').innerHTML = impl.map(s =>
      `<tr>
        <td style="color:#94a3b8;font-size:12px">${s.row}</td>
        <td><code>${s.field}</code></td>
        <td><strong>${s.suggested_value}</strong></td>
        <td style="font-size:12px;color:#64748b">${s.method}</td>
      </tr>`
    ).join('');
  } else {
    document.getElementById('implWrap').style.display = 'none';
  }
}

function drillDown(rule) {
  if (_activeRule === rule) { closeDrill(); return; }
  _activeRule = rule;

  document.querySelectorAll('.rule-card').forEach(c => c.classList.remove('active'));
  const card = document.getElementById('rc-' + rule);
  if (card) { card.classList.add('active'); card.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }

  const rows = _allIssues.filter(e => e.rule === rule);
  const isErr = rows.some(e => e.severity === 'error');
  document.getElementById('drillTitle').innerHTML =
    `<span class="badge ${isErr?'b-error':'b-warning'}" style="margin-right:8px">${rule.replace(/_/g,' ')}</span>` +
    `<span style="font-size:13px;font-weight:700;color:#0f172a">${rows.length} instance${rows.length>1?'s':''}</span>`;

  document.getElementById('drillBody').innerHTML = rows.map(e => {
    const display = e.inv_id && e.inv_id !== `Row ${e.row}` ? e.inv_id : `Row ${e.row}`;
    return `<tr>
      <td style="color:#2563eb;font-size:12px;white-space:nowrap;font-weight:600">${display}</td>
      <td><code>${e.field}</code></td>
      <td style="font-size:13px">${e.message}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="3" class="empty">No sample rows available</td></tr>';

  const moreEl = document.getElementById('drillMore');
  moreEl.style.display = rows.length >= 50 ? 'block' : 'none';
  if (rows.length >= 50) moreEl.textContent = 'Showing first 50 rows. Full dataset may contain more.';

  const panel = document.getElementById('drillPanel');
  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeDrill() {
  _activeRule = null;
  document.getElementById('drillPanel').style.display = 'none';
  document.querySelectorAll('.rule-card').forEach(c => c.classList.remove('active'));
}

// ── Pending approvals ─────────────────────────────────────────────────────────
let _currentPending = [];
let _allMappings = [];

const ALL_UDM = ["amount_excl_lc","amount_excl_oc","amount_incl_lc","amount_incl_oc","amount_vat_lc","amount_vat_oc","authorized_by","captured_by","company_code","concept_search","date_authorized","date_captured","date_documented","debit_credit_indicator","due_date","erp_source","fiscal_year","gl_indicator","invoice_description","invoice_line_ref","invoice_name","invoice_status","invoice_type","original_invoice_no","payment_amount","payment_date","payment_number","payment_type","physical_invoice_no","posting_key","ref_no","reference_currency","scheduled_pay_date","system_invoice_no","vendor_name","vendor_number"];

function updatePendingFromResult(data) {
  const sm = data.schema_mapping || {};
  const statuses = sm.status || {};
  const mapped = sm.mapped || {};
  const methods = sm.method || {};
  const alternatives = sm.alternatives || {};
  const llmSuggested = sm.llm_suggested || {};

  const pending = [];
  const allMaps = [];

  for (const [col, udmField] of Object.entries(mapped)) {
    const st = statuses[col] || 'confirmed';
    const alts = alternatives[col] || [];
    const entry = { id: 'local-' + col, source_column: col, udm_field: udmField, method: methods[col], status: st, confidence: alts[0] ? alts[0].confidence : 1.0, alternatives: alts, _local: true };
    allMaps.push(entry);
    if (st === 'inferred') pending.push(entry);
  }

  for (const [col, suggestion] of Object.entries(llmSuggested)) {
    if (!mapped[col]) {
      const alts = alternatives[col] || [{ udm_field: suggestion.udm_field, confidence: 0.7, reason: suggestion.reason }];
      const entry = { id: 'local-' + col, source_column: col, udm_field: suggestion.udm_field, method: 'llm', status: 'inferred', confidence: 0.7, alternatives: alts, _local: true };
      allMaps.push(entry);
      pending.push(entry);
    }
  }

  _currentPending = pending;
  _allMappings = allMaps;

  const alert = document.getElementById('pendingAlert');
  if (pending.length) {
    alert.style.display = 'block';
    document.getElementById('pendingCount').textContent = pending.length;
  } else if (allMaps.length) {
    alert.style.display = 'block';
    document.getElementById('pendingCount').textContent = allMaps.length;
  } else {
    alert.style.display = 'none';
  }
}

async function checkPending() {
  if (_currentPending.length > 0) {
    document.getElementById('pendingAlert').style.display = 'block';
    document.getElementById('pendingCount').textContent = _currentPending.length;
  } else {
    const res = await fetch('/api/registry/pending');
    const list = await res.json();
    const alert = document.getElementById('pendingAlert');
    if (list.length) {
      alert.style.display = 'block';
      document.getElementById('pendingCount').textContent = list.length;
    } else {
      alert.style.display = 'none';
    }
  }
}

async function openPending() {
  document.getElementById('overlay').style.display = 'flex';
  const list = _allMappings.length
    ? _allMappings
    : await fetch('/api/registry/pending').then(r => r.json());
  const tbody = document.getElementById('pendingBody');
  const empty = document.getElementById('pendingEmpty');
  if (!list.length) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
  } else {
    empty.style.display = 'none';
    tbody.innerHTML = list.map(p => {
      const st = p.status || 'confirmed';
      const currentField = p.udm_field || '';
      const alts = (p.alternatives && p.alternatives.length > 0) ? p.alternatives : [];
      const altFields = new Set(alts.map(a => a.udm_field));
      const suggestedOpts = alts.map(a =>
        `<option value="${a.udm_field}" ${a.udm_field === currentField ? 'selected' : ''}>${a.udm_field}${a.confidence ? ` (${Math.round(a.confidence*100)}%)` : ''}${a.reason ? ' · '+a.reason : ''}</option>`
      ).join('');
      const currentOpt = !altFields.has(currentField) && currentField
        ? `<option value="${currentField}" selected>${currentField} (current)</option>` : '';
      const manualOpts = ALL_UDM.filter(f => f !== currentField && !altFields.has(f)).map(f => `<option value="${f}">${f}</option>`).join('');
      const selectId = `sel-${p.id}`;
      const rowStyle = st === 'inferred' ? 'background:#fffbeb' : '';
      return `<tr id="pr-${p.id}" style="${rowStyle}">
        <td><code>${p.source_column}</code></td>
        <td><select id="${selectId}" style="width:100%;padding:5px 8px;border:1px solid #c7d2fe;border-radius:6px;font-size:12px;background:#fff;font-family:inherit">
          ${currentOpt}
          ${suggestedOpts ? `<optgroup label="Suggestions">${suggestedOpts}</optgroup>` : ''}
          <optgroup label="── All UDM Fields ──">${manualOpts}</optgroup>
        </select></td>
        <td>${methodBadge(p.method)}</td>
        <td>${statusBadge(st)}</td>
        <td style="white-space:nowrap">
          <button class="btn-approve" onclick="doApprove('${p.id}','${selectId}')">Confirm</button>
          <button class="btn-reject"  onclick="doReject('${p.id}','${selectId}')">Skip</button>
        </td>
      </tr>`;
    }).join('');
  }
}

function closePending() { document.getElementById('overlay').style.display = 'none'; }

async function doApprove(id, selectId) {
  const selected = selectId ? document.getElementById(selectId)?.value : '';
  const row = document.getElementById('pr-' + id) || document.querySelector(`#pr-${String(id).replace(/[^a-zA-Z0-9_-]/g,'_')}`);
  const sourceCol = row?.querySelector('td:first-child code')?.textContent;
  if (sourceCol && selected) {
    await fetch('/api/registry/upsert', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_column: sourceCol, udm_field: selected, status: 'confirmed', method: 'user-approved' })
    });
  }
  _currentPending = _currentPending.filter(p => String(p.id) !== String(id));
  _allMappings    = _allMappings.filter(p => String(p.id) !== String(id));
  if (sourceCol && selected) {
    document.querySelectorAll('#mappingBody tr').forEach(tr => {
      const cell = tr.querySelector('td:first-child code');
      if (cell && cell.textContent === sourceCol) {
        const cells = tr.querySelectorAll('td');
        if (cells[1]) cells[1].innerHTML = `<code>${selected}</code>`;
        if (cells[2]) cells[2].innerHTML = '<span class="badge b-registry">registry</span>';
        if (cells[3]) cells[3].innerHTML = '<span class="badge b-confirmed">confirmed</span>';
      }
    });
  }
  row?.remove();
  const remaining = document.querySelectorAll('#pendingBody tr').length;
  document.getElementById('pendingCount').textContent = _currentPending.length || _allMappings.length;
  if (!remaining) { document.getElementById('pendingEmpty').style.display = 'block'; document.getElementById('pendingAlert').style.display = 'none'; }
}

async function doReject(id) {
  const row = document.getElementById('pr-' + id) || document.querySelector(`#pr-${String(id).replace(/[^a-zA-Z0-9_-]/g,'_')}`);
  _currentPending = _currentPending.filter(p => String(p.id) !== String(id));
  _allMappings    = _allMappings.filter(p => String(p.id) !== String(id));
  row?.remove();
  const remaining = document.querySelectorAll('#pendingBody tr').length;
  document.getElementById('pendingCount').textContent = _currentPending.length || _allMappings.length;
  if (!remaining) { document.getElementById('pendingEmpty').style.display = 'block'; document.getElementById('pendingAlert').style.display = 'none'; }
}

checkPending();

// ── Analytics Tests ──────────────────────────────────────────────────────────
let _currentRunId = null;
let _testResults  = {};
let _activeDrillTest = null;

function renderAnalyticsTests(data) {
  const at = data.analytics_tests || {};
  const applicable    = at.applicable || [];
  const notApplicable = at.not_applicable || [];
  _currentRunId = data.run_id;
  if (data.run_id) localStorage.setItem('dia_last_run_id', data.run_id);

  document.getElementById('tsApplicable').textContent    = applicable.length;
  document.getElementById('tsNotApplicable').textContent = notApplicable.length;
  document.getElementById('testsMeta').textContent =
    `${applicable.length} applicable · ${notApplicable.length} need extra data`;

  document.getElementById('testList').innerHTML = applicable.map(t => {
    const sevClass = `b-sev-${t.severity}`;
    return `<label class="test-item" id="ti-${t.test_id}" onclick="highlightTestItem(this)">
      <input type="checkbox" class="test-check" data-id="${t.test_id}" checked />
      <div class="ti-info">
        <div class="ti-name">
          <span style="font-size:10px;font-weight:800;color:#7c3aed;margin-right:6px">${t.test_id}</span>${t.test_name}
          ${t.red_flag ? '<span class="b-red-flag" style="margin-left:6px">⚑ RED FLAG</span>' : ''}
        </div>
        <div class="ti-desc">${t.description}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end">
        <span class="badge ${sevClass}">${t.severity}</span>
        <span class="b-weight">w:${t.weightage}</span>
      </div>
    </label>`;
  }).join('');

  if (notApplicable.length) {
    document.getElementById('naSectionWrap').style.display = 'block';
    document.getElementById('naCount').textContent = notApplicable.length;
    document.getElementById('naList').innerHTML = notApplicable.map(t =>
      `<div class="test-na">
        <span style="font-size:10px;font-weight:800;color:#94a3b8;width:36px">${t.test_id}</span>
        <span class="ti-name">${t.test_name}</span>
        <span class="ti-reason">${t.reason}</span>
      </div>`
    ).join('');
  } else {
    document.getElementById('naSectionWrap').style.display = 'none';
  }

  document.getElementById('testsSection').style.display = 'block';
  document.getElementById('testResultsWrap').style.display = 'none';
  _testResults = {};
  _activeDrillTest = null;
  document.getElementById('testDrillPanel').style.display = 'none';
}

function highlightTestItem(label) {
  label.classList.toggle('selected', label.querySelector('input').checked);
}

function toggleSelectAll(checked) {
  document.querySelectorAll('.test-check').forEach(cb => {
    cb.checked = checked;
    cb.closest('.test-item')?.classList.toggle('selected', checked);
  });
}

function toggleNA() {
  const list = document.getElementById('naList');
  const tog  = document.querySelector('.na-toggle');
  const open = list.style.display !== 'none';
  list.style.display = open ? 'none' : 'flex';
  tog.textContent = (open ? '▶' : '▼') + tog.textContent.slice(1);
}

async function runSelectedTests() {
  if (!_currentRunId) { alert('No validation run found. Please upload a file first.'); return; }
  const selected = [...document.querySelectorAll('.test-check:checked')].map(cb => cb.dataset.id);
  if (!selected.length) { alert('Please select at least one test.'); return; }

  const btn    = document.getElementById('runTestsBtn');
  const status = document.getElementById('testRunStatus');
  btn.disabled = true;
  status.innerHTML = `<span class="spinner"></span> Running ${selected.length} test(s)...`;

  try {
    const res = await fetch(`/api/runs/${_currentRunId}/tests/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ selected_ids: selected }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Tests failed');
    renderTestResults(data.results, data.total_rows);
    status.textContent = `${data.tests_run} test(s) completed.`;
  } catch (e) {
    status.textContent = '✕ ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

function renderTestResults(results, totalRows) {
  _testResults = {};
  results.forEach(r => { _testResults[r.test_id] = r; });

  const hitTests  = results.filter(r => r.applicable && r.hit_count > 0);
  const noHitTests = results.filter(r => r.applicable && r.hit_count === 0);
  const totalHits   = hitTests.reduce((a, r) => a + r.hit_count, 0);
  const totalAmount = hitTests.reduce((a, r) => a + r.hit_amount, 0);
  const redFlags    = hitTests.filter(r => r.red_flag).length;

  document.getElementById('testResultsBar').innerHTML = `
    <div class="test-stat" style="${hitTests.length ? 'border-color:#fca5a5;background:#fff1f2' : ''}">
      <strong style="color:${hitTests.length ? '#dc2626' : '#059669'}">${hitTests.length}</strong> with hits
    </div>
    ${redFlags ? `<div class="test-stat" style="border-color:#fca5a5;background:#fef2f2"><strong style="color:#7f1d1d">⚑ ${redFlags}</strong> red flags</div>` : ''}
    <div class="test-stat"><strong>${totalHits.toLocaleString()}</strong> total hits</div>
    ${totalAmount > 0 ? `<div class="test-stat amber"><strong>${fmtAmt(totalAmount)}</strong> amount</div>` : ''}
    ${noHitTests.length ? `<div class="test-stat muted"><strong>${noHitTests.length}</strong> no hits</div>` : ''}
  `;

  const sortedResults = [...results].sort((a, b) => {
    if (!a.applicable && b.applicable) return 1;
    if (a.applicable && !b.applicable) return -1;
    return b.hit_count - a.hit_count;
  });

  document.getElementById('testResultsGrid').innerHTML = sortedResults.map(r => {
    if (!r.applicable) {
      return `<div class="trc trc-no-hit">
        <div class="trc-id">${r.test_id}</div>
        <div class="trc-name">${r.test_name}</div>
        <div class="trc-summary">${r.not_applicable_reason}</div>
      </div>`;
    }

    const hitPct   = r.hit_pct || 0;
    const hasHits  = r.hit_count > 0;
    const cardClass = hasHits
      ? (r.severity === 'error' || r.red_flag ? 'trc-hit-high' : r.hit_pct > 15 ? 'trc-hit-mid' : 'trc-hit-low')
      : 'trc-no-hit';
    const amtColor = r.hit_amount > 100000 ? 'red' : r.hit_amount > 10000 ? 'amber' : '';
    const pctColor = hitPct > 20 ? 'red' : hitPct > 5 ? 'amber' : 'green';
    const barColor = r.severity === 'error' || r.red_flag ? '#dc2626' : '#d97706';

    return `<div class="trc ${cardClass}" id="trc-${r.test_id}" onclick="toggleTestDrill('${r.test_id}')">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
        <span class="trc-id">${r.test_id}</span>
        ${r.red_flag ? '<span class="b-red-flag">⚑ RED FLAG</span>' : ''}
        <span class="badge b-sev-${r.severity}" style="margin-left:auto">${r.severity}</span>
      </div>
      <div class="trc-name">${r.test_name}</div>
      <div class="trc-stat-row">
        <div class="trc-stat">
          <span class="ts-val ${hasHits ? (r.severity==='error'?'red':'amber') : 'green'}">${r.hit_count.toLocaleString()}</span>
          <span class="ts-label">hits</span>
        </div>
        <div class="trc-stat">
          <span class="ts-val ${pctColor}">${hitPct}%</span>
          <span class="ts-label">of rows</span>
        </div>
        ${r.hit_amount > 0 ? `<div class="trc-stat"><span class="ts-val ${amtColor}">${fmtAmt(r.hit_amount)}</span><span class="ts-label">amount</span></div>` : ''}
      </div>
      <div class="trc-bar"><div class="trc-fill" style="width:${Math.min(100,hitPct*2)}%;background:${hasHits?barColor:'#e2e8f0'}"></div></div>
      ${r.summary ? `<div class="trc-summary">${r.summary}</div>` : ''}
      <div class="trc-footer">
        ${hasHits ? `<span class="view-btn">Inspect ${r.hit_count} row${r.hit_count>1?'s':''} →</span>` : '<span class="view-btn disabled-link">No hits detected</span>'}
      </div>
    </div>`;
  }).join('');

  document.getElementById('testResultsWrap').style.display = 'block';
  document.getElementById('testDrillPanel').style.display = 'none';
  _activeDrillTest = null;
  document.getElementById('testResultsWrap').scrollIntoView({ behavior: 'smooth', block: 'start' });
  // show Review Queue link in sidebar
  const reviewLink = document.getElementById('reviewLink');
  if (reviewLink) reviewLink.style.display = '';
}

function toggleTestDrill(testId) {
  const r = _testResults[testId];
  if (!r || !r.applicable || !r.hit_count) return;
  if (_activeDrillTest === testId) { closeTestDrill(); return; }
  _activeDrillTest = testId;

  document.querySelectorAll('.trc').forEach(c => c.classList.remove('trc-active'));
  document.getElementById('trc-' + testId)?.classList.add('trc-active');

  document.getElementById('testDrillTitle').innerHTML =
    `<span class="badge b-sev-${r.severity}" style="margin-right:6px">${r.severity}</span>` +
    `<strong>${r.test_id} – ${r.test_name}</strong>`;
  document.getElementById('testDrillSummary').textContent =
    `${r.hit_count} hit row(s) · ${r.hit_pct}% of total · ${fmtAmt(r.hit_amount)} affected` +
    (r.summary ? ` · ${r.summary}` : '');

  document.getElementById('testDrillBody').innerHTML = (r.sample_hits || []).map(h =>
    (() => {
      const invNo = h.physical_invoice_no || h.system_invoice_no || '—';
      const dispId = (h.vendor_number && invNo !== '—')
        ? `${h.vendor_number} / ${invNo}`
        : (invNo !== '—' ? invNo : `Row ${h.row}`);
      return `<tr>
        <td style="color:#2563eb;font-size:12px;white-space:nowrap;font-weight:600">${dispId}</td>
        <td style="font-size:12px">${h.vendor_number||''}<br><span style="color:#94a3b8;font-size:11px">${h.vendor_name||''}</span></td>
        <td><code style="font-size:11px">${invNo}</code></td>
        <td style="font-size:12px;white-space:nowrap">${h.date_authorized||h.date_documented||'—'}</td>
        <td style="font-size:13px;font-weight:700;text-align:right;white-space:nowrap">${fmtNum(h.amount_excl_lc)}</td>
        <td style="font-size:11px;color:#64748b;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${h.invoice_description||''}</td>
      </tr>`;
    })()
  ).join('') || '<tr><td colspan="6" class="empty">No sample rows available</td></tr>';

  const more = document.getElementById('testDrillMore');
  if (r.hit_count > 50) { more.style.display='block'; more.textContent=`Showing 50 of ${r.hit_count} rows.`; }
  else { more.style.display='none'; }

  const panel = document.getElementById('testDrillPanel');
  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeTestDrill() {
  _activeDrillTest = null;
  document.getElementById('testDrillPanel').style.display = 'none';
  document.querySelectorAll('.trc').forEach(c => c.classList.remove('trc-active'));
}

// ── Formatters ────────────────────────────────────────────────────────────────
function fmtAmt(v) {
  if (!v && v !== 0) return '—';
  if (v >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
  if (v >= 1e3) return '$' + (v/1e3).toFixed(1) + 'K';
  return '$' + Number(v).toFixed(0);
}

function fmtNum(v) {
  if (v === null || v === undefined || v === '') return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return v;
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ── Auto-restore last run on page load (e.g. after Back from review page) ───
(async function restoreLastRun() {
  // Step 1: check server session — a new UUID on every backend restart
  let currentSession = null;
  try {
    const sr = await fetch('/api/session');
    if (sr.ok) currentSession = (await sr.json()).session;
  } catch(e) { return; } // server not reachable

  const storedSession = localStorage.getItem('dia_server_session');
  if (storedSession !== currentSession) {
    // Backend restarted — clear all persisted UI state and start fresh
    localStorage.removeItem('dia_last_run_id');
    localStorage.removeItem('dia_last_run_file');
    localStorage.setItem('dia_server_session', currentSession);
    return;
  }

  // Step 2: same session — restore if user hasn't explicitly cleared
  const runId   = localStorage.getItem('dia_last_run_id');
  const runFile = localStorage.getItem('dia_last_run_file') || 'previous run';
  if (!runId) return;  // user pressed "Start New Run" — stay fresh
  try {
    const res = await fetch(`/api/runs/${runId}`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data || data.error) return;

    render(data);
    document.getElementById('results').style.display = 'block';
    updateSidebarRunInfo(data, runFile);
    document.getElementById('topbarTitle').textContent = runFile;
    document.getElementById('newRunBtn').style.display = 'block';
    const reviewLink = document.getElementById('reviewLink');
    if (reviewLink) reviewLink.style.display = '';
    // restore test results if they exist
    const trRes = await fetch(`/api/runs/${runId}/tests/results`);
    if (trRes.ok) {
      const trData = await trRes.json();
      if (trData.results && trData.results.length) {
        renderTestResults(trData.results, (data.summary || {}).total_rows || 0);
        document.getElementById('testsSection').style.display = 'block';
        document.getElementById('testResultsWrap').style.display = 'block';
      }
    }
  } catch(e) {
    console.warn('restoreLastRun failed:', e);
  }
})();
