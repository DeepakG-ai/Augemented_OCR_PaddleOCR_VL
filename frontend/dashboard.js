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

// ── BAR CHART (daily input vs output tokens, last 10 days) ────────────
function _renderBarChart(days) {
    if (!days || days.length === 0) {
        return `<div style="color:var(--text-dim);text-align:center;padding:48px 0;font-size:10px">No data yet</div>`;
    }

    // Take only the last 10 days
    const trimmed = days.slice(0, 10);
    const sorted = [...trimmed].reverse();
    const W = 500, H = 170;
    const PAD = { top: 14, right: 16, bottom: 28, left: 54 };
    const cW = W - PAD.left - PAD.right;
    const cH = H - PAD.top  - PAD.bottom;
    const n = sorted.length;

    const maxVal = Math.max(
        ...sorted.map(d => Math.max(Number(d.input_tokens || 0), Number(d.output_tokens || 0))),
        1
    );

    function yP(v) { return PAD.top + cH - (Number(v || 0) / maxVal) * cH; }

    const gridLines = [0.25, 0.5, 0.75, 1.0].map(f => {
        const y = PAD.top + cH * (1 - f);
        return `<line x1="${PAD.left}" y1="${y}" x2="${W - PAD.right}" y2="${y}"
                      stroke="var(--border)" stroke-width="1"/>
                <text x="${PAD.left - 5}" y="${y + 4}" text-anchor="end"
                      fill="var(--text-dim)" font-size="9" font-family="JetBrains Mono,monospace"
                      >${fmtTokens(maxVal * f)}</text>`;
    }).join('');

    const baseY = PAD.top + cH;
    const slotW = cW / Math.max(n, 1);
    const groupGap = Math.min(12, slotW * 0.22);
    const groupW = Math.min(42, Math.max(6, slotW - groupGap));
    const barGap = groupW >= 5 ? Math.min(4, groupW * 0.16) : 0.5;
    const barW = Math.max(2, (groupW - barGap) / 2);

    function groupX(i) { return PAD.left + i * slotW + (slotW - groupW) / 2; }
    function groupCenter(i) { return groupX(i) + groupW / 2; }

    // Show all x-axis labels since we only have ≤10 bars
    const xLabels = sorted.map((d, i) => {
        const label = String(d.day || '').slice(5);
        return `<text x="${groupCenter(i)}" y="${H - 5}" text-anchor="middle"
                      fill="var(--text-dim)" font-size="9" font-family="JetBrains Mono,monospace"
                      >${label}</text>`;
    }).join('');

    function barRect(d, i, key, color, label, offset) {
        const value = Number(d[key] || 0);
        const height = value > 0 ? Math.max(1, baseY - yP(value)) : 0;
        const x = groupX(i) + offset;
        const y = baseY - height;
        return `<rect x="${x}" y="${y}" width="${barW}" height="${height}" rx="1.5" fill="${color}" opacity="0.9">
                    <title>${escapeHtml(String(d.day || '').slice(5))}: ${fmtTokens(value)} ${label}</title>
                </rect>`;
    }

    const bars = sorted.map((d, i) =>
        barRect(d, i, 'input_tokens', '#3b82f6', 'input', 0) +
        barRect(d, i, 'output_tokens', '#22c55e', 'output', barW + barGap)
    ).join('');

    return `
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" style="display:block" preserveAspectRatio="xMidYMid meet">
        ${gridLines}
        <line x1="${PAD.left}" y1="${baseY}" x2="${W - PAD.right}" y2="${baseY}"
              stroke="var(--border)" stroke-width="1"/>
        ${bars}
        ${xLabels}
    </svg>`;
}

// ── LINE CHART (daily LLM calls vs total documents) ───────────────────
function _renderLineChart(days) {
    if (!days || days.length === 0) {
        return `<div style="color:var(--text-dim);text-align:center;padding:48px 0;font-size:10px">No data yet</div>`;
    }

    const trimmed = days.slice(0, 10);
    const sorted  = [...trimmed].reverse();
    const n = sorted.length;

    const W = 500, H = 170;
    const PAD = { top: 16, right: 20, bottom: 28, left: 44 };
    const cW = W - PAD.left - PAD.right;
    const cH = H - PAD.top  - PAD.bottom;

    const dataMax = Math.max(
        ...sorted.map(d => Math.max(Number(d.llm_calls || 0), Number(d.docs_processed || 0))),
        1
    );

    // Nice-number Y axis: snap interval to 1,2,5,10,20,50,100…
    function niceAxis(rawMax) {
        const targetTicks = 4;
        const rawInterval = rawMax / targetTicks;
        const mag = Math.pow(10, Math.floor(Math.log10(rawInterval || 1)));
        const norm = rawInterval / mag;
        const interval = norm <= 1 ? mag : norm <= 2 ? 2 * mag : norm <= 5 ? 5 * mag : 10 * mag;
        const axisMax = Math.ceil(rawMax / interval) * interval;
        const ticks = [];
        for (let t = interval; t <= axisMax; t += interval) ticks.push(t);
        return { axisMax, ticks };
    }
    const { axisMax, ticks } = niceAxis(dataMax);

    function xP(i) { return PAD.left + (n <= 1 ? cW / 2 : (i / (n - 1)) * cW); }
    function yP(v) { return PAD.top + cH - (Number(v || 0) / axisMax) * cH; }

    const gridLines = ticks.map(t => {
        const y = PAD.top + cH - (t / axisMax) * cH;
        return `<line x1="${PAD.left}" y1="${y}" x2="${W - PAD.right}" y2="${y}"
                      stroke="var(--border)" stroke-width="1" stroke-dasharray="3,3"/>
                <text x="${PAD.left - 5}" y="${y + 4}" text-anchor="end"
                      fill="var(--text-dim)" font-size="9" font-family="JetBrains Mono,monospace">${t}</text>`;
    }).join('');

    const llmPts  = sorted.map((d, i) => `${xP(i)},${yP(d.llm_calls)}`).join(' ');
    const docsPts = sorted.map((d, i) => `${xP(i)},${yP(d.docs_processed)}`).join(' ');

    const llmDots = sorted.map((d, i) => `
        <circle cx="${xP(i)}" cy="${yP(d.llm_calls)}" r="3" fill="#3b82f6" stroke="var(--bg1)" stroke-width="1.5">
            <title>${String(d.day || '').slice(5)}: ${d.llm_calls || 0} LLM calls</title>
        </circle>`).join('');

    const docsDots = sorted.map((d, i) => `
        <circle cx="${xP(i)}" cy="${yP(d.docs_processed)}" r="3" fill="#22c55e" stroke="var(--bg1)" stroke-width="1.5">
            <title>${String(d.day || '').slice(5)}: ${d.docs_processed || 0} documents</title>
        </circle>`).join('');

    const step = Math.max(1, Math.floor(n / 7));
    const xLabels = sorted.map((d, i) => {
        if (i % step !== 0 && i !== n - 1) return '';
        return `<text x="${xP(i)}" y="${H - 4}" text-anchor="middle"
                      fill="var(--text-dim)" font-size="9" font-family="JetBrains Mono,monospace"
                      >${String(d.day || '').slice(5)}</text>`;
    }).join('');

    return `
    <svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block">
        ${gridLines}
        <line x1="${PAD.left}" y1="${PAD.top + cH}" x2="${W - PAD.right}" y2="${PAD.top + cH}"
              stroke="var(--border)" stroke-width="1"/>
        <polyline points="${llmPts}"  fill="none" stroke="#3b82f6" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round"/>
        <polyline points="${docsPts}" fill="none" stroke="#22c55e" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round"/>
        ${llmDots}
        ${docsDots}
        ${xLabels}
    </svg>
    <div style="display:flex;justify-content:center;gap:20px;margin-top:8px">
        <span style="display:flex;align-items:center;gap:6px;font-size:9px;color:var(--text-dim)">
            <span style="display:inline-block;width:18px;height:2px;background:#3b82f6;border-radius:1px"></span>LLM CALLS
        </span>
        <span style="display:flex;align-items:center;gap:6px;font-size:9px;color:var(--text-dim)">
            <span style="display:inline-block;width:18px;height:2px;background:#22c55e;border-radius:1px"></span>TOTAL DOCUMENTS
        </span>
    </div>`;
}

// ── DAILY BREAKDOWN TABLE ─────────────────────────────────────────────
function _renderDailyTable(days, isAdmin) {
    if (!days || days.length === 0) return '';
    const displayDays = days.slice(0, 10);
    return `
    <div style="margin-top:20px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
        <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">DAILY BREAKDOWN</span>
            <span style="font-size:9px;color:var(--text-dim)">${displayDays.length} of ${days.length} days</span>
        </div>
        <div style="overflow-x:auto">
            <table style="width:100%;min-width:${isAdmin ? '700px' : '500px'};border-collapse:collapse;font-size:10px">
                <thead>
                    <tr style="border-bottom:1px solid var(--border)">
                        <th style="text-align:left;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em;white-space:nowrap">DATE</th>
                        ${isAdmin ? `
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em;white-space:nowrap">INPUT</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em;white-space:nowrap">OUTPUT</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em;white-space:nowrap">TOTAL</th>` : ''}
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em;white-space:nowrap">DOCS</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em;white-space:nowrap">${isAdmin ? 'CALLS' : 'PAGES EXTRACTED'}</th>
                        <th style="text-align:right;padding:8px 16px;color:var(--text-dim);font-weight:500;letter-spacing:0.08em;white-space:nowrap">AVG LATENCY</th>
                    </tr>
                </thead>
                <tbody>
                    ${displayDays.map((d, idx) => `
                    <tr style="border-bottom:1px solid var(--border);${idx % 2 === 1 ? 'background:var(--bg2)' : ''}">
                        <td style="padding:7px 16px;color:var(--text-mid);font-family:var(--mono);white-space:nowrap">${String(d.day || '').slice(0, 10)}</td>
                        ${isAdmin ? `
                        <td style="padding:7px 16px;text-align:right;color:var(--blue)">${fmtTokens(d.input_tokens)}</td>
                        <td style="padding:7px 16px;text-align:right;color:var(--green)">${fmtTokens(d.output_tokens)}</td>
                        <td style="padding:7px 16px;text-align:right;color:var(--text)">${fmtTokens(d.total_tokens)}</td>` : ''}
                        <td style="padding:7px 16px;text-align:right;color:var(--text-dim)">${d.docs_processed || 0}</td>
                        <td style="padding:7px 16px;text-align:right;color:var(--blue)">${d.llm_calls || 0}</td>
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

function _renderClientTable(clients, errorMsg = null) {
    if (errorMsg) {
        return `<div style="background:rgba(224,108,117,0.12);border:1px solid var(--red,#e06c75);border-radius:4px;padding:12px 16px;margin:12px;font-size:11px;color:var(--red,#e06c75)">&#9888; Could not load client breakdown: ${escapeHtml(errorMsg)}</div>`;
    }
    if (!clients || clients.length === 0) {
        return `<div style="color:var(--text-dim);padding:16px;font-size:11px">No clients yet.</div>`;
    }
    return `
    <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead>
            <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.08em">
                <th style="text-align:left;padding:8px 12px;font-weight:500">CLIENT</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">ROLE</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">HISTORY</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">PAGES USED</th>
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
        <td style="padding:9px 12px;text-align:right;color:var(--text-mid)">${fmtNum(c.billable_pages)}</td>
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
    const _au = (() => { try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch { return null; } })();
    const showTokens = _au && _au.role === 'admin';
    const colSpan = showTokens ? 10 : 7;
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
        ${showTokens ? `
        <td style="padding:6px 8px;text-align:right;color:var(--blue)">${fmtTokens(d.total_input_tokens)}</td>
        <td style="padding:6px 8px;text-align:right;color:var(--green)">${fmtTokens(d.total_output_tokens)}</td>
        <td style="padding:6px 8px;text-align:right;font-weight:500">${fmtTokens(d.grand_total)}</td>` : ''}
        <td style="padding:6px 8px;text-align:right;color:var(--text-dim)">${fmtLatency(d.total_latency_ms)}</td>
        <td style="padding:6px 8px;text-align:center">
            ${d.llm_calls > 0 ? `<button class="small-btn" onclick="togglePageUsage(${d.extraction_id})"
                style="${isExpanded ? 'background:var(--blue);color:#fff' : ''}">${isExpanded ? 'Hide' : 'Detail'}</button>` : '—'}
        </td>
    </tr>
    ${isExpanded ? `<tr id="pages-${d.extraction_id}"><td colspan="${colSpan}" style="padding:0 8px 8px 32px;background:var(--bg1)">${_renderPageUsage(d.extraction_id)}</td></tr>` : ''}`;
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

function _renderUserDocsTable(docs, errorMsg = null) {
    if (errorMsg) {
        return `<div style="background:rgba(224,108,117,0.12);border:1px solid var(--red,#e06c75);border-radius:4px;padding:12px 16px;margin:12px;font-size:11px;color:var(--red,#e06c75)">&#9888; Could not load PDF usage: ${escapeHtml(errorMsg)}</div>`;
    }
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
    let stats = {}, days = [], subscription = null;
    let _statsLoadFailed = false;
    let _clientsLoadError = null;
    let _userDocsLoadError = null;
    try {
        const data = await apiJSON(statsEndpoint);
        stats = data.stats || {};
        days  = data.days  || [];
        subscription = data.subscription || null;
    } catch (e) {
        console.warn('Dashboard fetch error:', e);
        _statsLoadFailed = true;
    }

    // Admin also fetches per-client breakdown; clients fetch their own docs
    _clientsData = [];
    _userDocsData = [];
    _pageUsageCache  = {};
    _expandedDoc     = null;
    if (isAdmin) {
        try {
            _clientsData = await apiJSON('/admin/usage/clients');
        } catch (e) {
            console.warn(e);
            _clientsLoadError = e.message || 'Failed to load client breakdown';
        }
    } else {
        try {
            _userDocsData = await apiJSON(`/user/documents?${_userDocsQuery(_userDocsFilter)}`);
        } catch (e) {
            console.warn(e);
            _userDocsLoadError = e.message || 'Failed to load document usage';
        }
    }

    const totalInput  = Number(stats.total_input_tokens  || 0);
    const totalOutput = Number(stats.total_output_tokens || 0);
    const grandTotal  = totalInput + totalOutput;

    // Subscription quota bar (client only)
    const subLimit = subscription ? Number(subscription.subscription_limit || 0) : 0;
    const subUsed  = subscription ? Number(subscription.billable_pages || 0) : 0;
    const subPct   = subLimit > 0 ? Math.min(100, Math.round(subUsed / subLimit * 100)) : 0;
    const subColor = subPct >= 90 ? 'var(--red,#e06c75)' : subPct >= 70 ? 'var(--amber)' : 'var(--green)';

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">Dashboard${isAdmin ? '' : ' — My Usage'}</div>

        ${_statsLoadFailed ? `
        <div style="background:rgba(224,108,117,0.12);border:1px solid var(--red,#e06c75);border-radius:4px;padding:10px 16px;margin-bottom:16px;font-size:11px;color:var(--red,#e06c75)">
            &#9888; Usage statistics could not be loaded — figures below may be stale or unavailable.
            <button onclick="renderDashboardPage(document.getElementById('appRoot'))"
                style="background:none;border:1px solid currentColor;border-radius:3px;color:inherit;font-size:10px;padding:2px 8px;margin-left:12px;cursor:pointer">Retry</button>
        </div>` : ''}

        <!-- KPI strip -->
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:${(!isAdmin && subLimit > 0) ? '14px' : '24px'}">
            ${_kpiCard('PAGES USED', fmtNum(stats.billable_pages), 'var(--blue)', 'lifetime pages (billing)')}
            ${_kpiCard(isAdmin ? 'HISTORY ITEMS' : 'TOTAL DOCUMENTS', fmtNum(stats.total_extractions), 'var(--green)', isAdmin ? 'visible extractions' : 'documents processed')}
            ${isAdmin ? `
            ${_kpiCard('INPUT TOKENS', fmtTokens(totalInput), 'var(--blue)', 'prompt tokens')}
            ${_kpiCard('OUTPUT TOKENS', fmtTokens(totalOutput), 'var(--green)', 'completion tokens')}
            ${_kpiCard('GRAND TOTAL', fmtTokens(grandTotal), 'var(--amber)', 'all tokens')}
            ${_kpiCard('LLM CALLS', fmtNum(stats.total_llm_calls), 'var(--text-mid)', 'model invocations')}` : ''}
        </div>

        <!-- Subscription quota bar (client users with a limit set) -->
        ${!isAdmin && subLimit > 0 ? `
        <div style="margin-bottom:24px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:12px 18px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-size:9px;letter-spacing:0.12em;color:var(--text-dim)">SUBSCRIPTION QUOTA</span>
                <div style="display:flex;align-items:center;gap:12px">
                    <span style="font-size:10px;color:var(--text-mid)">${fmtNum(subUsed)} / ${fmtNum(subLimit)} pages</span>
                    <button onclick="openTopupRequestModal()"
                        style="background:none;border:1px solid ${subPct >= 80 ? 'var(--amber,#e5c07b)' : 'var(--border)'};
                               color:${subPct >= 80 ? 'var(--amber,#e5c07b)' : 'var(--text-dim)'};
                               border-radius:3px;cursor:pointer;padding:3px 10px;font-size:9px;
                               font-family:var(--mono);letter-spacing:0.08em;font-weight:600;
                               transition:all 0.15s"
                        onmouseover="this.style.borderColor='var(--amber,#e5c07b)';this.style.color='var(--amber,#e5c07b)'"
                        onmouseout="this.style.borderColor='${subPct >= 80 ? 'var(--amber,#e5c07b)' : 'var(--border)'}';this.style.color='${subPct >= 80 ? 'var(--amber,#e5c07b)' : 'var(--text-dim)'}'">
                        ${subPct >= 100 ? '⚠ REQUEST TOP-UP' : '+ REQUEST TOP-UP'}
                    </button>
                </div>
            </div>
            <div style="height:6px;background:var(--bg2,#2a2a2a);border-radius:3px;overflow:hidden">
                <div style="height:100%;background:${subColor};width:${subPct}%;transition:width 0.3s"></div>
            </div>
            <div style="display:flex;justify-content:space-between;margin-top:5px">
                <span style="font-size:9px;color:${subPct >= 100 ? 'var(--red,#e06c75)' : 'var(--text-dim)'}">
                    ${subPct >= 100 ? '⚠ Quota exhausted — uploads blocked' : fmtNum(Math.max(0, subLimit - subUsed)) + ' pages remaining'}
                </span>
                <span style="font-size:9px;color:var(--text-dim)">${subPct}% used</span>
            </div>
        </div>` : (!isAdmin && subLimit === 0 ? `
        <div style="margin-bottom:24px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:12px 18px;display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:10px;color:var(--text-dim)">No active subscription — contact admin to set up a plan.</span>
            <button onclick="openTopupRequestModal()"
                style="background:none;border:1px solid var(--border);color:var(--text-dim);
                       border-radius:3px;cursor:pointer;padding:3px 10px;font-size:9px;
                       font-family:var(--mono);letter-spacing:0.08em;transition:all 0.15s"
                onmouseover="this.style.borderColor='var(--amber,#e5c07b)';this.style.color='var(--amber,#e5c07b)'"
                onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
                + REQUEST TOP-UP
            </button>
        </div>` : '')}

        ${isAdmin ? `
        <!-- Admin: 50/50 split — Line chart (left) | Vendor Breakdown (right) -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:stretch">

            <!-- LEFT: Daily activity line chart -->
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:14px 16px;min-width:0;overflow:hidden">
                <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:12px">
                    DAILY BREAKDOWN
                </div>
                ${_renderLineChart(days)}
            </div>

            <!-- RIGHT: Vendor Breakdown by Client -->
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
                <div style="padding:12px 16px;border-bottom:1px solid var(--border)">
                    <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">VENDOR BREAKDOWN BY CLIENT</span>
                </div>
                <div id="vendorSummaryTable" style="overflow-x:auto">
                    <div style="color:var(--text-dim);padding:14px;font-size:11px">Loading vendor data…</div>
                </div>
            </div>
        </div>` : `
        <!-- User: 50/50 split — Line chart (left) | Vendor Breakdown (right) -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:stretch">

            <!-- LEFT: Daily activity line chart -->
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:14px 16px;min-width:0;overflow:hidden">
                <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:12px">
                    DAILY BREAKDOWN
                </div>
                ${_renderLineChart(days)}
            </div>

            <!-- RIGHT: My Vendor Breakdown -->
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
                <div style="padding:12px 16px;border-bottom:1px solid var(--border)">
                    <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">MY VENDOR BREAKDOWN</span>
                </div>
                <div id="vendorSummaryTable" style="overflow-x:auto">
                    <div style="color:var(--text-dim);padding:14px;font-size:11px">Loading vendor data…</div>
                </div>
            </div>
        </div>`}

        ${_renderDailyTable(days, isAdmin)}

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
            ${_renderUserDocsTable(_userDocsData, _userDocsLoadError)}
        </div>` : ''}

        ${isAdmin ? `
        <!-- Per-client breakdown (admin only) -->
        <div style="margin-top:24px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
            <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
                <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">CLIENT TOKEN BREAKDOWN</span>
                <span style="font-size:9px;color:var(--text-dim)">${_clientsData.length} client${_clientsData.length !== 1 ? 's' : ''}</span>
            </div>
            <div style="overflow-x:auto">
                ${_renderClientTable(_clientsData, _clientsLoadError)}
            </div>
        </div>` : ''}
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${isAdmin ? 'SYSTEM' : 'MY'} USAGE DASHBOARD</span>
        <button class="small-btn" onclick="renderDashboardPage(document.getElementById('appRoot'))"
                style="margin-left:12px">REFRESH</button>
    </div>

    ${!isAdmin ? _topupRequestModalHTML() : ''}
    `;
    // Load vendor breakdown asynchronously after paint
    if (isAdmin) {
        apiJSON('/admin/dashboard/vendors')
            .then(rows => {
                const el = document.getElementById('vendorSummaryTable');
                if (el) el.innerHTML = _renderAdminVendorSummary(rows);
            })
            .catch(e => {
                const el = document.getElementById('vendorSummaryTable');
                if (el) el.innerHTML = `<div style="color:var(--red,#e06c75);padding:14px;font-size:11px">${escapeHtml(e.message)}</div>`;
            });
    } else {
        apiJSON('/user/dashboard/vendors')
            .then(rows => {
                const el = document.getElementById('vendorSummaryTable');
                if (el) el.innerHTML = _renderClientVendorList(rows, null);
            })
            .catch(e => {
                const el = document.getElementById('vendorSummaryTable');
                if (el) el.innerHTML = `<div style="color:var(--red,#e06c75);padding:14px;font-size:11px">${escapeHtml(e.message)}</div>`;
            });
    }

    updateNavActive();
}

// ── Vendor breakdown helpers ──────────────────────────────────────────

function _renderAdminVendorSummary(rows) {
    if (!rows || rows.length === 0) {
        return `<div style="color:var(--text-dim);padding:14px;font-size:11px">No clients with vendors yet.</div>`;
    }
    return `
    <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead>
            <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.08em">
                <th style="text-align:left;padding:8px 12px;font-weight:500">CLIENT</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">VENDORS</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">EXTRACTIONS</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">COMPLETED</th>
                <th style="text-align:center;padding:8px 12px;font-weight:500">DETAILS</th>
            </tr>
        </thead>
        <tbody>
            ${rows.map(r => `
            <tr style="border-bottom:1px solid var(--border)"
                onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background=''">
                <td style="padding:8px 12px;font-weight:500">${escapeHtml(r.email)}</td>
                <td style="padding:8px 12px;text-align:right;color:var(--blue)">${fmtNum(r.vendor_count)}</td>
                <td style="padding:8px 12px;text-align:right;color:var(--text-mid)">${fmtNum(r.extraction_count)}</td>
                <td style="padding:8px 12px;text-align:right;color:var(--green)">${fmtNum(r.completed_extractions)}</td>
                <td style="padding:8px 12px;text-align:center">
                    <button class="small-btn"
                        onclick="navigate('#/vendor-breakdown/${encodeURIComponent(r.user_id)}')">
                        Vendors
                    </button>
                </td>
            </tr>`).join('')}
        </tbody>
    </table>`;
}

function _renderClientVendorList(vendors, selectedVendorId) {
    if (!vendors || vendors.length === 0) {
        return `<div style="color:var(--text-dim);padding:14px;font-size:11px">No vendors configured yet.</div>`;
    }
    return `
    <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead>
            <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.08em">
                <th style="text-align:left;padding:8px 12px;font-weight:500">VENDOR</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">EXTRACTIONS</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">PAGES</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">DONE</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">FAILED</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">LAST RUN</th>
                <th style="text-align:center;padding:8px 12px;font-weight:500">STATS</th>
            </tr>
        </thead>
        <tbody>
            ${vendors.map(v => `
            <tr style="border-bottom:1px solid var(--border);${v.vendor_id === selectedVendorId ? 'background:var(--bg2)' : ''}"
                onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background='${v.vendor_id === selectedVendorId ? 'var(--bg2)' : ''}'">
                <td style="padding:8px 12px;font-weight:500">${escapeHtml(v.vendor_name)}</td>
                <td style="padding:8px 12px;text-align:right;color:var(--text-mid)">${fmtNum(v.extraction_count)}</td>
                <td style="padding:8px 12px;text-align:right;color:var(--text-mid)">${fmtNum(v.total_pages_processed)}</td>
                <td style="padding:8px 12px;text-align:right;color:var(--green)">${fmtNum(v.completed)}</td>
                <td style="padding:8px 12px;text-align:right;color:${v.failed > 0 ? 'var(--red,#e06c75)' : 'var(--text-dim)'}">${fmtNum(v.failed)}</td>
                <td style="padding:8px 12px;color:var(--text-dim)">${fmtDate(v.last_extraction_at)}</td>
                <td style="padding:8px 12px;text-align:center">
                    <button class="small-btn"
                        onclick="navigate('#/vendor-stats/${encodeURIComponent(v.vendor_id)}')">
                        Stats
                    </button>
                </td>
            </tr>`).join('')}
        </tbody>
    </table>`;
}

function _renderVendorDailyChart(daily) {
    if (!daily || daily.length === 0) {
        return `<div style="color:var(--text-dim);text-align:center;padding:32px 0;font-size:10px">No extraction data yet.</div>`;
    }
    const sorted = [...daily].reverse();
    const W = 560, H = 150;
    const PAD = { top: 10, right: 16, bottom: 24, left: 40 };
    const cW = W - PAD.left - PAD.right;
    const cH = H - PAD.top - PAD.bottom;
    const n = sorted.length;
    const maxVal = Math.max(...sorted.map(d => Number(d.extractions || 0)), 1);
    const baseY = PAD.top + cH;
    const slotW = cW / Math.max(n, 1);
    const barW = Math.max(2, slotW * 0.55);

    function barX(i) { return PAD.left + i * slotW + (slotW - barW) / 2; }
    function barH(v) { return Math.max(1, (Number(v || 0) / maxVal) * cH); }

    const bars = sorted.map((d, i) => {
        const h = barH(d.extractions);
        return `<rect x="${barX(i)}" y="${baseY - h}" width="${barW}" height="${h}"
                      rx="1.5" fill="var(--blue)" opacity="0.85">
                    <title>${escapeHtml(d.day)}: ${d.extractions} extractions (${d.completed} done, ${d.failed} failed)</title>
                </rect>`;
    }).join('');

    const step = Math.max(1, Math.floor(n / 7));
    const xLabels = sorted.filter((_, i) => i % step === 0 || i === n - 1).map((d, idx, arr) => {
        const realIdx = sorted.indexOf(d);
        return `<text x="${barX(realIdx) + barW / 2}" y="${H - 4}" text-anchor="middle"
                      fill="var(--text-dim)" font-size="8" font-family="JetBrains Mono,monospace">
                    ${String(d.day || '').slice(5)}
                </text>`;
    }).join('');

    const yLabels = [0.5, 1.0].map(f => {
        const y = PAD.top + cH * (1 - f);
        return `<text x="${PAD.left - 4}" y="${y + 3}" text-anchor="end"
                      fill="var(--text-dim)" font-size="8" font-family="JetBrains Mono,monospace">
                    ${Math.round(maxVal * f)}
                </text>
                <line x1="${PAD.left}" y1="${y}" x2="${W - PAD.right}" y2="${y}"
                      stroke="var(--border)" stroke-width="1"/>`;
    }).join('');

    return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" style="display:block">
        ${yLabels}
        <line x1="${PAD.left}" y1="${baseY}" x2="${W - PAD.right}" y2="${baseY}"
              stroke="var(--border)" stroke-width="1"/>
        ${bars}
        ${xLabels}
    </svg>`;
}

function _renderVendorPageTable(pages) {
    if (!pages || pages.length === 0) {
        return `<div style="color:var(--text-dim);padding:14px;font-size:11px">No per-page data yet.</div>`;
    }
    return `
    <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead>
            <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.08em">
                <th style="text-align:right;padding:7px 14px;font-weight:500">PAGE</th>
                <th style="text-align:right;padding:7px 14px;font-weight:500">TIMES PROCESSED</th>
                <th style="text-align:right;padding:7px 14px;font-weight:500">AVG TOKENS</th>
                <th style="text-align:right;padding:7px 14px;font-weight:500">AVG LATENCY</th>
            </tr>
        </thead>
        <tbody>
            ${pages.map((p, idx) => `
            <tr style="border-bottom:1px solid var(--border);${idx % 2 === 1 ? 'background:var(--bg2)' : ''}">
                <td style="padding:6px 14px;text-align:right;color:var(--blue);font-weight:500">${p.page_number}</td>
                <td style="padding:6px 14px;text-align:right;color:var(--text-mid)">${fmtNum(p.times_processed)}</td>
                <td style="padding:6px 14px;text-align:right">${p.avg_tokens ? fmtTokens(p.avg_tokens) : '—'}</td>
                <td style="padding:6px 14px;text-align:right;color:var(--text-dim)">${p.avg_latency_ms ? fmtLatency(p.avg_latency_ms) : '—'}</td>
            </tr>`).join('')}
        </tbody>
    </table>`;
}

// ── Vendor breakdown page (admin: per-client vendor list) ─────────────

async function renderVendorBreakdownPage(app, userId) {
    const authUser = (() => { try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch { return null; } })();
    const isAdmin = authUser && authUser.role === 'admin';
    const endpoint = isAdmin
        ? `/admin/dashboard/clients/${encodeURIComponent(userId)}/vendors`
        : `/user/dashboard/vendors`;

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:18px">
            <button class="small-btn" onclick="navigate('#/dashboard')">Back</button>
            <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.12em">DASHBOARD / VENDOR BREAKDOWN</div>
        </div>
        <div class="page-title">Vendor Breakdown</div>
        <div style="color:var(--text-dim);font-size:11px;margin-top:12px">Loading…</div>
    </div>`;
    updateNavActive();

    try {
        const vendors = await apiJSON(endpoint);
        app.innerHTML = headerHTML() + `
        <div class="page-content">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:18px">
                <button class="small-btn" onclick="navigate('#/dashboard')">Back</button>
                <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.12em">DASHBOARD / VENDOR BREAKDOWN</div>
            </div>
            <div class="page-title">Vendor Breakdown</div>
            <div style="margin-top:16px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
                <div style="padding:12px 16px;border-bottom:1px solid var(--border)">
                    <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">${vendors.length} VENDOR${vendors.length !== 1 ? 'S' : ''}</span>
                </div>
                <div style="overflow-x:auto">${_renderClientVendorList(vendors, null)}</div>
            </div>
        </div>`;
        updateNavActive();
    } catch (e) {
        app.innerHTML = headerHTML() + `
        <div class="page-content">
            <button class="small-btn" onclick="navigate('#/dashboard')" style="margin-bottom:18px">Back</button>
            <div class="page-title">Error</div>
            <div style="color:var(--red,#e06c75);font-size:12px;margin-top:12px">${escapeHtml(e.message)}</div>
        </div>`;
        updateNavActive();
    }
}

// ── Vendor stats detail page ──────────────────────────────────────────

async function renderVendorStatsPage(app, vendorId) {
    const authUser = (() => { try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch { return null; } })();
    const isAdmin = authUser && authUser.role === 'admin';
    const endpoint = isAdmin
        ? `/admin/dashboard/vendors/${encodeURIComponent(vendorId)}/stats`
        : `/user/dashboard/vendors/${encodeURIComponent(vendorId)}/stats`;

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:18px">
            <button class="small-btn" onclick="history.back()">Back</button>
            <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.12em">DASHBOARD / VENDOR STATS</div>
        </div>
        <div class="page-title">Vendor Stats</div>
        <div style="color:var(--text-dim);font-size:11px;margin-top:12px">Loading…</div>
    </div>`;
    updateNavActive();

    try {
        const data = await apiJSON(endpoint);
        app.innerHTML = headerHTML() + `
        <div class="page-content">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:18px">
                <button class="small-btn" onclick="history.back()">Back</button>
                <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.12em">VENDOR / ${escapeHtml(vendorId)}</div>
            </div>
            <div class="page-title">Vendor: ${escapeHtml(vendorId)}</div>

            <!-- Daily extractions chart -->
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:18px 20px;margin-top:20px">
                <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:12px">
                    DAILY EXTRACTIONS — LAST 30 DAYS
                </div>
                ${_renderVendorDailyChart(data.daily || [])}
            </div>

            <!-- Daily table -->
            ${(data.daily || []).length > 0 ? `
            <div style="margin-top:16px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
                <div style="padding:10px 16px;border-bottom:1px solid var(--border)">
                    <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">DAILY BREAKDOWN</span>
                </div>
                <div style="overflow-x:auto">
                    <table style="width:100%;border-collapse:collapse;font-size:11px">
                        <thead>
                            <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.08em">
                                <th style="text-align:left;padding:7px 14px;font-weight:500">DATE</th>
                                <th style="text-align:right;padding:7px 14px;font-weight:500">EXTRACTIONS</th>
                                <th style="text-align:right;padding:7px 14px;font-weight:500">DONE</th>
                                <th style="text-align:right;padding:7px 14px;font-weight:500">FAILED</th>
                                <th style="text-align:right;padding:7px 14px;font-weight:500">PAGES</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${(data.daily || []).map((d, idx) => `
                            <tr style="border-bottom:1px solid var(--border);${idx % 2 === 1 ? 'background:var(--bg2)' : ''}">
                                <td style="padding:6px 14px;color:var(--text-mid)">${escapeHtml(d.day || '')}</td>
                                <td style="padding:6px 14px;text-align:right">${d.extractions}</td>
                                <td style="padding:6px 14px;text-align:right;color:var(--green)">${d.completed}</td>
                                <td style="padding:6px 14px;text-align:right;color:${d.failed > 0 ? 'var(--red,#e06c75)' : 'var(--text-dim)'}">${d.failed}</td>
                                <td style="padding:6px 14px;text-align:right;color:var(--text-dim)">${d.total_pages}</td>
                            </tr>`).join('')}
                        </tbody>
                    </table>
                </div>
            </div>` : ''}

            <!-- Per-page stats -->
            <div style="margin-top:16px;background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
                <div style="padding:10px 16px;border-bottom:1px solid var(--border)">
                    <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">PER-PAGE STATS</span>
                </div>
                <div style="overflow-x:auto">${_renderVendorPageTable(data.pages || [])}</div>
            </div>
        </div>`;
        updateNavActive();
    } catch (e) {
        app.innerHTML = headerHTML() + `
        <div class="page-content">
            <button class="small-btn" onclick="history.back()" style="margin-bottom:18px">Back</button>
            <div class="page-title">Error</div>
            <div style="color:var(--red,#e06c75);font-size:12px;margin-top:12px">${escapeHtml(e.message)}</div>
        </div>`;
        updateNavActive();
    }
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
        </div>

        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:18px 20px;margin-bottom:20px">
            <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:12px">CLIENT TOKEN TREND</div>
            ${_renderBarChart(data.days || [])}
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


// ── Top-up Request Modal (client only) ───────────────────────────────────

function _topupRequestModalHTML() {
    return `
    <div class="modal-overlay" id="topupRequestModal">
        <div class="modal" style="max-width:460px">
            <div class="modal-title">Request Top-up Pages</div>
            <div style="font-size:10px;color:var(--text-dim);margin-bottom:16px;line-height:1.6">
                Your request will be sent to the admin for review.
                You'll be able to continue uploading once approved.
            </div>

            <!-- Pages — free numeric input -->
            <div class="modal-field">
                <label class="modal-label">How many pages do you need?</label>
                <div style="position:relative">
                    <input class="modal-input" id="topupReqPages" type="number"
                           min="1" step="1" placeholder="e.g. 1500"
                           autocomplete="off"
                           style="font-family:var(--mono);font-size:14px;letter-spacing:0.04em;padding-right:60px"
                           oninput="_topupReqValidatePages(this)">
                    <span style="position:absolute;right:12px;top:50%;transform:translateY(-50%);
                                 font-size:10px;color:var(--text-dim);pointer-events:none;letter-spacing:0.06em">
                        PAGES
                    </span>
                </div>
                <div id="topupReqPagesHint"
                     style="font-size:9px;color:var(--text-dim);margin-top:4px;letter-spacing:0.04em">
                    Enter any whole number greater than 0
                </div>
            </div>

            <!-- Period — preset + Custom option -->
            <div class="modal-field" style="margin-top:12px">
                <label class="modal-label">For what period?</label>
                <select class="modal-input" id="topupReqPeriod"
                        style="cursor:pointer" onchange="_topupReqPeriodChanged()">
                    <option value="1 month">1 Month</option>
                    <option value="3 months">3 Months</option>
                    <option value="6 months" selected>6 Months</option>
                    <option value="1 year">1 Year</option>
                    <option value="custom">Custom…</option>
                </select>
            </div>

            <!-- Custom period text input (hidden until "Custom…" is selected) -->
            <div class="modal-field" id="topupReqCustomPeriodWrap"
                 style="margin-top:8px;display:none">
                <label class="modal-label">Describe the period</label>
                <input class="modal-input" id="topupReqCustomPeriod" type="text"
                       maxlength="64"
                       placeholder="e.g. Q3 extension, 2 years, 18 months"
                       autocomplete="off">
            </div>

            <!-- Note / reason -->
            <div class="modal-field" style="margin-top:12px">
                <label class="modal-label">Note / Reason (optional)</label>
                <input class="modal-input" id="topupReqNote" type="text" maxlength="500"
                       placeholder="e.g. End-of-quarter processing spike" autocomplete="off">
            </div>

            <div id="topupReqStatus"
                 style="min-height:16px;margin-top:10px;font-size:11px;color:var(--text-dim)"></div>

            <div class="modal-actions">
                <button class="modal-btn secondary" onclick="closeModal('topupRequestModal')">Cancel</button>
                <button class="modal-btn primary" id="topupReqSubmitBtn"
                        onclick="submitTopupRequest()">Send Request</button>
            </div>
        </div>
    </div>`;
}

function _topupReqValidatePages(input) {
    const hint = document.getElementById('topupReqPagesHint');
    const val = parseInt(input.value, 10);
    if (!input.value || isNaN(val) || val < 1) {
        if (hint) { hint.textContent = 'Enter any whole number greater than 0'; hint.style.color = 'var(--text-dim)'; }
        input.style.borderColor = '';
    } else {
        if (hint) { hint.textContent = `${val.toLocaleString()} pages requested`; hint.style.color = 'var(--green,#98c379)'; }
        input.style.borderColor = 'var(--green,#98c379)';
    }
}

function _topupReqPeriodChanged() {
    const v = document.getElementById('topupReqPeriod').value;
    const wrap = document.getElementById('topupReqCustomPeriodWrap');
    if (wrap) wrap.style.display = v === 'custom' ? 'block' : 'none';
    if (v === 'custom') {
        setTimeout(() => document.getElementById('topupReqCustomPeriod')?.focus(), 30);
    }
}

function openTopupRequestModal() {
    const modal = document.getElementById('topupRequestModal');
    if (!modal) return;
    const pagesEl = document.getElementById('topupReqPages');
    if (pagesEl) { pagesEl.value = ''; pagesEl.style.borderColor = ''; }
    const hintEl = document.getElementById('topupReqPagesHint');
    if (hintEl) { hintEl.textContent = 'Enter any whole number greater than 0'; hintEl.style.color = 'var(--text-dim)'; }
    document.getElementById('topupReqPeriod').value = '6 months';
    document.getElementById('topupReqCustomPeriodWrap').style.display = 'none';
    document.getElementById('topupReqCustomPeriod').value = '';
    document.getElementById('topupReqNote').value = '';
    document.getElementById('topupReqStatus').textContent = '';
    document.getElementById('topupReqStatus').style.color = 'var(--text-dim)';
    modal.classList.add('open');
    setTimeout(() => document.getElementById('topupReqPages').focus(), 50);
}

async function submitTopupRequest() {
    const btn = document.getElementById('topupReqSubmitBtn');
    const statusEl = document.getElementById('topupReqStatus');

    // Validate pages
    const rawPages = document.getElementById('topupReqPages').value.trim();
    const pages = parseInt(rawPages, 10);
    if (!rawPages || isNaN(pages) || pages < 1) {
        statusEl.style.color = 'var(--red,#e06c75)';
        statusEl.textContent = 'Please enter a valid number of pages (minimum 1).';
        document.getElementById('topupReqPages').focus();
        return;
    }

    // Resolve period (preset or custom)
    const periodSel = document.getElementById('topupReqPeriod').value;
    let period = periodSel;
    if (periodSel === 'custom') {
        period = (document.getElementById('topupReqCustomPeriod').value || '').trim();
        if (!period) {
            statusEl.style.color = 'var(--red,#e06c75)';
            statusEl.textContent = 'Please describe the custom period.';
            document.getElementById('topupReqCustomPeriod').focus();
            return;
        }
    }

    const note = (document.getElementById('topupReqNote').value || '').trim();

    btn.disabled = true;
    btn.textContent = 'Sending…';
    statusEl.style.color = 'var(--text-dim)';
    statusEl.textContent = '';

    try {
        await apiJSON('/me/topup-requests', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                requested_pages: pages,
                requested_period: period,
                note: note || null,
            }),
        });
        statusEl.style.color = 'var(--green,#98c379)';
        statusEl.textContent = `Request sent — ${pages.toLocaleString()} pages for "${period}". Admin will review shortly.`;
        btn.textContent = 'Sent ✓';
        setTimeout(() => closeModal('topupRequestModal'), 2500);
    } catch (e) {
        statusEl.style.color = 'var(--red,#e06c75)';
        statusEl.textContent = 'Error: ' + e.message;
        btn.disabled = false;
        btn.textContent = 'Send Request';
    }
}
