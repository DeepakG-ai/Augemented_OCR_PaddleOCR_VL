/* ── Augmented OCR — Review page (HITL field mapping + correction) ─── */

// ══════════════════════════════════════════════════════════════════════
// PAGE 6: REVIEW (Human-in-the-Loop Field Mapping + Correction)
// ══════════════════════════════════════════════════════════════════════

// ── po_per_page helpers ──────────────────────────────────────────────

function _cloneJson(value) {
    return JSON.parse(JSON.stringify(value));
}

function _reviewValueLines(value) {
    if (value == null) return [];
    if (Array.isArray(value)) return value.flatMap(_reviewValueLines);
    if (typeof value === 'object') {
        return Object.keys(value).flatMap(key => _reviewValueLines(value[key]));
    }
    return String(value)
        .split(/\r?\n/)
        .map(line => line.trim())
        .filter(Boolean);
}

function _combineReviewHeaderValue(value) {
    if (!value || (typeof value !== 'object' && !Array.isArray(value))) return value;
    const lines = _reviewValueLines(value);
    const seen = new Set();
    const deduped = [];
    for (const line of lines) {
        const key = line.toLocaleLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        deduped.push(line);
    }
    return deduped.length ? deduped.join('\n') : '';
}

function _normalizeReviewRecord(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    const normalized = { ...value };
    for (const key of Object.keys(normalized)) {
        if (key !== 'line_items') normalized[key] = _combineReviewHeaderValue(normalized[key]);
    }
    return normalized;
}

function _rvCurrentPayload() {
    if (!_rvIsPoPerPage) return _rvResult;
    const payload = _cloneJson(_rvAllResults);
    const idx = _rvRecordIndexForPage(_rvCurrentPage);
    payload[idx] = _cloneJson(_rvResult);
    return payload;
}

function _rvRecordIndexForPage(pageNumber) {
    if (!_rvAllResults.length) return 0;
    return Math.max(0, Math.min(_rvAllResults.length - 1, pageNumber - 1));
}

function _rvPersistCurrentRecord() {
    if (!_rvIsPoPerPage) return;
    const idx = _rvRecordIndexForPage(_rvCurrentPage);
    _rvAllResults[idx] = _cloneJson(_rvResult);
    _rvAllFieldLocs[idx] = _cloneJson(_rvFieldLocs);
}

function _rvLoadCurrentRecord() {
    if (_rvIsPoPerPage) {
        const idx = _rvRecordIndexForPage(_rvCurrentPage);
        _rvResult = _cloneJson(_normalizeReviewRecord(_rvAllResults[idx]));
        _rvOriginalResult = _cloneJson(_normalizeReviewRecord(_rvAllOriginalResults[idx]));
        _rvFieldLocs = _cloneJson(_rvAllFieldLocs[idx] || {});
        return;
    }
    _rvResult = _cloneJson(_normalizeReviewRecord(_rvSingleResult));
    _rvOriginalResult = _cloneJson(_normalizeReviewRecord(_rvSingleOriginalResult));
}

function _rvHeaderKeys() {
    return Object.keys(_rvResult).filter(k => k !== 'line_items' && _rvResult[k] != null);
}

function _rvUpdateStats() {
    const headerKeys = _rvHeaderKeys();
    const matchedCount = headerKeys.filter(k => _rvFieldLocs[k]).length;
    const matchedEl = document.querySelector('.stat-matched');
    const missedEl = document.querySelector('.stat-missed');
    if (matchedEl) matchedEl.textContent = `${matchedCount} MATCHED`;
    if (missedEl) missedEl.textContent = `${headerKeys.length - matchedCount} UNMATCHED`;
}

function _rvSetCurrentPage(nextPage) {
    const clampedPage = Math.max(1, Math.min(_rvTotalPages, nextPage));
    if (_rvCurrentPage === clampedPage) return;
    if (_rvPendingSelection) {
        _rvPendingSelection = null;
        const previewBar = document.getElementById('rvSelPreview');
        if (previewBar) previewBar.remove();
        const selBox = document.querySelector('.rv-selection-box');
        if (selBox) selBox.style.display = 'none';
    }
    _rvPersistCurrentRecord();
    _rvCurrentPage = clampedPage;
    _rvLoadCurrentRecord();
    rvRenderFields();
    _rvUpdateStats();
    rvRenderCurrentPage();
    rvUpdateJSON();
}

// ── Core review state ────────────────────────────────────────────────

let _rvCurrentPage = 1;
let _rvTotalPages = 1;
let _rvPages = [];
let _rvResult = {};
let _rvSingleResult = {};
let _rvSingleOriginalResult = {};
let _rvAllResults = [];
let _rvAllOriginalResults = [];
let _rvIsPoPerPage = false;
let _rvAllFieldLocs = [];
let _rvFieldLocs = {};
let _rvOcrData = [];          // PaddleOCR words per page (for click-to-select)
let _rvZoom = 100;
let _rvIsDragging = false;
let _rvDragStartX = 0, _rvDragStartY = 0, _rvScrollStartX = 0, _rvScrollStartY = 0;
let _rvResizeHandler = null;
let _rvExtractionId = null;
let _rvVendorId = null;
let _rvExistingCorrectionFields = {};
let _rvExistingSpatialFields = {};

// Selection mode state
let _rvSelectionField = null;  // composite key of field being corrected (e.g. "vendor" or "line_item_0_qty")
let _rvSelecting = false;      // mouse is currently drawing a rectangle
let _rvSelStartX = 0;
let _rvSelStartY = 0;
let _rvDirty = false;          // true if user has made any corrections
let _rvOriginalResult = {};    // immutable original Qwen3-VL result (for reset/undo)
let _rvUndoStack = [];         // stack of {fieldKey, oldValue, oldFieldLoc} for Ctrl+Z
let _rvPendingSelection = null; // {fieldKey, combinedText, box, matchedWords, avgScore} awaiting accept/reject

async function renderReviewPage(app, extractionId) {
    _rvExtractionId = extractionId;
    _rvDirty = false;
    _rvSelectionField = null;
    _rvSelecting = false;

    // Clean up any prior SVG overlay
    const oldSvg = document.getElementById('rvMappingSvg');
    if (oldSvg) oldSvg.remove();

    // Always fetch fresh data from API to avoid stale in-memory snapshots.
    try {
        const data = await apiJSON(`/extractions/${extractionId}`);
        let effectiveResult = data.corrected_result || data.result || {};
        let origResult = data.result || {};
        _rvVendorId = data.vendor_id || null;

        // Detect po_per_page: only run heuristic when format_type is unknown.
        // If the extraction or template already declares single_po_multipage, trust it.
        const dbFormat = data.format_type || null;
        const skipHeuristic = dbFormat === 'single_po_multipage' || dbFormat === 'single_page';

        if (!skipHeuristic && !Array.isArray(effectiveResult) && Array.isArray(data.page_results) && data.page_results.length > 1) {
            const validPR = data.page_results.filter(pr => !pr._error && pr.fields);
            if (validPR.length > 1) {
                const poNums = validPR.map(pr => String(pr.fields.po_number ?? '')).filter(Boolean);
                if (new Set(poNums).size > 1) {
                    effectiveResult = validPR.map(pr => pr.fields);
                    origResult = validPR.map(pr => pr.fields);
                }
            }
        }

        _rvIsPoPerPage = Array.isArray(effectiveResult);
        if (_rvIsPoPerPage) {
            _rvAllResults = _cloneJson(effectiveResult);
            _rvAllOriginalResults = _cloneJson(Array.isArray(origResult) ? origResult : []);
            _rvSingleResult = {};
            _rvSingleOriginalResult = {};
            _rvAllFieldLocs = Array.isArray(data.field_locations) ? _cloneJson(data.field_locations) : [];
            if (_rvAllFieldLocs.length === 0) {
                _rvAllFieldLocs = _rvAllResults.map(() => ({}));
            }
        } else {
            _rvSingleResult = _cloneJson(_normalizeReviewRecord(effectiveResult));
            _rvSingleOriginalResult = _cloneJson(_normalizeReviewRecord(origResult));
            _rvAllResults = [];
            _rvAllOriginalResults = [];
            _rvAllFieldLocs = [];
            _rvFieldLocs = data.field_locations || {};
        }
        try { _rvPages = await apiJSON(`/extractions/${extractionId}/pages`); } catch (e2) { _rvPages = []; }
    } catch (e) {
        // Auth/ownership failure — do not fall back to cached state.
        // 401: token expired (apiFetch already reloads); 403: wrong tenant.
        if (e.message && (e.message.includes('401') || e.message.includes('403'))) throw e;
        // Other API failure — fall back to in-memory state if available
        console.warn('Failed to fetch extraction from API, using in-memory fallback:', e.message);
        _rvVendorId = null;
        const fallbackResult = reviewResult || {};
        _rvIsPoPerPage = Array.isArray(fallbackResult);
        if (_rvIsPoPerPage) {
            _rvAllResults = _cloneJson(fallbackResult);
            _rvAllOriginalResults = _cloneJson(fallbackResult);
            _rvSingleResult = {};
            _rvSingleOriginalResult = {};
            _rvAllFieldLocs = Array.isArray(reviewFieldLocations) ? _cloneJson(reviewFieldLocations) : [];
            if (_rvAllFieldLocs.length === 0) {
                _rvAllFieldLocs = _rvAllResults.map(() => ({}));
            }
        } else {
            _rvSingleResult = _cloneJson(_normalizeReviewRecord(fallbackResult));
            _rvSingleOriginalResult = _cloneJson(_normalizeReviewRecord(fallbackResult));
            _rvAllResults = [];
            _rvAllOriginalResults = [];
            _rvAllFieldLocs = [];
            _rvFieldLocs = JSON.parse(JSON.stringify(reviewFieldLocations || {}));
        }
        _rvPages = extractionPages.length ? extractionPages : [];
    }
    _rvUndoStack = [];
    _rvPendingSelection = null;
    _rvExistingCorrectionFields = {};
    _rvExistingSpatialFields = {};

    // Load OCR data for click-to-select
    _rvOcrData = [];
    try {
        const ocrResp = await apiJSON(`/extractions/${extractionId}/ocr`);
        _rvOcrData = ocrResp.ocr_pages || [];
    } catch (e) {
        if (e.message && (e.message.includes('401') || e.message.includes('403'))) throw e;
        console.warn('OCR data not available for click-to-select:', e.message);
    }

    if (_rvVendorId) {
        try {
            const corrections = await apiJSON(`/vendors/${encodeURIComponent(_rvVendorId)}/gold-corrections`);
            _rvExistingCorrectionFields = corrections.fields || {};
        } catch (e) {
            if (e.message && (e.message.includes('401') || e.message.includes('403'))) throw e;
            console.warn('Saved correction metadata not available:', e.message);
            _rvExistingCorrectionFields = {};
        }
    }

    // Load spatial memory fields for override warning
    try {
        const smResp = await apiJSON(`/extractions/${extractionId}/spatial-memory-fields`);
        _rvExistingSpatialFields = smResp.fields || {};
    } catch (e) {
        if (e.message && (e.message.includes('401') || e.message.includes('403'))) throw e;
        console.warn('Spatial memory fields not available:', e.message);
        _rvExistingSpatialFields = {};
    }

    _rvTotalPages = _rvPages.length || 1;
    _rvCurrentPage = 1;
    _rvZoom = 100;
    _rvLoadCurrentRecord();

    // Count matched vs total header fields
    const headerKeys = _rvHeaderKeys();
    const matchedCount = headerKeys.filter(k => _rvFieldLocs[k]).length;

    app.innerHTML = headerHTML() + `
    <aside class="sidebar review-field-panel">
        <div class="section-title">Extracted Fields</div>
        <div id="rvFieldsList"></div>
        <div class="section-title" style="margin-top:0">Line Items</div>
        <div id="rvLineItemsWrap" style="overflow-x:auto;padding:0 4px"></div>
    </aside>
    <main class="viewer">
        <div id="rvViewer" style="display:flex;flex-direction:column;width:100%;height:100%">
            <div class="viewer-toolbar">
                <div class="page-nav">
                    <button class="nav-btn" id="rvPrevBtn" onclick="rvChangePage(-1)">PREV</button>
                    <span class="page-indicator" id="rvPageInd">PAGE 1 / ${_rvTotalPages}</span>
                    <button class="nav-btn" id="rvNextBtn" onclick="rvChangePage(1)">NEXT</button>
                </div>
                <span class="zoom-info" id="rvZoomInfo">ZOOM: 100% | HOLD ALT TO SEE OCR</span>
            </div>
            <div class="viewer-canvas" id="rvCanvas">
                <div id="rvDocFrame" class="doc-frame">
                    <div id="rvMappingContainer" class="mapping-container"></div>
                </div>
            </div>
        </div>
    </main>
    <aside class="right-panel review-json-panel">
        <div class="section-title">JSON Output</div>
        <pre class="review-json-pre" id="rvJsonPre">${JSON.stringify(_rvResult, null, 2)}</pre>
    </aside>
    <div class="bottom-bar">
        <div class="review-stats">
            <span class="stat-matched">${matchedCount} MATCHED</span>
            <span class="stat-missed">${headerKeys.length - matchedCount} UNMATCHED</span>
            <span>${_rvTotalPages} PAGE${_rvTotalPages !== 1 ? 'S' : ''}</span>
            <span id="rvOcrStatus" style="color:var(--blue)">${_rvOcrData.length ? '✓ OCR LOADED' : '○ NO OCR'}</span>
        </div>
        <div style="flex:1"></div>
        <button class="review-btn secondary" onclick="rvUndo()" title="Undo last correction (Ctrl+Z)">↶ Undo</button>
        <button class="review-btn secondary" onclick="rvDownloadJSON()">Download JSON</button>
        <button class="review-btn secondary" onclick="rvCopyJSON()">Copy JSON</button>
        <button class="review-btn secondary" onclick="navigate('#/extract')">Back to Extract</button>
        <button class="review-btn confirm" id="rvConfirmBtn" onclick="rvConfirm()">Confirm</button>
    </div>`;

    rvRenderFields();
    rvRenderCurrentPage();
    rvSetupDragZoom();
    rvSetupSelectionMode();
    rvSetupAltOverlay();
    updateNavActive();
}

// ── Review: Render editable field list in sidebar ─────────────────────

function rvGetDotClass(fieldKey) {
    const loc = _rvFieldLocs[fieldKey];
    if (!loc) return 'missing';
    if (loc.strategy === 'manual') return 'manual';
    if (rvIsLowConfidenceLoc(loc)) return 'found-low';
    const conf = loc.confidence || 'high';
    if (conf === 'high') return 'found-high';
    if (conf === 'medium' || conf === 'low') return 'found-low';
    return 'found-high';
}

function rvIsLowConfidenceLoc(loc) {
    if (!loc || loc.strategy === 'manual') return false;
    if (loc.strategy === 'qwen_anchor' || loc.strategy === 'qwen_column_header') return false;
    if (loc.strategy === 'qwen_anchor_missing' || loc.strategy === 'qwen_column_header_missing') return true;
    return ['row_fallback_vertical', 'value_fallback_global', 'cell_fuzzy', 'column_inferred', 'column_fuzzy'].includes(loc.match_mode)
        || loc.confidence === 'medium'
        || loc.confidence === 'low';
}

function rvGetLineItemCellClass(loc) {
    if (!loc) return '';
    if (loc.strategy === 'manual') return 'cell-manual';
    return rvIsLowConfidenceLoc(loc) ? 'cell-matched-low' : 'cell-matched';
}

function _isFieldChanged(key) {
    // Check if current value differs from the original Qwen3-VL result
    const origVal = key.startsWith('line_item_')
        ? (() => { const p = key.replace('line_item_', '').split('_'); const r = parseInt(p[0], 10); const c = p.slice(1).join('_'); return _rvOriginalResult.line_items?.[r]?.[c]; })()
        : _rvOriginalResult[key];
    const currVal = key.startsWith('line_item_')
        ? (() => { const p = key.replace('line_item_', '').split('_'); const r = parseInt(p[0], 10); const c = p.slice(1).join('_'); return _rvResult.line_items?.[r]?.[c]; })()
        : _rvResult[key];
    return String(origVal ?? '') !== String(currVal ?? '');
}

function _rvComparableValue(value) {
    if (value == null) return '';
    if (typeof value === 'string') return value.replace(/\s+/g, ' ').trim();
    return JSON.stringify(value);
}

function _rvTypedOnlyChangedFields(payload, fieldLocs) {
    const fields = [];

    const collect = (current, original, locs, prefix = '') => {
        const curr = current && typeof current === 'object' && !Array.isArray(current) ? current : {};
        const orig = original && typeof original === 'object' && !Array.isArray(original) ? original : {};
        const allKeys = new Set([...Object.keys(curr), ...Object.keys(orig)]);
        for (const key of allKeys) {
            if (key === 'line_items' || key.startsWith('_')) continue;
            if (_rvComparableValue(curr[key]) === _rvComparableValue(orig[key])) continue;
            const loc = locs && locs[key];
            if (!loc || loc.strategy !== 'manual') {
                fields.push(prefix ? `${prefix}: ${key}` : key);
            }
        }
    };

    if (Array.isArray(payload)) {
        payload.forEach((record, idx) => {
            const original = Array.isArray(_rvAllOriginalResults) ? _rvAllOriginalResults[idx] : {};
            const locs = Array.isArray(fieldLocs) ? fieldLocs[idx] : {};
            collect(record, original, locs, `document ${idx + 1}`);
        });
    } else {
        collect(payload, _rvSingleOriginalResult, fieldLocs || {});
    }

    return fields;
}

function _rvConfirmTypedOnlyChanges(fields) {
    if (!fields.length) return true;
    const preview = fields.slice(0, 6).join(', ');
    const extra = fields.length > 6 ? ` and ${fields.length - 6} more` : '';
    return window.confirm(
        `Manual typed edits will override Qwen's final JSON value for: ${preview}${extra}.\n\n` +
        `These typed edits will NOT create spatial memory because no value box was selected on the document.\n\n` +
        `Use the draw/select button when you want future same-layout PDFs to reuse a field location.\n\n` +
        `Continue saving these value-only corrections?`
    );
}

function rvRenderFields() {
    const el = document.getElementById('rvFieldsList');
    if (!el) return;

    const headerKeys = _rvHeaderKeys();

    if (!headerKeys.length) {
        el.innerHTML = '<div style="padding:10px;color:var(--text-dim);font-size:10px">No fields extracted</div>';
    } else {
        el.innerHTML = headerKeys.map(key => {
            const dotClass = rvGetDotClass(key);
            const val = _rvResult[key] ?? '';
            const isSelecting = _rvSelectionField === key;
            const changed = _isFieldChanged(key);
            const safeKey = escapeHtml(key);
            const safeKeyJs = escapeInlineJsString(key);
            const safeVal = escapeHtml(String(val));
            const resetBtn = changed
                ? `<button class="rv-reset-btn" title="Reset to original" onclick="event.stopPropagation(); rvResetField('${safeKeyJs}')">↻</button>`
                : '';
            return `
            <div class="review-field-item ${isSelecting ? 'selecting' : ''} ${changed ? 'rv-changed' : ''}" id="rvField_${safeKey}"
                 onmouseenter="rvHighlight('${safeKeyJs}')"
                 onmouseleave="rvClearHighlight()"
                 onclick="rvFocusField('${safeKeyJs}')">
                <div class="review-field-dot ${dotClass}"></div>
                <div class="review-field-label">${safeKey}</div>
                <input class="review-field-input" value="${safeVal}"
                       data-field="${safeKey}" oninput="rvOnFieldEdit(this)" />
                <button class="rv-draw-btn" title="Draw on PDF to correct" onclick="event.stopPropagation(); rvStartSelection('${safeKeyJs}')">✎</button>
                ${resetBtn}
            </div>`;
        }).join('');
    }

    // Line items table
    rvRenderLineItems();
    _rvUpdateStats();
}

function rvOnFieldEdit(input) {
    const fieldKey = input.dataset.field;
    _rvResult[fieldKey] = input.value;
    _rvDirty = true;
    rvUpdateJSON();

    // Check if changed and update CSS/reset button dynamically to avoid losing input focus
    const changed = _isFieldChanged(fieldKey);
    const itemEl = document.getElementById(`rvField_${fieldKey}`);
    if (itemEl) {
        if (changed) {
            itemEl.classList.add('rv-changed');
            if (!itemEl.querySelector('.rv-reset-btn')) {
                const btn = document.createElement('button');
                btn.className = 'rv-reset-btn';
                btn.title = 'Reset to original';
                btn.onclick = (e) => { e.stopPropagation(); rvResetField(fieldKey); };
                btn.innerHTML = '↻';
                itemEl.appendChild(btn);
            }
        } else {
            itemEl.classList.remove('rv-changed');
            const btn = itemEl.querySelector('.rv-reset-btn');
            if (btn) btn.remove();
        }
    }
}

function rvUpdateJSON() {
    const pre = document.getElementById('rvJsonPre');
    if (pre) {
        pre.textContent = JSON.stringify(_rvResult, null, 2);
    }
}


function rvRenderLineItems() {
    const liWrap = document.getElementById('rvLineItemsWrap');
    if (!liWrap) return;
    const lineItems = _rvResult.line_items;
    if (!lineItems || !Array.isArray(lineItems) || !lineItems.length) {
        liWrap.innerHTML = '<div style="padding:10px;color:var(--text-dim);font-size:10px">No line items</div>';
        return;
    }
    const cols = Object.keys(lineItems[0]).filter(c => c !== '_page');
    
    let renderedItems = lineItems.map((row, idx) => ({ row, idx }));
    if (!_rvIsPoPerPage) {
        renderedItems = renderedItems.filter(item => {
            const p = item.row._page !== undefined ? item.row._page : 1;
            return p === _rvCurrentPage;
        });
    }

    if (!renderedItems.length) {
        liWrap.innerHTML = '<div style="padding:10px;color:var(--text-dim);font-size:10px">No line items on this page.</div>';
        return;
    }

    liWrap.innerHTML = `
    <table class="review-line-items">
        <thead><tr>${cols.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>
        <tbody>${renderedItems.map(({row, idx: rowIdx}) =>
        `<tr>${cols.map(c => {
            const compKey = `line_item_${rowIdx}_${c}`;
            const loc = _rvFieldLocs[compKey];
            const cellClass = rvGetLineItemCellClass(loc);
            const selClass = _rvSelectionField === compKey ? 'cell-selecting' : '';
            const cellValue = String(row[c] ?? '');
            const safeCompKey = escapeHtml(compKey);
            const safeCompKeyJs = escapeInlineJsString(compKey);
            return `<td class="${cellClass} ${selClass}" title="${escapeHtml(cellValue)}"
                            id="rvField_${safeCompKey}"
                            data-comp-key="${safeCompKey}" data-row="${rowIdx}" data-col="${escapeHtml(c)}"
                            onclick="rvStartSelection('${safeCompKeyJs}')"
                            onmouseenter="rvHighlight('${safeCompKeyJs}')"
                            onmouseleave="rvClearHighlight()">${escapeHtml(cellValue)}</td>`;
        }).join('')}</tr>`
    ).join('')}</tbody>
    </table>`;
}

// ── Review: Render current page image + mapping rectangles ────────────

function rvRenderCurrentPage() {
    if (!_rvPages.length) {
        const container = document.getElementById('rvMappingContainer');
        if (container) container.innerHTML = '<div style="padding:40px;color:var(--text-dim);text-align:center">No page images available.<br>Run extraction first.</div>';
        return;
    }
    const page = _rvPages.find(p => p.page_number === _rvCurrentPage);
    if (!page) return;

    const container = document.getElementById('rvMappingContainer');
    if (!container) return;

    const mime = safeMimeType(page.mime_type || 'image/jpeg');
    const img = document.createElement('img');
    img.src = `data:${mime};base64,${page.image_b64}`;
    img.alt = `Page ${_rvCurrentPage}`;
    img.className = 'doc-img';
    img.id = 'rvDocImg';
    img.style.display = 'block';
    img.onload = () => rvOnImageLoad();
    container.replaceChildren(img);
    rvApplyZoom();
    rvUpdatePageNav();
}

function rvOnImageLoad() {
    rvRenderMappingRects();
    setTimeout(rvRenderMappingLines, 80);
}

// ── Review: Blue dotted rectangles on the PDF ─────────────────────────

function rvRenderMappingRects() {
    const container = document.getElementById('rvMappingContainer');
    const img = document.getElementById('rvDocImg');
    if (!container || !img) return;

    // Remove old rects (but NOT the selection box or ocr overlays)
    container.querySelectorAll('.mapping-rect').forEach(r => r.remove());

    const natW = img.naturalWidth;
    const natH = img.naturalHeight;
    if (!natW || !natH) return;

    const dispW = img.clientWidth;
    const dispH = img.clientHeight;
    const scaleX = dispW / natW;
    const scaleY = dispH / natH;

    for (const [fieldName, loc] of Object.entries(_rvFieldLocs)) {
        if (loc.page !== _rvCurrentPage) continue;
        const box = loc.box;
        if (!box || box.length < 4) continue;

        const left = box[0] * scaleX;
        const top = box[1] * scaleY;
        const width = (box[2] - box[0]) * scaleX;
        const height = (box[3] - box[1]) * scaleY;

        const rect = document.createElement('div');
        let rectClass = 'mapping-rect';
        if (loc.strategy === 'manual') rectClass += ' mapping-rect-manual';
        else if (loc.strategy === 'spatial_memory') rectClass += ' mapping-rect-memory';
        else if (loc.strategy === 'qwen_anchor' || loc.strategy === 'qwen_layout') rectClass += ' mapping-rect-anchor';
        else if (loc.strategy === 'qwen_column_header') rectClass += ' mapping-rect-anchor';
        else if (loc.strategy === 'qwen_anchor_missing' || loc.strategy === 'qwen_column_header_missing') rectClass += ' mapping-rect-low';
        else if (loc.match_mode === 'row_fallback_vertical' || loc.match_mode === 'column_inferred') rectClass += ' mapping-rect-vertical mapping-rect-low';
        else if (loc.match_mode === 'column_fuzzy') rectClass += ' mapping-rect-low';
        else if (rvIsLowConfidenceLoc(loc)) rectClass += ' mapping-rect-low';
        rect.className = rectClass;
        rect.id = `rvRect_${fieldName}`;
        rect.style.left = `${left}px`;
        rect.style.top = `${top}px`;
        rect.style.width = `${width}px`;
        rect.style.height = `${height}px`;

        // Show short label
        const label = fieldName.startsWith('line_item_') ? fieldName.replace('line_item_', 'LI ') : fieldName;
        const labelEl = document.createElement('div');
        labelEl.className = 'mapping-rect-label';
        labelEl.textContent = label;
        rect.replaceChildren(labelEl);
        rect.onmouseenter = () => rvHighlight(fieldName);
        rect.onmouseleave = () => rvClearHighlight();
        rect.onclick = () => rvFocusField(fieldName);

        container.appendChild(rect);
    }
}

// ── Review: SVG connection lines ──────────────────────────────────────

function rvRenderMappingLines() {
    let svg = document.getElementById('rvMappingSvg');
    if (svg) svg.remove();

    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('mapping-svg');
    svg.id = 'rvMappingSvg';
    document.body.appendChild(svg);

    for (const [fieldName, loc] of Object.entries(_rvFieldLocs)) {
        if (loc.page !== _rvCurrentPage) continue;

        const rectEl = document.getElementById(`rvRect_${fieldName}`);
        if (!rectEl) continue;

        // Collect all UI field elements that should map to this rect
        let fieldElements = [];
        const directField = document.getElementById(`rvField_${fieldName}`);
        if (directField) {
            fieldElements.push(directField);
        } else {
            // It might be a column header (e.g., 'qty'), so find all line_item cells for this column
            const cells = document.querySelectorAll(`td[id^="rvField_line_item_"][id$="_${fieldName}"]`);
            cells.forEach(c => fieldElements.push(c));
        }

        if (fieldElements.length === 0) continue;

        const mRect = rectEl.getBoundingClientRect();
        const x2 = mRect.left;
        const y2 = mRect.top + mRect.height / 2;

        fieldElements.forEach(fieldEl => {
            const fRect = fieldEl.getBoundingClientRect();
            const x1 = fRect.right;
            const y1 = fRect.top + fRect.height / 2;

            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.classList.add('mapping-line');
            // If it's a column, generate a compound ID so they don't clash
            const isCol = !directField;
            line.id = isCol ? `rvLine_${fieldEl.id.replace('rvField_', '')}` : `rvLine_${fieldName}`;
            line.setAttribute('x1', x1);
            line.setAttribute('y1', y1);
            line.setAttribute('x2', x2);
            line.setAttribute('y2', y2);
            svg.appendChild(line);
        });
    }
}

// ── Review: Highlight interactions ────────────────────────────────────

function rvHighlight(fieldName) {
    activeMapField = fieldName;
    document.querySelectorAll('.review-field-item').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.review-line-items td').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.mapping-rect').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.mapping-line').forEach(el => el.classList.remove('active'));

    const fieldEl = document.getElementById(`rvField_${fieldName}`);
    if (fieldEl) fieldEl.classList.add('active');
    const rectEl = document.getElementById(`rvRect_${fieldName}`);
    if (rectEl) rectEl.classList.add('active');
    const lineEl = document.getElementById(`rvLine_${fieldName}`);
    if (lineEl) lineEl.classList.add('active');
}

function rvClearHighlight() {
    activeMapField = null;
    document.querySelectorAll('.review-field-item').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.review-line-items td').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.mapping-rect').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.mapping-line').forEach(el => el.classList.remove('active'));
}

function rvFocusField(fieldName) {
    rvHighlight(fieldName);
    const loc = _rvFieldLocs[fieldName];
    if (loc && loc.page !== _rvCurrentPage) {
        _rvCurrentPage = loc.page;
        rvRenderCurrentPage();
    }
    const rectEl = document.getElementById(`rvRect_${fieldName}`);
    if (rectEl) rectEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    const fieldEl = document.getElementById(`rvField_${fieldName}`);
    if (fieldEl) {
        const input = fieldEl.querySelector('.review-field-input');
        if (input) input.focus();
    }
}

// ══════════════════════════════════════════════════════════════════════
// PHASE 1 & 4: CLICK-TO-SELECT (Draw rectangle on PDF to correct fields)
// ══════════════════════════════════════════════════════════════════════

function rvStartSelection(fieldKey) {
    // Toggle selection mode
    if (_rvSelectionField === fieldKey) {
        rvCancelSelection();
        return;
    }
    _rvSelectionField = fieldKey;
    _rvSelecting = false;

    // Add crosshair cursor
    const viewer = document.getElementById('rvViewer');
    if (viewer) viewer.classList.add('rv-selection-active');

    // Update sidebar to show which field is in selection mode
    rvRenderFields();

    showToast(`Draw a box on the PDF to set ${fieldKey.replace(/_/g, ' ')}`);
}

function rvCancelSelection() {
    _rvSelectionField = null;
    _rvSelecting = false;

    const viewer = document.getElementById('rvViewer');
    if (viewer) viewer.classList.remove('rv-selection-active');

    // Remove drawn selection box
    const box = document.querySelector('.rv-selection-box');
    if (box) box.remove();

    // Remove selecting class from sidebar
    document.querySelectorAll('.review-field-item.selecting').forEach(el => el.classList.remove('selecting'));
    document.querySelectorAll('.cell-selecting').forEach(el => el.classList.remove('cell-selecting'));
}

function rvSetupSelectionMode() {
    const container = document.getElementById('rvMappingContainer');
    if (!container) return;

    container.addEventListener('mousedown', (e) => {
        if (!_rvSelectionField) return;
        // Don't start selection if clicking on a mapping-rect
        if (e.target.closest('.mapping-rect')) return;

        e.preventDefault();
        e.stopPropagation();
        _rvSelecting = true;

        // Get coordinates relative to the mapping container (accounting for zoom)
        const rect = container.getBoundingClientRect();
        const zoom = _rvZoom / 100;
        _rvSelStartX = (e.clientX - rect.left) / zoom;
        _rvSelStartY = (e.clientY - rect.top) / zoom;

        // Create the selection box
        let selBox = document.querySelector('.rv-selection-box');
        if (!selBox) {
            selBox = document.createElement('div');
            selBox.className = 'rv-selection-box';
            container.appendChild(selBox);
        }
        selBox.style.left = `${_rvSelStartX}px`;
        selBox.style.top = `${_rvSelStartY}px`;
        selBox.style.width = '0px';
        selBox.style.height = '0px';
        selBox.style.display = 'block';
    });

    container.addEventListener('mousemove', (e) => {
        if (!_rvSelecting) return;
        e.preventDefault();

        const rect = container.getBoundingClientRect();
        const zoom = _rvZoom / 100;
        const curX = (e.clientX - rect.left) / zoom;
        const curY = (e.clientY - rect.top) / zoom;

        const selBox = document.querySelector('.rv-selection-box');
        if (!selBox) return;

        const left = Math.min(_rvSelStartX, curX);
        const top = Math.min(_rvSelStartY, curY);
        const width = Math.abs(curX - _rvSelStartX);
        const height = Math.abs(curY - _rvSelStartY);

        selBox.style.left = `${left}px`;
        selBox.style.top = `${top}px`;
        selBox.style.width = `${width}px`;
        selBox.style.height = `${height}px`;

        // Highlight OCR words inside the drawn rectangle in real-time
        rvHighlightOcrWordsInRect(left, top, width, height);
    });

    container.addEventListener('mouseup', (e) => {
        if (!_rvSelecting) return;
        _rvSelecting = false;

        const rect = container.getBoundingClientRect();
        const zoom = _rvZoom / 100;
        const endX = (e.clientX - rect.left) / zoom;
        const endY = (e.clientY - rect.top) / zoom;

        const left = Math.min(_rvSelStartX, endX);
        const top = Math.min(_rvSelStartY, endY);
        const width = Math.abs(endX - _rvSelStartX);
        const height = Math.abs(endY - _rvSelStartY);

        // Ignore tiny accidental clicks (less than 5px drag)
        if (width < 5 || height < 5) {
            const selBox = document.querySelector('.rv-selection-box');
            if (selBox) selBox.style.display = 'none';
            return;
        }

        // Find OCR words inside the drawn rectangle
        rvApplySelection(left, top, width, height);
    });
}

// ── Intersection Engine: Find OCR words inside drawn rectangle ────────

function rvDisplayRectToOcrBox(displayLeft, displayTop, displayWidth, displayHeight) {
    const img = document.getElementById('rvDocImg');
    if (!img || !img.naturalWidth || !img.naturalHeight || !img.clientWidth || !img.clientHeight) return null;

    const scaleX = img.naturalWidth / img.clientWidth;
    const scaleY = img.naturalHeight / img.clientHeight;
    const ocrLeft = Math.max(0, Math.min(img.naturalWidth, displayLeft * scaleX));
    const ocrTop = Math.max(0, Math.min(img.naturalHeight, displayTop * scaleY));
    const ocrRight = Math.max(0, Math.min(img.naturalWidth, (displayLeft + displayWidth) * scaleX));
    const ocrBottom = Math.max(0, Math.min(img.naturalHeight, (displayTop + displayHeight) * scaleY));

    if (ocrRight <= ocrLeft || ocrBottom <= ocrTop) return null;
    return [Math.round(ocrLeft), Math.round(ocrTop), Math.round(ocrRight), Math.round(ocrBottom)];
}

function rvFindWordsInRect(displayLeft, displayTop, displayWidth, displayHeight) {
    const img = document.getElementById('rvDocImg');
    if (!img || !img.naturalWidth) return [];

    // Convert display coordinates → natural/OCR coordinates
    const scaleX = img.naturalWidth / img.clientWidth;
    const scaleY = img.naturalHeight / img.clientHeight;

    const ocrLeft = displayLeft * scaleX;
    const ocrTop = displayTop * scaleY;
    const ocrRight = (displayLeft + displayWidth) * scaleX;
    const ocrBottom = (displayTop + displayHeight) * scaleY;

    // Find OCR page data for current page
    const pageOcr = _rvOcrData.find(p => p.page_number === _rvCurrentPage);
    if (!pageOcr || !pageOcr.words) return [];

    // Check center-point intersection for each OCR word
    const matchedWords = [];
    for (const word of pageOcr.words) {
        const [wx0, wy0, wx1, wy1] = word.box;
        const cx = (wx0 + wx1) / 2;
        const cy = (wy0 + wy1) / 2;

        if (cx >= ocrLeft && cx <= ocrRight && cy >= ocrTop && cy <= ocrBottom) {
            matchedWords.push(word);
        }
    }

    return matchedWords;
}

function rvHighlightOcrWordsInRect(left, top, width, height) {
    // During drag, highlight OCR words that would be selected
    document.querySelectorAll('.ocr-word-overlay').forEach(el => el.classList.remove('highlighted'));

    const matchedWords = rvFindWordsInRect(left, top, width, height);
    for (const word of matchedWords) {
        // Find the overlay element for this word (if ALT overlay is active)
        const ocrEls = document.querySelectorAll('.ocr-word-overlay');
        ocrEls.forEach(el => {
            if (el.dataset.text === word.text && el.dataset.box === word.box.join(',')) {
                el.classList.add('highlighted');
            }
        });
    }
}

function rvApplySelection(displayLeft, displayTop, displayWidth, displayHeight) {
    const matchedWords = rvFindWordsInRect(displayLeft, displayTop, displayWidth, displayHeight);
    const drawnBox = rvDisplayRectToOcrBox(displayLeft, displayTop, displayWidth, displayHeight);

    if (matchedWords.length === 0 || !drawnBox) {
        showToast('No OCR text found in selection');
        const selBox = document.querySelector('.rv-selection-box');
        if (selBox) selBox.style.display = 'none';
        return;
    }

    // Combine text from matched words (sorted by position: top-to-bottom, left-to-right)
    matchedWords.sort((a, b) => {
        const rowDiff = a.box[1] - b.box[1];
        if (Math.abs(rowDiff) > 10) return rowDiff;  // different lines
        return a.box[0] - b.box[0];  // same line, sort by x
    });
    const combinedText = matchedWords.map(w => w.text.trim()).join(' ');

    const avgScore = matchedWords.reduce((s, w) => s + (w.score || 0), 0) / matchedWords.length;

    // Store pending selection — don't apply yet, show preview bar
    _rvPendingSelection = {
        fieldKey: _rvSelectionField,
        page: _rvCurrentPage,
        combinedText,
        box: drawnBox,
        avgScore,
    };

    // Show the accept/reject preview bar
    _rvShowSelectionPreview();
}

function _rvShowSelectionPreview() {
    // Remove any existing preview
    let bar = document.getElementById('rvSelPreview');
    if (bar) bar.remove();

    const sel = _rvPendingSelection;
    if (!sel) return;

    const displayText = sel.combinedText.length > 50
        ? sel.combinedText.substring(0, 50) + '...'
        : sel.combinedText;
    const fieldLabel = sel.fieldKey.replace(/_/g, ' ');

    bar = document.createElement('div');
    bar.id = 'rvSelPreview';
    bar.className = 'rv-selection-preview';
    const labelEl = document.createElement('span');
    labelEl.className = 'rv-preview-label';
    labelEl.textContent = `${fieldLabel}:`;
    const textEl = document.createElement('span');
    textEl.className = 'rv-preview-text';
    textEl.textContent = `"${displayText}"`;
    const acceptBtn = document.createElement('button');
    acceptBtn.className = 'rv-preview-accept';
    acceptBtn.title = 'Accept correction';
    acceptBtn.textContent = '✓ Accept';
    acceptBtn.onclick = () => rvAcceptSelection();
    const rejectBtn = document.createElement('button');
    rejectBtn.className = 'rv-preview-reject';
    rejectBtn.title = 'Discard selection';
    rejectBtn.textContent = '✗ Reject';
    rejectBtn.onclick = () => rvRejectSelection();
    bar.appendChild(labelEl);
    bar.appendChild(textEl);
    bar.appendChild(acceptBtn);
    bar.appendChild(rejectBtn);
    document.body.appendChild(bar);
}

function rvAcceptSelection() {
    const sel = _rvPendingSelection;
    if (!sel) return;
    if (sel.page !== _rvCurrentPage) {
        showToast('Selection was made on another page. Draw the box again on the current page.');
        rvRejectSelection();
        return;
    }

    const fieldKey = sel.fieldKey;
    const existingCorrection = !fieldKey.startsWith('line_item_')
        ? _rvExistingCorrectionFields[fieldKey]
        : null;
    const existingSpatial = !fieldKey.startsWith('line_item_')
        ? _rvExistingSpatialFields[fieldKey]
        : null;
    if (existingCorrection || existingSpatial) {
        const parts = [];
        if (existingCorrection) parts.push('a saved gold correction');
        if (existingSpatial) parts.push('a saved spatial memory region');
        const ok = window.confirm(
            `"${fieldKey}" already has ${parts.join(' and ')}. Override with this new correction?`
        );
        if (!ok) return;
    }

    // Push undo entry BEFORE applying
    const oldValue = fieldKey.startsWith('line_item_')
        ? (() => { const p = fieldKey.replace('line_item_', '').split('_'); const r = parseInt(p[0], 10); const c = p.slice(1).join('_'); return _rvResult.line_items?.[r]?.[c]; })()
        : _rvResult[fieldKey];
    const oldFieldLoc = _rvFieldLocs[fieldKey] ? JSON.parse(JSON.stringify(_rvFieldLocs[fieldKey])) : null;
    _rvUndoStack.push({ fieldKey, oldValue, oldFieldLoc });

    // Apply the correction
    if (fieldKey.startsWith('line_item_')) {
        const parts = fieldKey.replace('line_item_', '').split('_');
        const rowIdx = parseInt(parts[0], 10);
        const colName = parts.slice(1).join('_');
        if (_rvResult.line_items && _rvResult.line_items[rowIdx]) {
            _rvResult.line_items[rowIdx][colName] = sel.combinedText;
        }
    } else {
        _rvResult[fieldKey] = sel.combinedText;
    }

    // Update field_locations
    _rvFieldLocs[fieldKey] = {
        page: sel.page,
        box: sel.box,
        matched_text: sel.combinedText,
        score: Math.round(sel.avgScore * 10000) / 10000,
        strategy: 'manual',
        confidence: 'high',
    };

    _rvDirty = true;
    _rvPendingSelection = null;

    // Clean up selection UI
    rvCancelSelection();
    const previewBar = document.getElementById('rvSelPreview');
    if (previewBar) previewBar.remove();

    // Re-render everything
    rvRenderFields();
    rvRenderMappingRects();
    setTimeout(rvRenderMappingLines, 80);
    rvUpdateJSON();

    showToast(`✓ Accepted: ${fieldKey.replace(/_/g, ' ')}`);
}

function rvRejectSelection() {
    _rvPendingSelection = null;
    const previewBar = document.getElementById('rvSelPreview');
    if (previewBar) previewBar.remove();
    const selBox = document.querySelector('.rv-selection-box');
    if (selBox) selBox.style.display = 'none';
    showToast('Selection discarded');
}

function rvResetField(fieldKey) {
    // Push undo entry before resetting
    const oldValue = fieldKey.startsWith('line_item_')
        ? (() => { const p = fieldKey.replace('line_item_', '').split('_'); const r = parseInt(p[0], 10); const c = p.slice(1).join('_'); return _rvResult.line_items?.[r]?.[c]; })()
        : _rvResult[fieldKey];
    const oldFieldLoc = _rvFieldLocs[fieldKey] ? JSON.parse(JSON.stringify(_rvFieldLocs[fieldKey])) : null;
    _rvUndoStack.push({ fieldKey, oldValue, oldFieldLoc });

    // Restore original value
    if (fieldKey.startsWith('line_item_')) {
        const parts = fieldKey.replace('line_item_', '').split('_');
        const rowIdx = parseInt(parts[0], 10);
        const colName = parts.slice(1).join('_');
        const origVal = _rvOriginalResult.line_items?.[rowIdx]?.[colName];
        if (_rvResult.line_items && _rvResult.line_items[rowIdx]) {
            _rvResult.line_items[rowIdx][colName] = origVal;
        }
    } else {
        _rvResult[fieldKey] = _rvOriginalResult[fieldKey];
    }
    // Remove manual field location — let it fall back to auto-matched
    delete _rvFieldLocs[fieldKey];
    _rvDirty = true;

    rvRenderFields();
    rvRenderMappingRects();
    setTimeout(rvRenderMappingLines, 80);
    rvUpdateJSON();
    showToast(`↻ Reset: ${fieldKey.replace(/_/g, ' ')} to original`);
}

function rvUndo() {
    if (!_rvUndoStack.length) {
        showToast('Nothing to undo');
        return;
    }
    const entry = _rvUndoStack.pop();
    const fieldKey = entry.fieldKey;

    // Restore old value
    if (fieldKey.startsWith('line_item_')) {
        const parts = fieldKey.replace('line_item_', '').split('_');
        const rowIdx = parseInt(parts[0], 10);
        const colName = parts.slice(1).join('_');
        if (_rvResult.line_items && _rvResult.line_items[rowIdx]) {
            _rvResult.line_items[rowIdx][colName] = entry.oldValue;
        }
    } else {
        _rvResult[fieldKey] = entry.oldValue;
    }

    // Restore old field location
    if (entry.oldFieldLoc) {
        _rvFieldLocs[fieldKey] = entry.oldFieldLoc;
    } else {
        delete _rvFieldLocs[fieldKey];
    }

    rvRenderFields();
    rvRenderMappingRects();
    setTimeout(rvRenderMappingLines, 80);
    rvUpdateJSON();
    showToast(`Undo: ${fieldKey.replace(/_/g, ' ')}`);
}

// ══════════════════════════════════════════════════════════════════════
// PHASE 5: ALT-KEY OCR WORD OVERLAY
// ══════════════════════════════════════════════════════════════════════

function rvSetupAltOverlay() {
    const handler_keydown = (e) => {
        if (e.key === 'Alt') {
            e.preventDefault();
            rvShowOcrOverlay();
        }
    };
    const handler_keyup = (e) => {
        if (e.key === 'Alt') {
            rvHideOcrOverlay();
        }
    };

    // Remove old listeners using stored references (not the new ones!)
    if (window._rvAltDown) document.removeEventListener('keydown', window._rvAltDown);
    if (window._rvAltUp) document.removeEventListener('keyup', window._rvAltUp);

    // Store references for next cleanup
    window._rvAltDown = handler_keydown;
    window._rvAltUp = handler_keyup;

    document.addEventListener('keydown', handler_keydown);
    document.addEventListener('keyup', handler_keyup);
}

function rvShowOcrOverlay() {
    const container = document.getElementById('rvMappingContainer');
    const img = document.getElementById('rvDocImg');
    if (!container || !img || !img.naturalWidth) return;

    // Don't add if already visible
    if (container.querySelector('.ocr-word-overlay')) return;

    const pageOcr = _rvOcrData.find(p => p.page_number === _rvCurrentPage);
    if (!pageOcr || !pageOcr.words) return;

    const scaleX = img.clientWidth / img.naturalWidth;
    const scaleY = img.clientHeight / img.naturalHeight;

    for (const word of pageOcr.words) {
        const [wx0, wy0, wx1, wy1] = word.box;
        const el = document.createElement('div');
        el.className = 'ocr-word-overlay';
        el.style.left = `${wx0 * scaleX}px`;
        el.style.top = `${wy0 * scaleY}px`;
        el.style.width = `${(wx1 - wx0) * scaleX}px`;
        el.style.height = `${(wy1 - wy0) * scaleY}px`;
        el.title = word.text;
        el.dataset.text = word.text;
        el.dataset.box = word.box.join(',');
        container.appendChild(el);
    }
}

function rvHideOcrOverlay() {
    const container = document.getElementById('rvMappingContainer');
    if (!container) return;
    container.querySelectorAll('.ocr-word-overlay').forEach(el => el.remove());
}

// ── Review: Page navigation ──────────────────────────────────────────

function rvChangePage(dir) {
    _rvSetCurrentPage(_rvCurrentPage + dir);
    return;  // _rvSetCurrentPage handles rendering
}

function rvUpdatePageNav() {
    const ind = document.getElementById('rvPageInd');
    if (ind) ind.textContent = `PAGE ${_rvCurrentPage} / ${_rvTotalPages}`;
    const prev = document.getElementById('rvPrevBtn');
    const next = document.getElementById('rvNextBtn');
    if (prev) prev.disabled = _rvCurrentPage <= 1;
    if (next) next.disabled = _rvCurrentPage >= _rvTotalPages;
}

// ── Review: Zoom & Pan ───────────────────────────────────────────────

function rvSetupDragZoom() {
    const canvas = document.getElementById('rvCanvas');
    if (!canvas) return;

    canvas.addEventListener('wheel', e => {
        e.preventDefault();
        const delta = e.deltaY > 0 ? -10 : 10;
        _rvZoom = Math.max(25, Math.min(400, _rvZoom + delta));
        rvApplyZoom();
        setTimeout(rvRenderMappingLines, 80);
    }, { passive: false });

    canvas.addEventListener('mousedown', e => {
        // Only pan if NOT in selection mode
        if (_rvSelectionField) return;
        if (e.button !== 0) return;
        _rvIsDragging = true; canvas.classList.add('dragging');
        _rvDragStartX = e.clientX; _rvDragStartY = e.clientY;
        _rvScrollStartX = canvas.scrollLeft; _rvScrollStartY = canvas.scrollTop;
        e.preventDefault();
    });
    canvas.addEventListener('mousemove', e => {
        if (!_rvIsDragging) return;
        canvas.scrollLeft = _rvScrollStartX - (e.clientX - _rvDragStartX);
        canvas.scrollTop = _rvScrollStartY - (e.clientY - _rvDragStartY);
    });
    canvas.addEventListener('mouseup', () => { _rvIsDragging = false; canvas.classList.remove('dragging'); });
    canvas.addEventListener('mouseleave', () => { _rvIsDragging = false; canvas.classList.remove('dragging'); });
    canvas.addEventListener('scroll', () => { requestAnimationFrame(rvRenderMappingLines); });
    // Remove old resize listener to prevent accumulation, then add fresh one
    if (_rvResizeHandler) window.removeEventListener('resize', _rvResizeHandler);
    _rvResizeHandler = () => { requestAnimationFrame(rvRenderMappingLines); };
    window.addEventListener('resize', _rvResizeHandler);
}

function rvApplyZoom() {
    const frame = document.getElementById('rvDocFrame');
    const info = document.getElementById('rvZoomInfo');
    if (frame) frame.style.transform = `scale(${_rvZoom / 100})`;
    if (info) info.textContent = `ZOOM: ${_rvZoom}% | HOLD ALT TO SEE OCR`;
    rvRenderMappingRects();
}

// ── Review: Actions ──────────────────────────────────────────────────

function rvCopyJSON() {
    navigator.clipboard.writeText(JSON.stringify(_rvCurrentPayload(), null, 2));
    showToast('JSON copied to clipboard');
}

function rvDownloadJSON() {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(_rvCurrentPayload(), null, 2)], { type: 'application/json' }));
    a.download = `review_${_rvExtractionId || Date.now()}.json`;
    a.click();
}

async function rvConfirm() {
    // Save corrections to backend if changes were made
    if (_rvDirty && _rvExtractionId) {
        try {
            _rvPersistCurrentRecord();
            const payload = _rvCurrentPayload();
            const finalFieldLocs = _rvIsPoPerPage ? _rvAllFieldLocs : _rvFieldLocs;
            const typedOnlyFields = _rvTypedOnlyChangedFields(payload, finalFieldLocs);
            if (!_rvConfirmTypedOnlyChanges(typedOnlyFields)) {
                return;
            }
            await apiJSON(`/extractions/${_rvExtractionId}/corrections`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    corrected_result: payload,
                    field_locations: finalFieldLocs,
                }),
            });
            // Update in-memory snapshots so reopening review shows saved state
            reviewResult = JSON.parse(JSON.stringify(payload));
            reviewFieldLocations = JSON.parse(JSON.stringify(finalFieldLocs));
            showToast('Corrections saved successfully');
        } catch (e) {
            showToast('Failed to save corrections: ' + e.message);
            return;
        }
    } else {
        showToast('Review confirmed (no changes)');
    }
    navigate('#/history');
}

// Keyboard shortcuts for review
document.addEventListener('keydown', (e) => {
    // Escape: cancel selection or reject pending preview
    if (e.key === 'Escape') {
        if (_rvPendingSelection) {
            rvRejectSelection();
        } else if (_rvSelectionField) {
            rvCancelSelection();
        }
    }
    // Ctrl+Z: undo last correction
    if (e.key === 'z' && (e.ctrlKey || e.metaKey) && !e.shiftKey) {
        if (_rvUndoStack && _rvUndoStack.length && document.querySelector('.review-field-panel')) {
            e.preventDefault();
            rvUndo();
        }
    }
});
