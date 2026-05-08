/* ── Augmented OCR — Dashboard page ──────────────────────────────────── */

// ══════════════════════════════════════════════════════════════════════
// PAGE: DASHBOARD
// Admin  → /admin/stats  (all users) + /admin/usage/clients
// Client → /user/stats   (own vendors only)
// ══════════════════════════════════════════════════════════════════════

function fmtTokens(n) {
    n = Number(n || 0);
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
    if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'K';
    return String(n);
}

function fmtNum(n) {
    return Number(n || 0).toLocaleString();
}

function fmtDate(value) {
    if (!value) return '—';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value).slice(0, 10);
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' });
}

function fmtLatency(ms) {
    const n = Number(ms || 0);
    if (!n) return '—';
    if (n < 1000) return Math.round(n) + 'ms';
    return (n / 1000).toFixed(1) + 's';
}

function fmtCurrency(n) {
    return '$' + Number(n || 0).toFixed(4);
}

function _todayISO() {
    return new Date().toISOString().slice(0, 10);
}

function _shiftDateISO(daysBack) {
    const d = new Date();
    d.setDate(d.getDate() - daysBack);
    return d.toISOString().slice(0, 10);
}

// ── KPI card ──────────────────────────────────────────────────────────
function _kpiCard(label, value, color, sub) {
    return `
    <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:18px 20px;min-width:0">
        <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:10px">${label}</div>
        <div style="font-size:30px;font-family:var(--display);font-weight:700;color:${color};letter-spacing:0.02em;line-height:1">${value}</div>
        ${sub ? `<div style="font-size:9px;color:var(--text-dim);margin-top:6px;letter-spacing:0.06em">${sub}</div>` : ''}
    </div>`;
}

// ── PIE CHART (two-segment donut: input vs output) ────────────────────
function _renderPieChart(inputTokens, outputTokens) {
    const total = inputTokens + outputTokens;
    if (total === 0) {
        return `<div style="color:var(--text-dim);text-align:center;padding:48px 0;font-size:10px">No data yet</div>`;
    }

    const cx = 90, cy = 90, r = 72, innerR = 44;
    const inputFrac = inputTokens / total;
    const inputAngle = inputFrac * 2 * Math.PI;
    const inputPct = (inputFrac * 100).toFixed(1);
    const outputPct = (100 - parseFloat(inputPct)).toFixed(1);

    function arcPath(startAngle, endAngle, outerR, iR, color) {
        const sa = startAngle - Math.PI / 2;
        const ea = endAngle - Math.PI / 2;
        const x1o = cx + outerR * Math.cos(sa), y1o = cy + outerR * Math.sin(sa);
        const x2o = cx + outerR * Math.cos(ea), y2o = cy + outerR * Math.sin(ea);
        const x1i = cx + iR * Math.cos(ea),     y1i = cy + iR * Math.sin(ea);
        const x2i = cx + iR * Math.cos(sa),     y2i = cy + iR * Math.sin(sa);
        const large = endAngle - startAngle > Math.PI ? 1 : 0;
        return `<path d="M ${x1o} ${y1o} A ${outerR} ${outerR} 0 ${large} 1 ${x2o} ${y2o} L ${x1i} ${y1i} A ${iR} ${iR} 0 ${large} 0 ${x2i} ${y2i} Z" fill="${color}"/>`;
    }

    const effectiveInput  = Math.max(inputAngle,  0.08);
    const effectiveOutput = Math.max(2 * Math.PI - inputAngle, 0.08);
    const scale = 2 * Math.PI / (effectiveInput + effectiveOutput);

    return `
    <svg viewBox="0 0 180 180" width="180" height="180" style="overflow:visible">
        ${arcPath(0, effectiveInput * scale, r, innerR, '#3b82f6')}
        ${arcPath(effectiveInput * scale, (effectiveInput + effectiveOutput) * scale, r, innerR, '#22c55e')}
        <text x="${cx}" y="${cy - 7}" text-anchor="middle" fill="var(--text)" font-size="12"
              font-family="JetBrains Mono, monospace" font-weight="500">${inputPct}%</text>
        <text x="${cx}" y="${cy + 6}" text-anchor="middle" fill="var(--text-dim)" font-size="8"
              font-family="JetBrains Mono, monospace">IN / OUT</text>
        <text x="${cx}" y="${cy + 20}" text-anchor="middle" fill="var(--green)" font-size="12"
              font-family="JetBrains Mono, monospace" font-weight="500">${outputPct}%</text>
    </svg>
    <div style="display:flex;justify-content:center;gap:20px;margin-top:10px">
        <span style="display:flex;align-items:center;gap:6px;font-size:9px;color:var(--text-dim)">
            <span style="display:inline-block;width:10px;height:10px;background:#3b82f6;border-radius:2px"></span>Input
        </span>
        <span style="display:flex;align-items:center;gap:6px;font-size:9px;color:var(--text-dim)">
            <span style="display:inline-block;width:10px;height:10px;background:#22c55e;border-radius:2px"></span>Output
        </span>
    </div>`;
}

// ── LINE CHART (daily input vs output tokens, last 30 days) ───────────
function _renderLineChart(days) {
    if (!days || days.length === 0) {
        return `<div style="color:var(--text-dim);text-align:center;padding:48px 0;font-size:10px">No data yet</div>`;
    }

    const sorted = [...days].reverse();
    const W = 580, H = 170;
    const PAD = { top: 14, right: 20, bottom: 28, left: 54 };
    const cW = W - PAD.left - PAD.right;
    const cH = H - PAD.top  - PAD.bottom;
    const n = sorted.length;

    const maxVal = Math.max(
        ...sorted.map(d => Math.max(Number(d.input_tokens || 0), Number(d.output_tokens || 0))),
        1
    );

    function xP(i) { return PAD.left + (i / Math.max(n - 1, 1)) * cW; }
    function yP(v) { return PAD.top + cH - (Number(v || 0) / maxVal) * cH; }

    const gridLines = [0.25, 0.5, 0.75, 1.0].map(f => {
        const y = PAD.top + cH * (1 - f);
        return `<line x1="${PAD.left}" y1="${y}" x2="${W - PAD.right}" y2="${y}"
                      stroke="var(--border)" stroke-width="1"/>
                <text x="${PAD.left - 5}" y="${y + 4}" text-anchor="end"
                      fill="var(--text-dim)" font-size="9" font-family="JetBrains Mono,monospace"
                      >${fmtTokens(maxVal * f)}</text>`;
    }).join('');

    const step = Math.max(1, Math.floor(n / 8));
    const xLabels = sorted
        .filter((_, i) => i % step === 0 || i === n - 1)
        .map(d => {
            const i = sorted.indexOf(d);
            const label = String(d.day || '').slice(5);
            return `<text x="${xP(i)}" y="${H - 5}" text-anchor="middle"
                          fill="var(--text-dim)" font-size="9" font-family="JetBrains Mono,monospace"
                          >${label}</text>`;
        }).join('');

    const inputPts  = sorted.map((d, i) => `${xP(i)},${yP(d.input_tokens)}`).join(' ');
    const outputPts = sorted.map((d, i) => `${xP(i)},${yP(d.output_tokens)}`).join(' ');

    const baseY = PAD.top + cH;
    const inputAreaPts  = `${xP(0)},${baseY} ${inputPts}  ${xP(n-1)},${baseY}`;
    const outputAreaPts = `${xP(0)},${baseY} ${outputPts} ${xP(n-1)},${baseY}`;

    const inputDots  = sorted.map((d, i) =>
        `<circle cx="${xP(i)}" cy="${yP(d.input_tokens)}"  r="3" fill="#3b82f6"
                 style="cursor:default" title="${String(d.day).slice(5)}: ${fmtTokens(d.input_tokens)} input"/>`
    ).join('');
    const outputDots = sorted.map((d, i) =>
        `<circle cx="${xP(i)}" cy="${yP(d.output_tokens)}" r="3" fill="#22c55e"
                 style="cursor:default" title="${String(d.day).slice(5)}: ${fmtTokens(d.output_tokens)} output"/>`
    ).join('');

    return `
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" style="max-width:${W}px;display:block">
        ${gridLines}
        <polygon points="${inputAreaPts}"  fill="#3b82f6" opacity="0.08"/>
        <polygon points="${outputAreaPts}" fill="#22c55e" opacity="0.08"/>
        <polyline points="${inputPts}"  fill="none" stroke="#3b82f6" stroke-width="2" stroke-linejoin="round"/>
        <polyline points="${outputPts}" fill="none" stroke="#22c55e" stroke-width="2" stroke-linejoin="round"/>
        ${inputDots}${outputDots}
        ${xLabels}
    </svg>`;
}

// ── DAILY BREAKDOWN TABLE ─────────────────────────────────────────────
function _renderDailyTable(days) {
    if (!days || days.length === 0) return '';
    return `
    <div style="margin-top:20px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
        <div style="padding:12px 16px;border-bottom:1px solid var(--border);font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">
            DAILY BREAKDOWN
        </div>
        <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse;font-size:10px">
                <thead>
                    <tr style="border-bottom:1px solid var(--border)">
                        <th style="text-align:left;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em">DATE</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em">INPUT</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em">OUTPUT</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em">TOTAL</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em">DOCS</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em">CALLS</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em">AVG LATENCY</th>
                    </tr>
                </thead>
                <tbody>
                    ${days.map((d, idx) => `
                    <tr style="border-bottom:1px solid var(--border);${idx % 2 === 1 ? 'background:var(--bg2)' : ''}">
                        <td style="padding:7px 16px;color:var(--text-mid);font-family:var(--mono)">${String(d.day || '').slice(0, 10)}</td>
                        <td style="padding:7px 16px;text-align:right;color:var(--blue)">${fmtTokens(d.input_tokens)}</td>
                        <td style="padding:7px 16px;text-align:right;color:var(--green)">${fmtTokens(d.output_tokens)}</td>
                        <td style="padding:7px 16px;text-align:right;color:var(--text)">${fmtTokens(d.total_tokens)}</td>
                        <td style="padding:7px 16px;text-align:right;color:var(--text-dim)">${d.docs_processed || 0}</td>
                        <td style="padding:7px 16px;text-align:right;color:var(--text-dim)">${d.llm_calls || 0}</td>
                        <td style="padding:7px 16px;text-align:right;color:var(--text-dim)">${d.avg_call_ms ? (d.avg_call_ms / 1000).toFixed(1) + 's' : '—'}</td>
                    </tr>`).join('')}
                </tbody>
            </table>
        </div>
    </div>`;
}

// ── ADMIN: PER-CLIENT BREAKDOWN ───────────────────────────────────────

let _pageUsageCache  = {};   // extractionId → [{page_num, ...}]
let _expandedDoc     = null; // extractionId currently expanded
let _clientDashboardFilters = {}; // userId -> {preset, date_from, date_to}
let _clientDashboardData = null;
let _activeClientDashboardUser = null;
let _userDocsFilter = { preset: 'today', date_from: _todayISO(), date_to: _todayISO() };
let _userDocsData = [];

function _renderClientTable(clients) {
    if (!clients || clients.length === 0) {
        return `<div style="color:var(--text-dim);padding:16px;font-size:11px">No clients yet.</div>`;
    }
    return `
    <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead>
            <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.08em">
                <th style="text-align:left;padding:8px 12px;font-weight:500">CLIENT</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">ROLE</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">EXTRACTIONS</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">PAGES</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">INPUT</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">OUTPUT</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">TOTAL</th>
                <th style="text-align:center;padding:8px 12px;font-weight:500">DOCS</th>
            </tr>
        </thead>
        <tbody id="clientTableBody">
            ${clients.map(c => _renderClientRow(c)).join('')}
        </tbody>
    </table>`;
}

function _renderClientRow(c) {
    const clientHash = `#/admin/client/${encodeURIComponent(c.user_id || '')}`;
    const roleBadge = c.role === 'admin'
        ? `<span style="color:var(--blue);font-weight:500">ADMIN</span>`
        : `<span style="color:var(--text-dim)">CLIENT</span>`;
    const activeStyle = c.is_active ? '' : 'opacity:0.5';
    return `
    <tr id="cr-${c.user_id}" style="border-bottom:1px solid var(--border);${activeStyle}"
        onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background=''">
        <td style="padding:9px 12px;font-weight:500">${escapeHtml(c.email)}</td>
        <td style="padding:9px 12px">${roleBadge}</td>
        <td style="padding:9px 12px;text-align:right;color:var(--text-mid)">${fmtNum(c.total_extractions)}</td>
        <td style="padding:9px 12px;text-align:right;color:var(--text-mid)">${fmtNum(c.total_pages)}</td>
        <td style="padding:9px 12px;text-align:right;color:var(--blue)">${fmtTokens(c.total_input_tokens)}</td>
        <td style="padding:9px 12px;text-align:right;color:var(--green)">${fmtTokens(c.total_output_tokens)}</td>
        <td style="padding:9px 12px;text-align:right;font-weight:500">${fmtTokens(c.grand_total)}</td>
        <td style="padding:9px 12px;text-align:center">
            <button class="small-btn" onclick="navigate('${escapeInlineJsString(clientHash)}')">
                View
            </button>
        </td>
    </tr>
    `;
}

function _renderDocRow(d) {
    const isExpanded = _expandedDoc === d.extraction_id;
    const statusColor = d.status === 'done' ? 'var(--green)' : d.status === 'error' ? 'var(--red,#e06c75)' : 'var(--amber)';
    const billablePages = Number(d.billable_pages || 0);
    const totalPages = Number(d.total_pages || 0);
    return `
    <tr id="dr-${d.extraction_id}" style="border-bottom:1px solid var(--border)"
        onmouseover="this.style.background='var(--bg1)'" onmouseout="this.style.background=''">
        <td style="padding:6px 8px;font-weight:500;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${escapeHtml(d.filename || '')}">${escapeHtml(d.filename || '—')}</td>
        <td style="padding:6px 8px;color:var(--text-mid)">${escapeHtml(d.vendor_name || d.vendor_id || '—')}</td>
        <td style="padding:6px 8px;color:var(--text-dim);white-space:nowrap">${fmtDate(d.created_at)}</td>
        <td style="padding:6px 8px;text-align:right;color:var(--text-mid);white-space:nowrap">${billablePages}/${totalPages} pages</td>
        <td style="padding:6px 8px;text-align:right;color:${statusColor};font-weight:500">${(d.status || '').toUpperCase()}</td>
        <td style="padding:6px 8px;text-align:right;color:var(--blue)">${fmtTokens(d.total_input_tokens)}</td>
        <td style="padding:6px 8px;text-align:right;color:var(--green)">${fmtTokens(d.total_output_tokens)}</td>
        <td style="padding:6px 8px;text-align:right;font-weight:500">${fmtTokens(d.grand_total)}</td>
        <td style="padding:6px 8px;text-align:right;color:var(--text-dim)">${fmtLatency(d.total_latency_ms)}</td>
        <td style="padding:6px 8px;text-align:center">
            ${d.llm_calls > 0 ? `<button class="small-btn" onclick="togglePageUsage(${d.extraction_id})"
                style="${isExpanded ? 'background:var(--blue);color:#fff' : ''}">${isExpanded ? 'Hide' : 'Detail'}</button>` : '—'}
        </td>
    </tr>
    ${isExpanded ? `<tr id="pages-${d.extraction_id}"><td colspan="10" style="padding:0 8px 8px 32px;background:var(--bg1)">${_renderPageUsage(d.extraction_id)}</td></tr>` : ''}`;
}

function _renderPageUsage(extractionId) {
    const pages = _pageUsageCache[extractionId];
    if (!pages) return `<div style="padding:8px;color:var(--text-dim);font-size:10px">Loading…</div>`;
    if (pages.length === 0) return `<div style="padding:8px;color:var(--text-dim);font-size:10px">No per-page data available.</div>`;
    return `
    <table style="border-collapse:collapse;font-size:10px;margin-top:4px">
        <thead>
            <tr style="color:var(--text-dim);letter-spacing:0.06em">
                <th style="text-align:right;padding:4px 10px;font-weight:500">PAGE</th>
                <th style="text-align:left;padding:4px 10px;font-weight:500">TYPE</th>
                <th style="text-align:right;padding:4px 10px;font-weight:500">INPUT</th>
                <th style="text-align:right;padding:4px 10px;font-weight:500">OUTPUT</th>
                <th style="text-align:right;padding:4px 10px;font-weight:500">TOTAL</th>
                <th style="text-align:right;padding:4px 10px;font-weight:500">LATENCY</th>
            </tr>
        </thead>
        <tbody>
            ${pages.map(p => `
            <tr style="border-bottom:1px solid var(--border)">
                <td style="padding:4px 10px;text-align:right;color:var(--blue)">${p.page_num ?? '—'}</td>
                <td style="padding:4px 10px;color:var(--text-dim)">${escapeHtml(p.call_type || 'llm')}</td>
                <td style="padding:4px 10px;text-align:right">${fmtTokens(p.prompt_tokens)}</td>
                <td style="padding:4px 10px;text-align:right;color:var(--green)">${fmtTokens(p.completion_tokens)}</td>
                <td style="padding:4px 10px;text-align:right;font-weight:500">${fmtTokens(p.total_tokens)}</td>
                <td style="padding:4px 10px;text-align:right;color:var(--text-dim)">${p.duration_ms ? (p.duration_ms / 1000).toFixed(1) + 's' : '—'}</td>
            </tr>`).join('')}
        </tbody>
    </table>`;
}

async function togglePageUsage(extractionId) {
    if (_expandedDoc === extractionId) {
        _expandedDoc = null;
    } else {
        _expandedDoc = extractionId;
        _refreshClientTable();
        _refreshClientDashboardDocs();
        _refreshUserDocsDashboard();
        if (!_pageUsageCache[extractionId]) {
            const authUser = (() => { try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch { return null; } })();
            const isAdmin = authUser && authUser.role === 'admin';
            const pageUrl = isAdmin
                ? `/admin/usage/extractions/${extractionId}/pages`
                : `/user/extractions/${extractionId}/pages`;
            try {
                _pageUsageCache[extractionId] = await apiJSON(pageUrl);
            } catch (e) {
                _pageUsageCache[extractionId] = [];
                showToast('Failed to load page data: ' + e.message);
            }
        }
    }
    _refreshClientTable();
    _refreshClientDashboardDocs();
    _refreshUserDocsDashboard();
}

function _refreshUserDocsDashboard() {
    const el = document.getElementById('userDocsTableBody');
    if (!el) return;
    el.innerHTML = _userDocsData.map(d => _renderDocRow(d)).join('');
}

let _clientsData = [];

function _refreshClientTable() {
    const el = document.getElementById('clientTableBody');
    if (!el) return;
    el.innerHTML = _clientsData.map(c => _renderClientRow(c)).join('');
}

function _refreshClientDashboardDocs() {
    const el = document.getElementById('clientDashboardDocsBody');
    if (!el || !_clientDashboardData) return;
    el.innerHTML = (_clientDashboardData.documents || []).map(d => _renderDocRow(d)).join('');
}

// ── CLIENT: OWN PDF USAGE FILTERS ────────────────────────────────────

function _userDocsQuery(filter) {
    const params = new URLSearchParams();
    params.set('limit', '200');
    if (filter.preset === 'all') {
        params.set('range', 'all');
    } else {
        if (filter.date_from) params.set('date_from', filter.date_from);
        if (filter.date_to)   params.set('date_to', filter.date_to);
    }
    return params.toString();
}

function _renderUserDocsFilterRow(filter) {
    function btn(label, preset) {
        const active = filter.preset === preset;
        return `<button class="small-btn" onclick="setUserDocsRange('${preset}')"
                style="${active ? 'background:var(--blue);color:#fff' : ''}">${label}</button>`;
    }
    return `
    <div style="display:flex;align-items:end;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:14px 0">
        <div style="display:flex;gap:8px;flex-wrap:wrap">
            ${btn('Today', 'today')}
            ${btn('7D', '7d')}
            ${btn('30D', '30d')}
            ${btn('All', 'all')}
        </div>
        <div style="display:flex;align-items:end;gap:8px;flex-wrap:wrap">
            <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em">FROM
                <input id="userDateFrom" type="date" value="${escapeHtml(filter.date_from || '')}"
                       style="display:block;margin-top:4px;background:var(--bg1);border:1px solid var(--border);color:var(--text);padding:6px 8px;font-family:var(--mono);font-size:11px">
            </label>
            <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em">TO
                <input id="userDateTo" type="date" value="${escapeHtml(filter.date_to || '')}"
                       style="display:block;margin-top:4px;background:var(--bg1);border:1px solid var(--border);color:var(--text);padding:6px 8px;font-family:var(--mono);font-size:11px">
            </label>
            <button class="small-btn" onclick="applyUserDocsDates()">Apply</button>
        </div>
    </div>`;
}

function setUserDocsRange(preset) {
    const today = _todayISO();
    if (preset === 'today')     _userDocsFilter = { preset, date_from: today, date_to: today };
    else if (preset === '7d')   _userDocsFilter = { preset, date_from: _shiftDateISO(6), date_to: today };
    else if (preset === '30d')  _userDocsFilter = { preset, date_from: _shiftDateISO(29), date_to: today };
    else                        _userDocsFilter = { preset: 'all', date_from: '', date_to: '' };
    renderDashboardPage(document.getElementById('appRoot'));
}

function applyUserDocsDates() {
    const from = document.getElementById('userDateFrom')?.value || '';
    const to   = document.getElementById('userDateTo')?.value || '';
    _userDocsFilter = { preset: 'custom', date_from: from, date_to: to };
    renderDashboardPage(document.getElementById('appRoot'));
}

function _renderUserDocsTable(docs) {
    if (!docs || docs.length === 0) {
        return `<div style="padding:18px;color:var(--text-dim);font-size:11px">No PDFs found for this date range.</div>`;
    }
    return `
    <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:10px">
            <thead>
                <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.07em">
                    <th style="text-align:left;padding:8px;font-weight:500">FILENAME</th>
                    <th style="text-align:left;padding:8px;font-weight:500">VENDOR</th>
                    <th style="text-align:left;padding:8px;font-weight:500">DATE</th>
                    <th style="text-align:right;padding:8px;font-weight:500">BILLABLE PAGES</th>
                    <th style="text-align:right;padding:8px;font-weight:500">STATUS</th>
                    <th style="text-align:right;padding:8px;font-weight:500">INPUT</th>
                    <th style="text-align:right;padding:8px;font-weight:500">OUTPUT</th>
                    <th style="text-align:right;padding:8px;font-weight:500">TOTAL</th>
                    <th style="text-align:right;padding:8px;font-weight:500">LATENCY</th>
                    <th style="text-align:center;padding:8px;font-weight:500">DETAIL</th>
                </tr>
            </thead>
            <tbody id="userDocsTableBody">
                ${docs.map(d => _renderDocRow(d)).join('')}
            </tbody>
        </table>
    </div>`;
}

// ── MAIN RENDER ───────────────────────────────────────────────────────
async function renderDashboardPage(app) {
    const authUser = (() => { try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch { return null; } })();
    const isAdmin = authUser && authUser.role === 'admin';

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">Dashboard</div>
        <div style="color:var(--text-dim);font-size:11px;padding:24px 0">Loading usage data…</div>
    </div>`;
    updateNavActive();

    // Admin sees system-wide stats; clients see their own
    const statsEndpoint = isAdmin ? '/admin/stats' : '/user/stats';
    let stats = {}, days = [];
    try {
        const data = await apiJSON(statsEndpoint);
        stats = data.stats || {};
        days  = data.days  || [];
    } catch (e) {
        console.warn('Dashboard fetch error:', e);
    }

    // Admin also fetches per-client breakdown; clients fetch their own docs
    _clientsData = [];
    _userDocsData = [];
    _pageUsageCache  = {};
    _expandedDoc     = null;
    if (isAdmin) {
        try { _clientsData = await apiJSON('/admin/usage/clients'); } catch (e) { console.warn(e); }
    } else {
        try {
            _userDocsData = await apiJSON(`/user/documents?${_userDocsQuery(_userDocsFilter)}`);
        } catch (e) { console.warn(e); }
    }

    const totalInput  = Number(stats.total_input_tokens  || 0);
    const totalOutput = Number(stats.total_output_tokens || 0);
    const grandTotal  = totalInput + totalOutput;

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">Dashboard${isAdmin ? '' : ' — My Usage'}</div>

        <!-- KPI strip -->
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px">
            ${_kpiCard('TOTAL PAGES', fmtNum(stats.total_pages), 'var(--blue)', 'pages processed')}
            ${_kpiCard('EXTRACTIONS', fmtNum(stats.total_extractions), 'var(--green)', 'completed')}
            ${_kpiCard('INPUT TOKENS', fmtTokens(totalInput), 'var(--blue)', 'prompt tokens')}
            ${_kpiCard('OUTPUT TOKENS', fmtTokens(totalOutput), 'var(--green)', 'completion tokens')}
            ${_kpiCard('GRAND TOTAL', fmtTokens(grandTotal), 'var(--amber)', 'all tokens')}
            ${_kpiCard('LLM CALLS', fmtNum(stats.total_llm_calls), 'var(--text-mid)', 'model invocations')}
        </div>

        <!-- Charts row -->
        <div style="display:grid;grid-template-columns:1fr 260px;gap:16px;align-items:start">
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:18px 20px">
                <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:6px">
                    TOKEN TREND — LAST 30 DAYS
                </div>
                <div style="display:flex;gap:20px;margin-bottom:12px">
                    <span style="display:flex;align-items:center;gap:6px;font-size:9px;color:var(--text-dim)">
                        <span style="display:inline-block;width:22px;height:2px;background:#3b82f6;border-radius:1px"></span>Input
                    </span>
                    <span style="display:flex;align-items:center;gap:6px;font-size:9px;color:var(--text-dim)">
                        <span style="display:inline-block;width:22px;height:2px;background:#22c55e;border-radius:1px"></span>Output
                    </span>
                </div>
                ${_renderLineChart(days)}
            </div>
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:18px 20px;text-align:center">
                <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:12px">
                    INPUT vs OUTPUT SPLIT
                </div>
                ${_renderPieChart(totalInput, totalOutput)}
            </div>
        </div>

        ${_renderDailyTable(days)}

        ${!isAdmin ? `
        <!-- Per-document PDF usage (client only) -->
        <div style="margin-top:24px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
            <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
                <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">PDF USAGE</span>
                <span style="font-size:9px;color:var(--text-dim)">${_userDocsData.length} PDF${_userDocsData.length !== 1 ? 's' : ''}</span>
            </div>
            <div style="padding:0 16px">
                ${_renderUserDocsFilterRow(_userDocsFilter)}
            </div>
            ${_renderUserDocsTable(_userDocsData)}
        </div>` : ''}

        ${isAdmin ? `
        <!-- Per-client breakdown (admin only) -->
        <div style="margin-top:24px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
            <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
                <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">CLIENT TOKEN BREAKDOWN</span>
                <span style="font-size:9px;color:var(--text-dim)">${_clientsData.length} client${_clientsData.length !== 1 ? 's' : ''}</span>
            </div>
            <div style="overflow-x:auto">
                ${_renderClientTable(_clientsData)}
            </div>
        </div>` : ''}
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${isAdmin ? 'SYSTEM' : 'MY'} USAGE DASHBOARD</span>
        <button class="small-btn" onclick="renderDashboardPage(document.getElementById('appRoot'))"
                style="margin-left:12px">REFRESH</button>
    </div>`;
    updateNavActive();
}

function _clientDashboardFilter(userId) {
    if (!_clientDashboardFilters[userId]) {
        const today = _todayISO();
        _clientDashboardFilters[userId] = { preset: 'today', date_from: today, date_to: today };
    }
    return _clientDashboardFilters[userId];
}

function _clientDashboardQuery(filter) {
    const params = new URLSearchParams();
    params.set('limit', '200');
    if (filter.preset === 'all') {
        params.set('range', 'all');
    } else {
        if (filter.date_from) params.set('date_from', filter.date_from);
        if (filter.date_to) params.set('date_to', filter.date_to);
    }
    return params.toString();
}

function _renderDateFilterRow(userId, filter) {
    function btn(label, preset) {
        const active = filter.preset === preset;
        return `<button class="small-btn" onclick="setClientDashboardRange('${escapeInlineJsString(userId)}','${preset}')"
                style="${active ? 'background:var(--blue);color:#fff' : ''}">${label}</button>`;
    }
    return `
    <div style="display:flex;align-items:end;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:18px 0">
        <div style="display:flex;gap:8px;flex-wrap:wrap">
            ${btn('Today', 'today')}
            ${btn('7D', '7d')}
            ${btn('30D', '30d')}
            ${btn('All', 'all')}
        </div>
        <div style="display:flex;align-items:end;gap:8px;flex-wrap:wrap">
            <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em">FROM
                <input id="clientDateFrom" type="date" value="${escapeHtml(filter.date_from || '')}"
                       style="display:block;margin-top:4px;background:var(--bg1);border:1px solid var(--border);color:var(--text);padding:6px 8px;font-family:var(--mono);font-size:11px">
            </label>
            <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em">TO
                <input id="clientDateTo" type="date" value="${escapeHtml(filter.date_to || '')}"
                       style="display:block;margin-top:4px;background:var(--bg1);border:1px solid var(--border);color:var(--text);padding:6px 8px;font-family:var(--mono);font-size:11px">
            </label>
            <button class="small-btn" onclick="applyClientDashboardDates('${escapeInlineJsString(userId)}')">Apply</button>
        </div>
    </div>`;
}

function setClientDashboardRange(userId, preset) {
    const today = _todayISO();
    if (preset === 'today') {
        _clientDashboardFilters[userId] = { preset, date_from: today, date_to: today };
    } else if (preset === '7d') {
        _clientDashboardFilters[userId] = { preset, date_from: _shiftDateISO(6), date_to: today };
    } else if (preset === '30d') {
        _clientDashboardFilters[userId] = { preset, date_from: _shiftDateISO(29), date_to: today };
    } else {
        _clientDashboardFilters[userId] = { preset: 'all', date_from: '', date_to: '' };
    }
    renderClientDashboardPage(document.getElementById('appRoot'), userId);
}

function applyClientDashboardDates(userId) {
    const from = document.getElementById('clientDateFrom')?.value || '';
    const to = document.getElementById('clientDateTo')?.value || '';
    _clientDashboardFilters[userId] = { preset: 'custom', date_from: from, date_to: to };
    renderClientDashboardPage(document.getElementById('appRoot'), userId);
}

function _renderClientDashboardDocs(docs) {
    if (!docs || docs.length === 0) {
        return `<div style="padding:18px;color:var(--text-dim);font-size:11px">No PDFs found for this date range.</div>`;
    }
    return `
    <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:10px">
            <thead>
                <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.07em">
                    <th style="text-align:left;padding:8px;font-weight:500">FILENAME</th>
                    <th style="text-align:left;padding:8px;font-weight:500">VENDOR</th>
                    <th style="text-align:left;padding:8px;font-weight:500">DATE</th>
                    <th style="text-align:right;padding:8px;font-weight:500">BILLABLE PAGES</th>
                    <th style="text-align:right;padding:8px;font-weight:500">STATUS</th>
                    <th style="text-align:right;padding:8px;font-weight:500">INPUT</th>
                    <th style="text-align:right;padding:8px;font-weight:500">OUTPUT</th>
                    <th style="text-align:right;padding:8px;font-weight:500">TOTAL</th>
                    <th style="text-align:right;padding:8px;font-weight:500">LATENCY</th>
                    <th style="text-align:center;padding:8px;font-weight:500">DETAIL</th>
                </tr>
            </thead>
            <tbody id="clientDashboardDocsBody">
                ${docs.map(d => _renderDocRow(d)).join('')}
            </tbody>
        </table>
    </div>`;
}

async function renderClientDashboardPage(app, userId) {
    _activeClientDashboardUser = userId;
    const filter = _clientDashboardFilter(userId);
    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:18px">
            <button class="small-btn" onclick="navigate('#/dashboard')">Back</button>
            <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.12em">ADMIN DASHBOARD / CLIENT</div>
        </div>
        <div class="page-title">Viewing Client</div>
        <div style="color:var(--text-dim);font-size:11px;padding:18px 0">Loading client usage...</div>
    </div>`;
    updateNavActive();

    let data;
    try {
        data = await apiJSON(`/admin/usage/clients/${encodeURIComponent(userId)}/dashboard?${_clientDashboardQuery(filter)}`);
    } catch (e) {
        app.innerHTML = headerHTML() + `
        <div class="page-content">
            <button class="small-btn" onclick="navigate('#/dashboard')" style="margin-bottom:18px">Back</button>
            <div class="page-title">Client Dashboard Error</div>
            <div style="color:var(--red,#e06c75);font-size:12px;margin-top:12px">${escapeHtml(e.message || String(e))}</div>
        </div>`;
        updateNavActive();
        return;
    }

    _clientDashboardData = data;
    const s = data.stats || {};
    const inputTokens = Number(s.input_tokens || 0);
    const outputTokens = Number(s.output_tokens || 0);
    const client = data.client || {};

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
            <button class="small-btn" onclick="navigate('#/dashboard')">Back</button>
            <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.12em">ADMIN DASHBOARD / ${escapeHtml(client.email || userId)}</div>
        </div>
        <div class="page-title">Viewing: ${escapeHtml(client.email || userId)}</div>

        ${_renderDateFilterRow(userId, filter)}

        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px">
            ${_kpiCard('PDFS', fmtNum(s.todays_pdfs), 'var(--blue)', 'selected range')}
            ${_kpiCard('BILLABLE PAGES', fmtNum(s.billable_pages), 'var(--green)', 'pages with extraction tokens')}
            ${_kpiCard('UNBILLED PAGES', fmtNum(s.unbilled_pages), 'var(--amber)', 'not charged as extraction pages')}
            ${_kpiCard('INPUT TOKENS', fmtTokens(inputTokens), 'var(--blue)', 'prompt tokens')}
            ${_kpiCard('OUTPUT TOKENS', fmtTokens(outputTokens), 'var(--green)', 'completion tokens')}
            ${_kpiCard('COST ESTIMATE', fmtCurrency(s.cost_estimate), 'var(--amber)', s.currency || 'USD')}
        </div>

        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:18px 20px;margin-bottom:20px">
            <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:12px">CLIENT TOKEN TREND</div>
            ${_renderLineChart(data.days || [])}
        </div>

        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
            <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
                <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">PDF USAGE</span>
                <span style="font-size:9px;color:var(--text-dim)">${(data.documents || []).length} PDF${(data.documents || []).length === 1 ? '' : 's'}</span>
            </div>
            ${_renderClientDashboardDocs(data.documents || [])}
        </div>
    </div>`;
    updateNavActive();
}
