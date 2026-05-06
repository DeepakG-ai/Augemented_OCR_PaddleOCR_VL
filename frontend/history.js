/* ── Augmented OCR — History page ──────────────────────────────────── */

// ══════════════════════════════════════════════════════════════════════
// PAGE 5: HISTORY
// ══════════════════════════════════════════════════════════════════════
async function renderHistoryPage(app) {
    let extractions = [];
    let totalCount = 0;
    try {
        extractions = await apiJSON('/extractions?limit=50');
        const countRes = await apiJSON('/extractions/count');
        totalCount = countRes.count || extractions.length;
    } catch (e) { console.warn(e); }

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">📜 Extraction History</div>
        <div id="historyList">${extractions.length === 0 ? '<div style="color:var(--text-dim);padding:20px">No extractions yet.</div>' : extractions.map(e => `
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
                    <button
                        class="history-delete-btn"
                        title="Delete this extraction"
                        onclick="event.stopPropagation(); deleteExtractionSafe(${e.id}, '${escapeInlineJsString(e.filename || 'this file')}')"
                    >Delete</button>
                    </div>
                </div>
            </div>`).join('')}
        </div>
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${totalCount} EXTRACTION${totalCount !== 1 ? 'S' : ''}</span>
    </div>
    <div class="detail-overlay" id="detailOverlay" onclick="if(event.target===this)this.classList.remove('open')">
        <div class="detail-box" id="detailBox"></div>
    </div>`;
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
