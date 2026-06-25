/* ── Augmented OCR — History page ──────────────────────────────────── */

// ══════════════════════════════════════════════════════════════════════
// PAGE 5: HISTORY
// ══════════════════════════════════════════════════════════════════════
const HISTORY_PAGE_SIZE = 10;
window._historyCurrentPage = 1;

function _historyReviewUnavailable(e) {
    const progress = e.progress || {};
    return e.status !== 'done'
        || e.error
        || progress.review_available === false
        || progress.layout_boxes_available === false
        || progress.warning_code
        || progress.ocr_error
        || (e.result && typeof e.result === 'object' && e.result._all_pages_failed === true)
        || (Array.isArray(e.page_results) && e.page_results.some(pr => pr && pr._error));
}

function _historyHasPipelineFailure(e) {
    return e.error
        || (e.result && typeof e.result === 'object' && e.result._all_pages_failed === true)
        || (Array.isArray(e.page_results) && e.page_results.some(pr => pr && pr._error));
}

function _historyEffectiveStatus(e) {
    return _historyHasPipelineFailure(e) ? 'failed' : e.status;
}

function _historyItemHTML(e, showDelete = false) {
    const reviewUnavailable = _historyReviewUnavailable(e);
    const effectiveStatus = _historyEffectiveStatus(e);
    const layoutSkipped = (e.progress || {}).layout_boxes_available === false;
    return `
    <div class="history-item" onclick="showHistoryDetailSafe(${e.id})">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
            <div style="flex:1;min-width:0">
                <div class="history-filename">${escapeHtml(e.filename || 'Unknown')}</div>
                <div class="history-meta">
                    <span style="color:var(--blue);font-weight:500">${escapeHtml(e.vendor_name || e.vendor_id)}</span>
                    <span class="history-status status-${safeClassToken(effectiveStatus)}">${escapeHtml(effectiveStatus)}</span>
                    <span>${e.total_pages || 0} pages</span>
                    <span>${new Date(e.created_at).toLocaleString()}</span>
                    ${reviewUnavailable
                        ? `<span class="link-btn" style="font-size:9px;color:var(--text-dim);cursor:not-allowed" title="Review unavailable for this extraction">Review N/A</span>`
                        : `<a class="link-btn" href="#/review/${e.id}" onclick="event.stopPropagation()" style="font-size:9px">Review</a>`}
                    ${layoutSkipped
                        ? `<span style="font-size:8px;background:var(--bg2);border:1px solid var(--border);border-radius:3px;padding:1px 5px;margin-left:4px;color:var(--text-dim);cursor:help" title="Layout boxes were skipped because this extraction was submitted through the API in JSON-only mode.">JSON-only API</span>`
                        : ''}
                </div>
            </div>
            <div class="history-actions">
                <div class="history-latency ${e.duration_ms ? '' : 'no-data'}" title="End-to-end extraction latency">
                    <span class="history-latency-icon">⏱</span>
                    <span class="history-latency-value">${e.duration_ms ? (e.duration_ms / 1000).toFixed(1) + 's' : '—'}</span>
                </div>
                ${showDelete ? `<button class="history-delete-btn" title="Delete this extraction"
                    onclick="event.stopPropagation(); deleteExtractionSafe(${e.id}, '${escapeInlineJsString(e.filename || 'this file')}')">Delete</button>` : ''}
            </div>
        </div>
    </div>`;
}

async function renderHistoryPage(app, page) {
    if (page !== undefined) window._historyCurrentPage = page;
    const currentPage = window._historyCurrentPage || 1;
    const offset = (currentPage - 1) * HISTORY_PAGE_SIZE;

    let extractions = [], totalCount = 0, _loadError = null;
    try {
        extractions = await apiJSON(`/extractions?limit=${HISTORY_PAGE_SIZE}&offset=${offset}`);
    } catch (e) {
        console.warn(e);
        _loadError = e.message || 'Failed to load extractions';
    }

    if (!_loadError) {
        try {
            const countRes = await apiJSON('/extractions/count');
            totalCount = countRes.count || 0;
        } catch (e) {
            console.warn('Failed to fetch count:', e);
            totalCount = extractions.length + offset;
        }
    }

    const totalPages = Math.max(1, Math.ceil(totalCount / HISTORY_PAGE_SIZE));
    const isAdmin = (getAuthUser() || {}).role === 'admin';

    // Admin: build vendor_id → client email map
    let vendorClientMap = {};
    let clientOrder = [];
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
        if (_loadError) {
            historyHTML = `<div style="background:rgba(224,108,117,0.12);border:1px solid var(--red,#e06c75);border-radius:4px;padding:12px 16px;font-size:11px;color:var(--red,#e06c75)">&#9888; Could not load extraction history: ${escapeHtml(_loadError)}</div>`;
        } else {
            historyHTML = extractions.length === 0
                ? '<div style="color:var(--text-dim);padding:20px">No extractions yet.</div>'
                : extractions.map(e => _historyItemHTML(e, isAdmin)).join('');
        }
    } else {
        // Group by client
        const groups = {};
        extractions.forEach(e => {
            const key = vendorClientMap[e.vendor_id] || 'Unassigned';
            if (!groups[key]) groups[key] = [];
            groups[key].push(e);
        });

        const keys = [...clientOrder.filter(k => groups[k])];
        Object.keys(groups).forEach(k => { if (k !== 'Unassigned' && !keys.includes(k)) keys.push(k); });
        if (groups['Unassigned']) keys.push('Unassigned');

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
                    ${groups[key].map(e => _historyItemHTML(e, isAdmin)).join('')}
                </div>`;
            }).join('');
    }

    const startNum = totalCount === 0 ? 0 : offset + 1;
    const endNum = Math.min(offset + extractions.length, totalCount);

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">📜 Extraction History</div>
        <div id="historyList">${historyHTML}</div>
    </div>
    <div class="bottom-bar" id="historyBottomBar">
        <span style="font-size:10px;letter-spacing:0.1em" id="historyBottomSpan">
            <span style="color:var(--text-dim)">${totalCount} EXTRACTION${totalCount !== 1 ? 'S' : ''}</span>
        </span>
        <span style="display:flex;align-items:center;gap:6px">
            <button id="histPrevBtn" class="small-btn" style="padding:2px 10px;font-size:10px" ${currentPage <= 1 ? 'disabled' : ''}>&#8249; Prev</button>
            <span style="font-size:10px;color:var(--text-dim);min-width:80px;text-align:center">${startNum}–${endNum} of ${totalCount}</span>
            <button id="histNextBtn" class="small-btn" style="padding:2px 10px;font-size:10px" ${currentPage >= totalPages ? 'disabled' : ''}>Next &#8250;</button>
        </span>
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

    document.getElementById('histPrevBtn').addEventListener('click', () => {
        renderHistoryPage(app, currentPage - 1);
    });
    document.getElementById('histNextBtn').addEventListener('click', () => {
        renderHistoryPage(app, currentPage + 1);
    });

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
            const effectiveStatus = _historyEffectiveStatus(data);
            statusEl.className = `rp-badge ${effectiveStatus === 'done' ? 'optimal' : effectiveStatus === 'failed' ? 'review' : 'processing'}`;
            statusEl.textContent = effectiveStatus || 'unknown';
        }

        setText('historyDetailTitle', `${data.filename || 'Extraction'} - ${data.vendor_name || data.vendor_id}`);
        setText(
            'historyDetailMeta',
            `${data.total_pages || 0} pages · ${formatDurationMs(data.duration_ms)}${data.duration_ms ? ' · ' : ''}${new Date(data.created_at).toLocaleString()}`
        );
        const histResult = data.corrected_result || data.result;
        const histResultEl = document.getElementById('historyDetailResult');
        const histErr = parseResultErrors(histResult);
        if (histErr) {
            renderResultErrorBlock(histResultEl, histErr);
        } else {
            setText('historyDetailResult', JSON.stringify(histResult, null, 2) || 'null');
        }

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
