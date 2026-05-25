/* ── Augmented OCR — History page ──────────────────────────────────── */

// ══════════════════════════════════════════════════════════════════════
// PAGE 5: HISTORY
// ══════════════════════════════════════════════════════════════════════
function _historyItemHTML(e) {
    return `
    <div class="history-item" onclick="showHistoryDetailSafe(${e.id})">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
            <div style="flex:1;min-width:0">
                <div class="history-filename">${escapeHtml(e.filename || 'Unknown')}</div>
                <div class="history-meta">
                    <span style="color:var(--blue);font-weight:500">${escapeHtml(e.vendor_name || e.vendor_id)}</span>
                    <span class="history-status status-${safeClassToken(e.status)}">${escapeHtml(e.status)}</span>
                    <span>${e.total_pages || 0} pages</span>
                    <span>${new Date(e.created_at).toLocaleString()}</span>
                    <a class="link-btn" href="#/review/${e.id}" onclick="event.stopPropagation()" style="font-size:9px">Review</a>
                </div>
            </div>
            <div class="history-actions">
                <div class="history-latency ${e.duration_ms ? '' : 'no-data'}" title="End-to-end extraction latency">
                    <span class="history-latency-icon">⏱</span>
                    <span class="history-latency-value">${e.duration_ms ? (e.duration_ms / 1000).toFixed(1) + 's' : '—'}</span>
                </div>
                <button class="history-delete-btn" title="Delete this extraction"
                    onclick="event.stopPropagation(); deleteExtractionSafe(${e.id}, '${escapeInlineJsString(e.filename || 'this file')}')">Delete</button>
            </div>
        </div>
    </div>`;
}

async function renderHistoryPage(app) {
    let extractions = [], totalCount = 0;
    try {
        extractions = await apiJSON('/extractions?limit=50');
        const countRes = await apiJSON('/extractions/count');
        totalCount = countRes.count || extractions.length;
    } catch (e) { console.warn(e); }

    const isAdmin = (getAuthUser() || {}).role === 'admin';

    // Admin: build vendor_id → client email map
    let vendorClientMap = {};   // vendor_id → email
    let clientOrder = [];       // ordered list of client emails (clients first)
    if (isAdmin) {
        try {
            const [vendors, users] = await Promise.all([apiJSON('/vendors'), apiJSON('/admin/users')]);
            const userEmailMap = {};
            users.forEach(u => { userEmailMap[u.id] = u.email; });
            vendors.forEach(v => {
                vendorClientMap[v.id] = v.user_id ? (userEmailMap[v.user_id] || 'Unassigned') : 'Unassigned';
            });
            clientOrder = users.filter(u => u.role === 'client' && u.is_active).map(u => u.email);
        } catch (e) { console.warn(e); }
    }

    let historyHTML;
    if (!isAdmin || !extractions.length) {
        historyHTML = extractions.length === 0
            ? '<div style="color:var(--text-dim);padding:20px">No extractions yet.</div>'
            : extractions.map(_historyItemHTML).join('');
    } else {
        // Group by client
        const groups = {};
        extractions.forEach(e => {
            const key = vendorClientMap[e.vendor_id] || 'Unassigned';
            if (!groups[key]) groups[key] = [];
            groups[key].push(e);
        });

        // Order: known clients first, then others, then unassigned
        const keys = [...clientOrder.filter(k => groups[k])];
        Object.keys(groups).forEach(k => { if (k !== 'Unassigned' && !keys.includes(k)) keys.push(k); });
        if (groups['Unassigned']) keys.push('Unassigned');

        // Bottom bar per-client summary
        const clientSummary = keys.map(k =>
            `<span style="margin-right:14px;color:var(--text-dim)">${escapeHtml(k.split('@')[0])}: <strong style="color:var(--text)">${groups[k].length}</strong></span>`
        ).join('') + '<span style="color:var(--border);margin-right:14px">|</span>';

        historyHTML = `<div id="_histClientSummary" data-summary="${escapeHtml(clientSummary)}"></div>` +
            keys.map(key => {
                const isUnassigned = key === 'Unassigned';
                return `
                <div style="margin-bottom:8px">
                    <div style="padding:7px 12px;background:var(--bg2);border:1px solid var(--border);border-radius:3px;display:flex;align-items:center;justify-content:space-between;margin-bottom:2px">
                        <span style="font-size:10px;font-weight:600;letter-spacing:0.1em;color:${isUnassigned ? 'var(--text-dim)' : 'var(--blue)'}">${escapeHtml(key.toUpperCase())}</span>
                        <span style="font-size:9px;color:var(--text-dim)">${groups[key].length} EXTRACTION${groups[key].length !== 1 ? 'S' : ''}</span>
                    </div>
                    ${groups[key].map(_historyItemHTML).join('')}
                </div>`;
            }).join('');
    }

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">📜 Extraction History</div>
        <div id="historyList">${historyHTML}</div>
    </div>
    <div class="bottom-bar" id="historyBottomBar">
        <span style="font-size:10px;letter-spacing:0.1em" id="historyBottomSpan"><span style="color:var(--text-dim)">${totalCount} EXTRACTION${totalCount !== 1 ? 'S' : ''}</span></span>
    </div>
    <div class="detail-overlay" id="detailOverlay" onclick="if(event.target===this)this.classList.remove('open')">
        <div class="detail-box" id="detailBox"></div>
    </div>`;

    // Inject per-client summary into bottom bar for admin
    if (isAdmin) {
        const summaryEl = document.getElementById('_histClientSummary');
        const bottomSpan = document.getElementById('historyBottomSpan');
        if (summaryEl && bottomSpan) {
            bottomSpan.innerHTML = summaryEl.dataset.summary +
                `<span style="color:var(--text-dim)">${totalCount} TOTAL EXTRACTION${totalCount !== 1 ? 'S' : ''}</span>`;
            summaryEl.remove();
        }
    }

    updateNavActive();
}


async function deleteExtractionSafe(id, filename) {
    const msg = `Delete "${filename}" from History?\n\nThis removes only this extraction and its files.`;
    if (!window.confirm(msg)) return;
    try {
        await apiJSON(`/extractions/${id}`, { method: 'DELETE' });
        showToast('Extraction deleted');
        const app = document.getElementById('appRoot');
        if (app) await renderHistoryPage(app);
    } catch (e) {
        showToast(`Delete failed: ${e.message}`);
    }
}

async function showHistoryDetailSafe(id) {
    try {
        const data = await apiJSON(`/extractions/${id}`);
        const box = document.getElementById('detailBox');
        if (!box) return;

        box.innerHTML = `
            <div class="modal-title" id="historyDetailTitle"></div>
            <div style="margin-bottom:8px">
                <span class="rp-badge" id="historyDetailStatus"></span>
                <span style="font-size:10px;color:var(--text-dim);margin-left:8px" id="historyDetailMeta"></span>
            </div>
            <div id="historyDetailError" style="color:var(--red);font-size:11px;margin-bottom:8px;display:none"></div>
            <div class="rp-title" style="margin-top:12px">Final Result</div>
            <div class="result-block" style="max-height:300px" id="historyDetailResult"></div>
            <div id="historyDetailPagesWrap" style="display:none">
                <div class="rp-title" style="margin-top:12px">Page Results</div>
                <div class="result-block" style="max-height:200px" id="historyDetailPages"></div>
            </div>
            <div style="display:flex;gap:6px;margin-top:10px">
                <button class="small-btn" id="historyCopyBtnSafe">Copy JSON</button>
                <button class="small-btn" id="historyCloseBtnSafe">Close</button>
            </div>`;

        const statusEl = document.getElementById('historyDetailStatus');
        if (statusEl) {
            statusEl.className = `rp-badge ${data.status === 'done' ? 'optimal' : data.status === 'failed' ? 'review' : 'processing'}`;
            statusEl.textContent = data.status || 'unknown';
        }

        setText('historyDetailTitle', `${data.filename || 'Extraction'} - ${data.vendor_name || data.vendor_id}`);
        setText(
            'historyDetailMeta',
            `${data.total_pages || 0} pages · ${formatDurationMs(data.duration_ms)}${data.duration_ms ? ' · ' : ''}${new Date(data.created_at).toLocaleString()}`
        );
        setText('historyDetailResult', JSON.stringify(data.corrected_result || data.result, null, 2) || 'null');

        const errorEl = document.getElementById('historyDetailError');
        if (errorEl && data.error) {
            errorEl.style.display = 'block';
            errorEl.textContent = `Error: ${data.error}`;
        }

        const pagesWrap = document.getElementById('historyDetailPagesWrap');
        if (pagesWrap && data.page_results) {
            pagesWrap.style.display = 'block';
            setText('historyDetailPages', JSON.stringify(data.page_results, null, 2));
        }

        const copyBtn = document.getElementById('historyCopyBtnSafe');
        if (copyBtn) {
            copyBtn.onclick = () => {
                navigator.clipboard.writeText(JSON.stringify(data.corrected_result || data.result, null, 2));
                showToast('Copied');
            };
        }

        const closeBtn = document.getElementById('historyCloseBtnSafe');
        if (closeBtn) {
            closeBtn.onclick = () => document.getElementById('detailOverlay').classList.remove('open');
        }

        document.getElementById('detailOverlay').classList.add('open');
    } catch (e) { showToast('Failed to load: ' + e.message); }
}
