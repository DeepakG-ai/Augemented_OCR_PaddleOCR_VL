/* ── Augmented OCR — Extraction page (upload, pipeline viz, SSE, exports) */

// ══════════════════════════════════════════════════════════════════════
// PAGE 4: EXTRACTION (preserving old UI exactly)
// ══════════════════════════════════════════════════════════════════════
async function renderExtractPage(app) {
    const user = getAuthUser();
    const isAdmin = user && user.role === 'admin';

    // ── Admin: load client list for the "Act As Client" selector ──────────
    if (isAdmin) {
        try {
            const allUsers = await apiJSON('/admin/users');
            actAsClientList = allUsers
                .filter(u => u.role === 'client' && u.is_active)
                .map(u => ({ id: u.id, email: u.email }));
        } catch (e) { actAsClientList = []; }
        // Restore previous selection from session storage
        const saved = sessionStorage.getItem('actAsClientId');
        if (saved && (saved === 'ADMIN' || actAsClientList.find(c => c.id === saved))) {
            actAsClientId = saved;
        } else {
            // Forces admin to explicitly select a context before continuing
            actAsClientId = null;
        }
    }

    // ── Load vendors scoped to selected client (or admin's own) ──────────
    await extReloadVendors();

    const savedVid = localStorage.getItem('extractVendor');
    if (savedVid && db.vendors.find(v => v.id === savedVid)) {
        db.activeVendorId = savedVid;
        localStorage.removeItem('extractVendor');
    } else if (!savedVid) {
        db.activeVendorId = null;
    }
    detectedVendorName = null;

    // ── Build admin "Act As Client" dropdown HTML ─────────────────────────
    const clientDropdownHTML = isAdmin ? `
        <div class="sidebar-section" style="padding-bottom:0">
            <div class="section-title" style="display:flex;align-items:center;gap:6px">
                <span>&#9881; Acting As Client</span>
                <span style="font-size:9px;color:var(--blue);letter-spacing:0.08em;font-weight:600">ADMIN</span>
            </div>
        </div>
        <div style="padding:0 0 10px 0">
            <select
                id="actAsClientSelect"
                class="modal-input"
                style="width:100%;font-size:11px;padding:5px 8px;cursor:pointer;border-color:var(--blue-dim)"
                onchange="extSetActAsClient(this.value)"
            >
                <option value="" ${!actAsClientId ? 'selected' : ''} disabled>&#8212; Select Client Context &#8212;</option>
                <option value="ADMIN" ${actAsClientId === 'ADMIN' ? 'selected' : ''}>Admin (myself)</option>
                ${actAsClientList.map(c =>
                    `<option value="${escapeHtml(c.id)}" ${actAsClientId === c.id ? 'selected' : ''}>${escapeHtml(c.email)}</option>`
                ).join('')}
            </select>
            <div id="actAsClientInfo" style="font-size:9px;color:var(--text-dim);margin-top:4px;line-height:1.5">
                ${!actAsClientId
                    ? '<strong style="color:var(--red)">Please select a client context to enable extraction.</strong>'
                    : actAsClientId === 'ADMIN'
                        ? 'Using admin&#39;s own vendors for detection.'
                        : `Detection &amp; vendor list scoped to: <strong style="color:var(--blue)">${escapeHtml((actAsClientList.find(c => c.id === actAsClientId) || {}).email || '')}</strong>`}
            </div>
        </div>
        <div class="divider"></div>` : '';

    app.innerHTML = headerHTML() + `
    <aside class="sidebar">
        ${clientDropdownHTML}
        <div class="sidebar-section"><div class="section-title">Vendor Detection</div></div>
        <div class="detected-vendor-card" id="detectedVendorCard">
            <div class="detected-vendor-label">Vendor Status</div>
            <div class="detected-vendor-name" id="detectedVendorName">Upload a document</div>
            <div class="detected-vendor-detail" id="detectedVendorDetail">System will auto-detect vendor from page 1.</div>
        </div>
        <div class="divider"></div>
        <div class="sidebar-section"><div class="section-title">Document</div></div>
        <div class="upload-zone" id="dropzone" onclick="document.getElementById('fileInput').click()">
            <div class="upload-icon">⬆</div>
            <div class="upload-text"><strong>Drop file or browse</strong><br>PDF only</div>
        </div>
        <input type="file" id="fileInput" accept="application/pdf,.pdf" style="display:none" onchange="handleFile(this.files[0])">
        <div id="fileBadge" style="display:none" class="file-badge"><span>✓</span><span id="fileNameLabel" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span></div>
    </aside>
    <main class="viewer">
        <div id="extractionView" style="display:flex;flex-direction:column;width:100%;height:100%">
            <div class="viewer-toolbar">
                <div class="page-nav">
                    <button class="nav-btn" id="prevBtn" onclick="changePage(-1)">◀ PREV</button>
                    <span class="page-indicator" id="pageIndicator">PAGE 1 / 1</span>
                    <button class="nav-btn" id="nextBtn" onclick="changePage(1)">NEXT ▶</button>
                </div>
                <span class="zoom-info" id="zoomInfo">ZOOM: 100% · DRAG TO PAN</span>
            </div>
            <div class="viewer-canvas" id="viewerCanvas">
                <div class="no-doc" id="noDoc">
                    <div class="no-doc-icon">▣</div>
                    <div class="no-doc-text">No document loaded</div>
                    <div style="font-size:10px;color:var(--text-dim);margin-top:4px">Upload a file to begin extraction</div>
                </div>
                <div id="docFrame" class="doc-frame" style="display:none;width:100%">
                    <div id="docImgContainer" style="width:100%;display:flex;align-items:flex-start;justify-content:center"></div>
                </div>
            </div>
        </div>
    </main>
    <aside class="right-panel">
        <div class="rp-section">
            <div class="rp-title">Active Entity</div>
            <div class="entity-name" id="rpEntityName">—</div>
            <span class="rp-badge optimal" id="rpBadge">OPTIMAL</span>
        </div>
        ${buildPipelineHTML()}
        <div class="rp-section" id="conflictSection" style="display:none">
            <div class="rp-title" style="color:var(--red)">⚠ Needs Review</div>
            <div id="conflictMsg"></div><div id="conflictCandidates"></div>
        </div>
        <div class="rp-section" id="resultSection" style="display:none">
            <div class="rp-title">Extracted Data</div>
            <div class="result-block" id="resultBlock"></div>
            <div style="display:flex;gap:6px;margin-top:6px">
                <button class="small-btn" style="flex:1" onclick="copyResult()">Copy JSON</button>
                <button class="small-btn" style="flex:1" onclick="downloadResult()">Download JSON</button>
            </div>
        </div>
    </aside>
    <div class="bottom-bar">
        <button class="extract-btn" id="extractBtn" onclick="runExtract()" disabled>EXTRACT</button>
        <button class="cancel-btn" id="cancelBtn" style="display:none" onclick="cancelExtract()">&#9632; CANCEL</button>
    </div>` + vendorModalHTML();

    await extLoadVendorConfig();
    setupDragZoom();
    setupDropzone();
    updateNavActive();
}

// ── Extract page helpers ──────────────────────────────────────────────

/**
 * Reload vendors scoped to the currently selected client (for admin) or
 * the logged-in client's own vendors. Writes to db.vendors.
 */
async function extReloadVendors() {
    try {
        const user = getAuthUser();
        const isAdmin = user && user.role === 'admin';
        let url = '/vendors';
        // For admin: filter by selected client via query param so the server
        // returns only that client's vendors (prevents leaking other tenants).
        if (isAdmin) {
            if (!actAsClientId) {
                db.vendors = [];
                return;
            }
            if (actAsClientId !== 'ADMIN') {
                url = `/vendors?user_id=${encodeURIComponent(actAsClientId)}`;
            }
        }
        const vendors = await apiJSON(url);
        db.vendors = vendors;
    } catch (e) { db.vendors = []; }
}

/**
 * Called when admin changes the "Act As Client" dropdown.
 * Re-scopes the vendor list and clears any prior manual vendor selection.
 */
async function extSetActAsClient(clientId) {
    actAsClientId = clientId || null;
    // Persist for the current browser session
    if (actAsClientId) {
        sessionStorage.setItem('actAsClientId', actAsClientId);
    } else {
        sessionStorage.removeItem('actAsClientId');
    }
    // Clear manual vendor pick — it belonged to the old client
    db.activeVendorId = null;
    detectedVendorName = null;
    // Reload vendors for the new scope
    await extReloadVendors();
    // Update the info label
    const infoEl = document.getElementById('actAsClientInfo');
    if (infoEl) {
        if (!actAsClientId) {
            infoEl.innerHTML = '<strong style="color:var(--red)">Please select a client context to enable extraction.</strong>';
        } else if (actAsClientId === 'ADMIN') {
            infoEl.innerHTML = 'Using admin&#39;s own vendors for detection.';
        } else {
            const client = actAsClientList.find(c => c.id === actAsClientId);
            infoEl.innerHTML = client
                ? `Detection &amp; vendor list scoped to: <strong style="color:var(--blue)">${escapeHtml(client.email)}</strong>`
                : '';
        }
    }
    await extLoadVendorConfig();
    setDetectedVendorDisplay(null, 'Client changed — upload a document to detect vendor.');
    renderBottomBar();
}

async function extSetVendor(id) {
    db.activeVendorId = id;
    await extLoadVendorConfig();
}

function setDetectedVendorDisplay(name, detail = '') {
    detectedVendorName = name || null;
    const nameEl = document.getElementById('detectedVendorName');
    const detailEl = document.getElementById('detectedVendorDetail');
    const rpName = document.getElementById('rpEntityName');
    if (nameEl) nameEl.textContent = name || 'Upload a document';
    if (detailEl) detailEl.textContent = detail || (name ? 'Detected from the uploaded document.' : 'System will auto-detect vendor from page 1.');
    if (rpName) rpName.textContent = name || '---';
}

async function extLoadVendorConfig() {
    const v = db.vendors.find(v => v.id === db.activeVendorId);
    const nameEl = document.getElementById('rpEntityName');
    if (nameEl) nameEl.textContent = v ? v.name : (detectedVendorName || '---');
    if (v) setDetectedVendorDisplay(v.name, 'Configured from vendor/template shortcut.');

    if (v) {
        try {
            const tmpl = await apiJSON(`/vendors/${v.id}/template`);
            extractionRules = [...(tmpl.extraction_rules || [])];
            headerFields = [...(tmpl.header_fields || [])];
            lineItemFields = [...(tmpl.line_item_fields || [])];
            activePromptInstructions = tmpl.prompt_instructions || null;
            activeFormatType = tmpl.format_type || 'single_po_multipage';
        } catch (e) {
            extractionRules = []; headerFields = []; lineItemFields = [];
            activePromptInstructions = null;
            activeFormatType = 'single_po_multipage';
        }
    } else {
        extractionRules = []; headerFields = []; lineItemFields = [];
        activePromptInstructions = null;
        activeFormatType = 'single_po_multipage';
    }

    extUpdateFormatHint(); renderRules(); renderHeaderFields(); renderLineItemFields(); renderBottomBar();
}

function addHeaderField() {
    const inp = document.getElementById('headerFieldInput');
    const val = inp.value.trim().toLowerCase().replace(/\s+/g, '_');
    if (!val || headerFields.includes(val)) return;
    headerFields.push(val); inp.value = '';
    renderHeaderFields(); renderBottomBar();
}
function removeHeaderField(i) { headerFields.splice(i, 1); renderHeaderFields(); renderBottomBar(); }
function renderHeaderFields() {
    const el = document.getElementById('headerFieldsList'); if (!el) return;
    el.innerHTML = headerFields.length ? headerFields.map((f, i) => `<div class="rule-item"><div class="rule-dot" style="background:var(--blue)"></div><span style="flex:1;text-transform:uppercase;letter-spacing:0.08em">${escapeHtml(f)}</span><button class="rule-del" onclick="removeHeaderField(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim);padding:4px 0">No header fields added</div>';
}

function addLineItemField() {
    const inp = document.getElementById('lineItemFieldInput');
    const val = inp.value.trim().toLowerCase().replace(/\s+/g, '_');
    if (!val || lineItemFields.includes(val)) return;
    lineItemFields.push(val); inp.value = '';
    renderLineItemFields(); renderBottomBar();
}
function removeLineItemField(i) { lineItemFields.splice(i, 1); renderLineItemFields(); renderBottomBar(); }
function renderLineItemFields() {
    const el = document.getElementById('lineItemFieldsList'); if (!el) return;
    el.innerHTML = lineItemFields.length ? lineItemFields.map((f, i) => `<div class="rule-item"><div class="rule-dot" style="background:var(--green)"></div><span style="flex:1;text-transform:uppercase;letter-spacing:0.08em">${escapeHtml(f)}</span><button class="rule-del" onclick="removeLineItemField(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim);padding:4px 0">No line item columns added</div>';
}

function renderBottomBar() {
    const btn = document.getElementById('extractBtn');
    if (!btn) return;
    if (activeJobId) return;  // extraction in progress — don't override showStopButton()
    btn.style.display = '';
    btn.textContent = 'EXTRACT';
    btn.disabled = !loadedFile;
    btn.className = 'extract-btn';
    btn.onclick = runExtract;
}

function addRule() {
    const inp = document.getElementById('newRuleInput');
    const val = inp.value.trim(); if (!val) return;
    extractionRules.push(val); inp.value = ''; renderRules();
}
function deleteRule(i) { extractionRules.splice(i, 1); renderRules(); }
function renderRules() {
    const el = document.getElementById('rulesList'); if (!el) return;
    el.innerHTML = extractionRules.length ? extractionRules.map((r, i) => `<div class="rule-item"><div class="rule-dot"></div><span style="flex:1">${escapeHtml(r)}</span><button class="rule-del" onclick="deleteRule(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim);padding:4px 0">No rules yet</div>';
}

async function saveTemplate() {
    const v = db.vendors.find(v => v.id === db.activeVendorId);
    if (!v) {
        showToast('Run extraction first so the system can detect the vendor');
        return;
    }
    addRule();
    const formatEl = document.getElementById('formatType');
    const promptEl = document.getElementById('promptInstructions');
    const payload = {
        format_type: formatEl ? formatEl.value : 'single_po_multipage',
        vendor_name: v.name,
        header_fields: headerFields,
        line_item_fields: lineItemFields,
        prompt_instructions: promptEl ? (promptEl.value || null) : null,
        extraction_rules: extractionRules,
    };
    try {
        const resp = await apiJSON(`/vendors/${v.id}/template`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        showToast(`Template saved — hash: ${(resp.prompt_hash || 'none').slice(0, 12)}...`);
    } catch (e) { showToast('Failed: ' + e.message); }
}

function extUpdateFormatHint() {
    // Guard: these DOM elements only exist on the Template page, not the Extract page.
    const el = document.getElementById('formatHint');
    if (!el) return;
    const hints = { single_po_multipage: 'Page 1: header + line items. Pages 2-N: line items only, same PO.', po_per_page: 'Each page is a self-contained PO with its own header and line items.', single_page: 'Entire document is a single page. Extract all fields at once.' };
    const formatEl = document.getElementById('formatType');
    el.textContent = hints[formatEl ? formatEl.value : 'single_po_multipage'] || '';
}



// ── FILE UPLOAD ────────────────────────────────────────────────────────
function setupDropzone() {
    const dz = document.getElementById('dropzone'); if (!dz) return;
    dz.addEventListener('dragover', e => { e.preventDefault(); dz.style.background = 'var(--blue-bg)'; });
    dz.addEventListener('dragleave', () => { dz.style.background = ''; });
    dz.addEventListener('drop', e => { e.preventDefault(); dz.style.background = ''; if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });
}

async function handleFile(file) {
    if (!file) return;

    // Hard block — only PDFs allowed (matches backend _require_pdf guard)
    const nameLower = (file.name || '').toLowerCase();
    const isPdfByName = nameLower.endsWith('.pdf');
    const isPdfByMime = !file.type || file.type === 'application/pdf';
    if (!isPdfByName || !isPdfByMime) {
        showToast(`Only PDF files are accepted. "${file.name}" was rejected.`);
        const fi = document.getElementById('fileInput'); if (fi) fi.value = '';
        return;
    }

    loadedFile = file;
    document.getElementById('noDoc').style.display = 'none';
    document.getElementById('docFrame').style.display = 'block';
    const dz = document.getElementById('dropzone');
    if (dz) dz.classList.add('has-file');
    document.getElementById('fileBadge').style.display = 'flex';
    document.getElementById('fileNameLabel').textContent = file.name;

    // Use backend preview for proper page rendering
    try {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('max_pages', '5'); // Quick preview; full pages load after extraction
        const resp = await apiJSON('/upload-preview', { method: 'POST', body: formData });
        extractionPages = resp.pages;
        totalPages = extractionPages.length; // Limit pagination to what we actually have in memory
        window.docTotalPages = resp.total_pages; // Store true document length
        currentPage = 1;
        updatePageNav();
        renderCurrentPage();
    } catch (e) {
        // Fallback: client-side PDF preview if /upload-preview is unreachable
        const reader = new FileReader();
        reader.onload = ev => {
            const container = document.getElementById('docImgContainer');
            if (!container) return;
            container.replaceChildren();
            const embed = document.createElement('embed');
            embed.src = ev.target.result;
            embed.type = 'application/pdf';
            embed.style.cssText = 'width:100%;height:600px;border:none;';
            container.appendChild(embed);
            totalPages = 1; currentPage = 1; updatePageNav();
        };
        reader.readAsDataURL(file);
    }
    // Reset pipeline to idle and mark upload complete
    PIPELINE_STAGES.forEach(s => {
        const el = document.getElementById(`pipeStage_${s.id}`);
        if (el) el.className = 'pipeline-stage';
        const badge = document.getElementById(`pipeBadge_${s.id}`);
        if (badge) { badge.className = 'pipeline-badge'; badge.textContent = ''; }
        const detail = document.getElementById(`pipeDetail_${s.id}`);
        if (detail) detail.textContent = s.detail;
    });
    setPipelineStage('upload', 'done', 'Document stream received');
    renderBottomBar();
    setStatus('optimal');
}

function updatePageNav() {
    const ind = document.getElementById('pageIndicator');
    if (ind) {
        if (window.docTotalPages && window.docTotalPages > totalPages) {
            ind.innerHTML = `PAGE ${currentPage} / ${totalPages} <span style="color:var(--text-dim);font-size:9px;margin-left:4px">(of ${window.docTotalPages} total)</span>`;
        } else {
            ind.textContent = `PAGE ${currentPage} / ${totalPages}`;
        }
    }
    const prev = document.getElementById('prevBtn');
    const next = document.getElementById('nextBtn');
    if (prev) prev.disabled = currentPage <= 1;
    if (next) next.disabled = currentPage >= totalPages;
}

function changePage(dir) {
    currentPage = Math.max(1, Math.min(totalPages, currentPage + dir));
    updatePageNav();
    if (extractionPages.length) renderCurrentPage();
}

function renderCurrentPage() {
    if (!extractionPages.length) return;
    const page = extractionPages.find(p => p.page_number === currentPage);
    if (!page) return;
    const container = document.getElementById('docImgContainer');
    if (!container) return;
    const mime = safeMimeType(page.mime_type || 'image/jpeg');
    const img = document.createElement('img');
    img.src = `data:${mime};base64,${page.image_b64}`;
    img.alt = `Page ${currentPage}`;
    img.className = 'doc-img';
    container.replaceChildren(img);
    applyZoom();
}

// ── ZOOM & PAN ─────────────────────────────────────────────────────────
function setupDragZoom() {
    const canvas = document.getElementById('viewerCanvas'); if (!canvas) return;

    // Wheel zoom
    canvas.addEventListener('wheel', e => {
        e.preventDefault();
        const delta = e.deltaY > 0 ? -10 : 10;
        zoomLevel = Math.max(25, Math.min(400, zoomLevel + delta));
        applyZoom();
    }, { passive: false });

    // Drag to pan
    canvas.addEventListener('mousedown', e => {
        if (e.button !== 0) return;
        isDragging = true; canvas.classList.add('dragging');
        dragStartX = e.clientX; dragStartY = e.clientY;
        scrollStartX = canvas.scrollLeft; scrollStartY = canvas.scrollTop;
        e.preventDefault();
    });
    canvas.addEventListener('mousemove', e => {
        if (!isDragging) return;
        canvas.scrollLeft = scrollStartX - (e.clientX - dragStartX);
        canvas.scrollTop = scrollStartY - (e.clientY - dragStartY);
    });
    canvas.addEventListener('mouseup', () => { isDragging = false; canvas.classList.remove('dragging'); });
    canvas.addEventListener('mouseleave', () => { isDragging = false; canvas.classList.remove('dragging'); });
}

function applyZoom() {
    const frame = document.getElementById('docFrame');
    const info = document.getElementById('zoomInfo');
    if (frame) frame.style.transform = `scale(${zoomLevel / 100})`;
    if (info) info.textContent = `ZOOM: ${zoomLevel}% · DRAG TO PAN`;
}

// ── PIPELINE VISUALIZATION ─────────────────────────────────────────────
let _pipelineStartTime = null;
let _pipelineTimerInterval = null;

const PIPELINE_STAGES = [
    { id: 'upload', label: 'Uploading', detail: 'Document stream received' },
    { id: 'detect', label: 'Detecting Vendor', detail: 'Reading page 1' },
    { id: 'normalize', label: 'PDF Rendering', detail: 'Classifying pages and extracting geometry' },
    { id: 'ocr', label: 'OCR Bounding Box', detail: 'Waiting for page classification' },
    { id: 'llm', label: 'Vision Extraction', detail: 'AI Vision extraction' },
    { id: 'json', label: 'JSON Created', detail: 'Structured output assembled' },
    { id: 'postprocess', label: 'Post Processing', detail: 'Field mapping and review memory' },
];

function buildPipelineHTML() {
    const stagesHTML = PIPELINE_STAGES.map(s => `
        <div class="pipeline-stage" id="pipeStage_${s.id}" data-stage="${s.id}">
            <div class="pipeline-dot"></div>
            <div class="pipeline-stage-name">
                <span>${escapeHtml(s.label)}</span>
                <span class="pipeline-badge" id="pipeBadge_${s.id}"></span>
            </div>
            <div class="pipeline-stage-detail" id="pipeDetail_${s.id}">${escapeHtml(s.detail)}</div>
            <div class="pipeline-progress-bar"><div class="pipeline-progress-fill indeterminate" id="pipeFill_${s.id}"></div></div>
        </div>
    `).join('');

    return `
        <div class="pipeline-panel active" id="pipelinePanel">
            <div class="pipeline-header">
                <div class="pipeline-title">Pipeline Sequence</div>
                <div class="pipeline-subtitle">Real-time extraction progress</div>
            </div>
            <div class="pipeline-stages">${stagesHTML}</div>
            <div class="pipeline-elapsed" id="pipelineElapsed">
                Working: <span class="pipeline-elapsed-value" id="pipelineTimer">0.0s</span>
            </div>
        </div>
    `;
}

function _extractHasPipelineFailure(extraction) {
    if (!extraction) return false;
    const result = extraction.result;
    return extraction.error
        || (result && typeof result === 'object' && result._all_pages_failed === true)
        || (Array.isArray(extraction.page_results) && extraction.page_results.some(pr => pr && pr._error));
}

function resetPipelinePanelMarkup() {
    const panel = document.getElementById('pipelinePanel');
    if (!panel || panel.querySelector('.pipeline-stages')) return;
    const holder = document.createElement('div');
    holder.innerHTML = buildPipelineHTML().trim();
    const fresh = holder.firstElementChild;
    if (fresh) panel.replaceWith(fresh);
}

function showPipelinePanel(options = {}) {
    const selectedVendorName = options.selectedVendorName || null;
    resetPipelinePanelMarkup();
    _pipelineSeenStages = new Set();

    // Reset all stages to pending
    PIPELINE_STAGES.forEach(s => {
        const el = document.getElementById(`pipeStage_${s.id}`);
        if (el) el.className = 'pipeline-stage';
        const badge = document.getElementById(`pipeBadge_${s.id}`);
        if (badge) { badge.className = 'pipeline-badge'; badge.textContent = ''; }
        const detail = document.getElementById(`pipeDetail_${s.id}`);
        if (detail) detail.textContent = s.detail;
        const fill = document.getElementById(`pipeFill_${s.id}`);
        if (fill) { fill.className = 'pipeline-progress-fill indeterminate'; fill.style.width = ''; }
    });

    // Mark upload as done immediately (file is already uploaded)
    setPipelineStage('upload', 'done', 'Document stream received');
    if (selectedVendorName) {
        setPipelineStage('detect', 'done', `Manual vendor selected: ${selectedVendorName}. Auto-detection skipped.`);
    } else {
        setPipelineStage('detect', 'active', 'Reading page 1 for vendor');
    }

    // Start elapsed timer
    _pipelineStartTime = Date.now();
    if (_pipelineTimerInterval) clearInterval(_pipelineTimerInterval);
    _pipelineTimerInterval = setInterval(() => {
        const el = document.getElementById('pipelineTimer');
        if (el && _pipelineStartTime) {
            const elapsed = ((Date.now() - _pipelineStartTime) / 1000).toFixed(1);
            el.textContent = `${elapsed}s`;
        }
    }, 100);
}

function hidePipelinePanel() {
    if (_pipelineTimerInterval) { clearInterval(_pipelineTimerInterval); _pipelineTimerInterval = null; }
}

function setPipelineStage(stageId, state, detail) {
    const el = document.getElementById(`pipeStage_${stageId}`);
    if (!el) return;
    el.className = `pipeline-stage ${state}`;

    const badge = document.getElementById(`pipeBadge_${stageId}`);
    if (badge) {
        if (state === 'done') {
            badge.className = 'pipeline-badge done';
            badge.textContent = 'DONE';
        } else if (state === 'active') {
            badge.className = 'pipeline-badge active';
            badge.textContent = '...';
        } else if (state === 'failed') {
            badge.className = 'pipeline-badge failed';
            badge.textContent = 'FAIL';
        } else {
            badge.className = 'pipeline-badge';
            badge.textContent = '';
        }
    }

    if (detail) {
        const det = document.getElementById(`pipeDetail_${stageId}`);
        if (det) det.textContent = detail;
    }
}

function setPipelineProgress(stageId, current, total) {
    const badge = document.getElementById(`pipeBadge_${stageId}`);
    const fill = document.getElementById(`pipeFill_${stageId}`);
    if (total > 0 && current > 0) {
        const pct = Math.round((current / total) * 100);
        if (badge) badge.textContent = `${pct}%`;
        if (fill) {
            fill.className = 'pipeline-progress-fill';
            fill.style.width = `${pct}%`;
        }
    }
}

// Track which stages we've seen as active so we can mark them done
let _pipelineSeenStages = new Set();

// One short, human-readable headline for any pipeline failure.
function _pipelineErrorMessage(extraction, message) {
    const map = {
        unknown_vendor: 'Unknown vendor — no alias matched the document.',
        no_template: 'The detected vendor has no template.',
        no_fields: 'The detected vendor’s template has no fields.',
        ocr_unavailable: 'OCR was unavailable while reading the document.',
        llm_failed: 'Vision (LLM) extraction failed — the model server was unreachable or returned an error.',
    };
    if (_extractHasPipelineFailure(extraction) && !extraction.error) {
        return map.llm_failed;
    }
    return map[extraction.error] || message || extraction.error || 'Extraction failed.';
}

function updatePipelineFromSSE(jobState) {
    const extraction = jobState.extraction || {};
    const progress = extraction.progress || {};
    const stage = progress.stage;
    const message = progress.message || '';
    const event = (jobState.event === 'done' && _extractHasPipelineFailure(extraction)) ? 'failed' : jobState.event;
    const stageOrder = ['upload', 'detect', 'normalize', 'ocr', 'llm', 'json', 'postprocess'];

    // Terminal events may omit extraction.progress.stage, especially in tests
    // and older stream payloads. Handle them before the stage guard.
    if (event === 'done') {
        stageOrder.forEach(s => {
            setPipelineStage(s, 'done', null);
        });
        if (_pipelineTimerInterval) { clearInterval(_pipelineTimerInterval); _pipelineTimerInterval = null; }
        const el = document.getElementById('pipelineTimer');
        if (el && _pipelineStartTime) {
            const elapsed = ((Date.now() - _pipelineStartTime) / 1000).toFixed(1);
            el.textContent = `${elapsed}s — COMPLETE`;
        }
        return;
    }

    // failed / partial must be caught here — before !stage — because terminal
    // events from the backend (e.g. status=unverified) often omit progress.stage.
    if (event === 'failed') {
        if (_pipelineTimerInterval) { clearInterval(_pipelineTimerInterval); _pipelineTimerInterval = null; }
        // On ANY error, hide the whole pipeline sequence — no half-finished
        // stages, no stalled orange dots — and show the error in its place.
        // The recovery actions (Resume / Retry) are rendered separately by streamJob.
        const _elapsed = _pipelineStartTime ? ((Date.now() - _pipelineStartTime) / 1000).toFixed(1) : '0.0';
        const panel = document.getElementById('pipelinePanel');
        if (panel) {
            panel.innerHTML = `
                <div class="pipeline-header">
                    <div class="pipeline-title" style="color:var(--red)">Extraction Failed</div>
                    <div class="pipeline-subtitle">Stopped after ${_elapsed}s</div>
                </div>
                <div style="display:flex;gap:12px;align-items:flex-start;padding:18px 4px 6px">
                    <div style="font-size:26px;line-height:1">⛔</div>
                    <div style="flex:1;min-width:0">
                        <div style="color:var(--red);font-weight:600;font-size:13px;margin-bottom:6px;letter-spacing:0.02em">${escapeHtml(_pipelineErrorMessage(extraction, message))}</div>
                        <div style="color:var(--text-dim);font-size:11px">See the recovery options below to resume or restart.</div>
                    </div>
                </div>`;
        }
        return;
    }

    if (event === 'partial') {
        if (_pipelineTimerInterval) { clearInterval(_pipelineTimerInterval); _pipelineTimerInterval = null; }
        const _tel = document.getElementById('pipelineTimer');
        if (_tel && _pipelineStartTime) {
            _tel.textContent = `${((Date.now() - _pipelineStartTime) / 1000).toFixed(1)}s — STOPPED`;
        }
        return;
    }

    if (!stage) return;

    // Mark upload as done always
    setPipelineStage('upload', 'done', 'Document stream received');

    // Define stage order for sequential markings
    const stageMap = { normalize: 'normalize', ocr: 'ocr', llm: 'llm', postprocess: 'postprocess' };
    const uiStage = stageMap[stage] || stage;
    const currentIdx = stageOrder.indexOf(uiStage);

    if (extraction.vendor_name) {
        setDetectedVendorDisplay(extraction.vendor_name, 'Detected from page 1');
        setPipelineStage('detect', 'done', `Detected: ${extraction.vendor_name}`);
        // Auto-detected in the worker: load the vendor's config (fields, etc.)
        // the moment detection lands, so the review panel shows the right shape.
        if (extraction.vendor_id && db.activeVendorId !== extraction.vendor_id) {
            db.activeVendorId = extraction.vendor_id;
            extLoadVendorConfig();
        }
    }

    // Mark all stages before current as done (preserve their live detail text)
    for (let i = 1; i < currentIdx; i++) {
        const s = stageOrder[i];
        const el = document.getElementById(`pipeStage_${s}`);
        if (el && !el.classList.contains('done')) {
            const detEl = document.getElementById(`pipeDetail_${s}`);
            const liveDetail = detEl ? detEl.textContent : null;
            const stageInfo = PIPELINE_STAGES.find(p => p.id === s);
            const fallback = stageInfo ? stageInfo.detail : 'Complete';
            const finalDetail = (liveDetail && liveDetail !== fallback)
                ? liveDetail
                : fallback + ' — complete';
            setPipelineStage(s, 'done', finalDetail);
        }
    }

    // Set current stage as active
    _pipelineSeenStages.add(stage);
    let detail = message || PIPELINE_STAGES.find(p => p.id === uiStage)?.detail || '';
    if (stage === 'normalize') {
        const digital = progress.digital_pages;
        const scanned = progress.scanned_pages;
        const total = progress.total_pages;
        if (Number.isFinite(digital) && Number.isFinite(scanned)) {
            if (scanned === 0) {
                detail = `${total} page(s) — all digital`;
            } else if (digital === 0) {
                detail = `${total} page(s) — all scanned`;
            } else {
                detail = `${total} page(s) — ${digital} digital, ${scanned} scanned`;
            }
        } else {
            detail = message || 'Classifying pages...';
        }
    } else if (stage === 'ocr') {
        detail = message || 'Processing page geometry';
    } else if (stage === 'llm') {
        detail = message || 'Vision extraction in progress';
    } else if (stage === 'postprocess') {
        setPipelineStage('json', 'done', 'JSON output created');
        detail = message || 'Field mapping and review memory';
    }
    setPipelineStage(uiStage, 'active', detail);

    // For LLM stage, show page progress
    if (stage === 'llm' && progress.page && progress.total_pages) {
        setPipelineProgress('llm', progress.page, progress.total_pages);
        setPipelineStage('llm', 'active', `Page ${progress.page}/${progress.total_pages}`);
    }
}

// ── EXTRACTION SSE ─────────────────────────────────────────────────────
function showStopButton(buttonId = 'extractBtn') {
    activeExtractButtonId = buttonId;
    const btn = document.getElementById(buttonId);
    if (btn) btn.style.display = 'none';
    const cancelBtn = document.getElementById('cancelBtn');
    if (cancelBtn) cancelBtn.style.display = '';
}

function resetExtractButtons() {
    activeJobId = null;
    activeExtractButtonId = 'extractBtn';
    _pipelineSeenStages = new Set();
    hidePipelinePanel();
    const cancelBtn = document.getElementById('cancelBtn');
    if (cancelBtn) cancelBtn.style.display = 'none';
    renderBottomBar();
}

async function cancelExtract() {
    if (!activeExtractionId) return;
    const btn = document.getElementById(activeExtractButtonId);
    if (btn) { btn.textContent = 'Cancelling...'; btn.disabled = true; }
    const badge = document.getElementById('rpBadge');
    if (badge) { badge.className = 'rp-badge processing'; badge.textContent = 'CANCELLING...'; }
    try {
        await apiJSON(`/jobs/extractions/${activeExtractionId}/cancel`, { method: 'POST' });
    } catch (e) { showToast('Cancel failed: ' + e.message); }
}

function showResumeButton(extractionId, lastPage, totalPg) {
    const cs = document.getElementById('conflictSection');
    if (cs) cs.style.display = 'block';
    const cm = document.getElementById('conflictMsg');
    if (cm) cm.textContent = `Stopped after page ${lastPage} of ${totalPg}. Partial results saved.`;
    const cc = document.getElementById('conflictCandidates');
    if (cc) cc.innerHTML = `
        <button class="small-btn" onclick="resumeExtract(${extractionId})" style="margin-top:8px;margin-right:6px">▶ Resume from page ${lastPage + 1}</button>
        <button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">↻ Restart</button>`;
}

function applyJobStatus(jobState) {
    const job = jobState.job || {};
    const extraction = jobState.extraction || null;
    const progress = (job.progress || (extraction && extraction.progress) || {});

    if (extraction && extraction.id) activeExtractionId = extraction.id;

    // Feed pipeline visualization
    updatePipelineFromSSE(jobState);

    if (progress.total_pages && progress.page) {
        totalPages = progress.total_pages;
    }

    if (!extraction) return;

    if (_extractHasPipelineFailure(extraction)) {
        const failedExtractionId = extraction.id || activeExtractionId;
        setStatus('failed');
        document.getElementById('rpBadge').className = 'rp-badge review';
        document.getElementById('rpBadge').textContent = extraction.error === 'llm_failed' || (extraction.result && extraction.result._all_pages_failed) ? 'LLM FAILED' : 'FAILED';
        const cs = document.getElementById('conflictSection');
        const cm = document.getElementById('conflictMsg');
        const cc = document.getElementById('conflictCandidates');
        if (cs) cs.style.display = 'block';
        if (cm) cm.textContent = 'LLM extraction failed. Restart from scratch after the model server is running.';
        if (cc) cc.innerHTML = failedExtractionId
            ? `<button class="small-btn" onclick="resumeExtract(${failedExtractionId})" style="margin-top:8px;margin-right:6px">▶ Resume Pipeline</button>
               <button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">↻ Restart from Scratch</button>`
            : `<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">↻ Restart from Scratch</button>`;
        return;
    }

    if (extraction.status === 'done') {
        const reviewDisabled = progress.review_available === false || progress.warning_code === 'ocr_failed_review_unavailable';
        lastResult = extraction.corrected_result || extraction.result;
        totalPages = extraction.total_pages || totalPages;
        showResult(lastResult);
        setStatus('optimal');
        document.getElementById('rpBadge').className = 'rp-badge optimal';
        document.getElementById('rpBadge').textContent = 'OPTIMAL';
        reviewResult = lastResult;
        reviewExtractionId = extraction.id;
        reviewFieldLocations = extraction.field_locations || {};
        if (extraction.id) loadExtractionPages(extraction.id);
        if (extraction.id && !reviewDisabled) {
            const rs = document.getElementById('resultSection');
            if (rs && !rs.querySelector('.review-link-btn')) {
                const btn = document.createElement('button');
                btn.className = 'small-btn review-link-btn';
                btn.style.cssText = 'margin-top:8px;width:100%;background:var(--blue-bg);border-color:var(--blue);color:var(--blue)';
                btn.textContent = 'Open Review';
                btn.onclick = () => navigate(`#/review/${extraction.id}`);
                rs.appendChild(btn);
            }
        }
        if (reviewDisabled) {
            const cs = document.getElementById('conflictSection');
            const cm = document.getElementById('conflictMsg');
            const cc = document.getElementById('conflictCandidates');
            if (cs) cs.style.display = 'block';
            if (cm) cm.textContent = 'OCR failed, so review, corrections, bounding boxes, and spatial memory are unavailable. JSON extraction completed.';
            if (cc) cc.innerHTML = '';
        }
    } else if (extraction.status === 'partial' || extraction.status === 'cancelled') {
        if (extraction.result) {
            lastResult = extraction.result;
            showResult(extraction.result);
        }
        setStatus('optimal');
        document.getElementById('rpBadge').className = 'rp-badge review';
        document.getElementById('rpBadge').textContent = 'PARTIAL';
        showResumeButton(extraction.id, progress.last_completed_page || 0, extraction.total_pages || totalPages);
    }
}

// ── SSE-based job streaming (replaces polling) ─────────────────────────
// One persistent HTTP connection instead of 50+ requests/minute.
// The server pushes progress events; heavy fields (result, ocr_data)
// are only sent in the final terminal event.

async function streamJob(jobId) {
    // Abort any previous stream
    if (_activeStreamAbort) { _activeStreamAbort.abort(); _activeStreamAbort = null; }
    const controller = new AbortController();
    _activeStreamAbort = controller;
    activeJobId = jobId;

    const token = localStorage.getItem('auth_token');
    const headers = token ? { 'Authorization': `Bearer ${token}` } : {};
    const response = await fetch(`${API}/jobs/${jobId}/stream`, { signal: controller.signal, headers });
    if (response.status === 401) {
        localStorage.removeItem('auth_token');
        localStorage.removeItem('auth_user');
        window.location.hash = '#/login';
        throw new Error('HTTP 401: not authenticated');
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed.startsWith('data: ')) continue;

                try {
                    const event = JSON.parse(trimmed.slice(6));

                    // Feed to existing UI handler (reads job.progress, extraction.status, etc.)
                    applyJobStatus(event);

                    // Terminal events — handle completion/failure and close stream
                    if (event.event === 'done' || event.event === 'failed' || event.event === 'partial') {
                        if (event.event === 'failed') {
                            const extraction = event.extraction || {};
                            const cs2 = document.getElementById('conflictSection'); if (cs2) cs2.style.display = 'block';
                            const cm = document.getElementById('conflictMsg');
                            const cc = document.getElementById('conflictCandidates');
                            const _vendorConfigErrors = ['unknown_vendor', 'no_template', 'no_fields'];
                            if (_vendorConfigErrors.includes(extraction.error)) {
                                // Detection (now in the worker) found no usable vendor.
                                // Resuming won't help — send the user to configure it.
                                const _msg = {
                                    unknown_vendor: 'Unknown vendor — no alias matched the document. Add the vendor name as an alias, then retry.',
                                    no_template: 'The detected vendor has no template. Create a template with at least one field, then retry.',
                                    no_fields: 'The detected vendor’s template has no fields. Add at least one field, then retry.',
                                };
                                if (cm) cm.textContent = _msg[extraction.error];
                                if (cc) cc.innerHTML = `<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px;margin-right:6px">↻ Retry Extraction</button>
                                   <button class="small-btn" onclick="navigate('#/vendors')" style="margin-top:8px">Manage Vendors</button>`;
                            } else if (extraction.error === 'ocr_unavailable') {
                                // Transient infrastructure problem — just let them retry.
                                if (cm) cm.textContent = 'OCR was unavailable while reading the document for vendor detection. Please retry.';
                                if (cc) cc.innerHTML = `<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">↻ Retry Extraction</button>`;
                            } else if (extraction.error === 'llm_failed') {
                                const failedExtractionId = (event.extraction && event.extraction.id) || activeExtractionId;
                                const p = extraction.progress || {};
                                const failedPages = Array.isArray(p.failed_pages) && p.failed_pages.length
                                    ? ` Failed page(s): ${p.failed_pages.join(', ')}.`
                                    : '';
                                if (cm) cm.textContent = `LLM extraction failed.${failedPages} Resume to retry missing pages.`;
                                if (cc) cc.innerHTML = failedExtractionId
                                    ? `<button class="small-btn" onclick="resumeExtract(${failedExtractionId})" style="margin-top:8px;margin-right:6px">▶ Resume Pipeline</button>
                                       <button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">↻ Restart from Scratch</button>`
                                    : `<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">↻ Restart from Scratch</button>`;
                            } else {
                                const message = extraction.error || (event.job && event.job.error) || 'Extraction failed';
                                if (cm) cm.textContent = 'Extraction failed: ' + message;
                                const failedExtractionId = (event.extraction && event.extraction.id) || activeExtractionId;
                                if (cc) cc.innerHTML = failedExtractionId
                                    ? `<button class="small-btn" onclick="resumeExtract(${failedExtractionId})" style="margin-top:8px;margin-right:6px">▶ Resume Pipeline</button>
                                       <button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">↻ Restart from Scratch</button>`
                                    : `<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">↻ Restart from Scratch</button>`;
                            }
                            setStatus('failed');
                            document.getElementById('rpBadge').className = 'rp-badge review';
                            document.getElementById('rpBadge').textContent = extraction.error === 'llm_failed' ? 'LLM FAILED' : 'FAILED';
                        }
                        activeJobId = null;
                        return event;
                    }

                    if (event.event === 'error') {
                        throw new Error(event.error || 'Stream error');
                    }
                } catch (pe) {
                    if (pe.message.includes('Stream error') || pe.message.startsWith('HTTP')) throw pe;
                }
            }
        }
    } finally {
        _activeStreamAbort = null;
    }
}

async function runExtract() {
    if (!loadedFile) return;
    const user = getAuthUser();
    if (user && user.role === 'admin' && !actAsClientId) {
        showToast('Please select a client context to enable extraction.');
        return;
    }

    const v = db.vendors.find(v => v.id === db.activeVendorId);

    setStatus('processing');
    document.getElementById('rpBadge').className = 'rp-badge processing';
    document.getElementById('rpBadge').textContent = 'PROCESSING';
    const btn = document.getElementById('extractBtn');
    if (btn) {
        btn.className = 'extract-btn processing';
        btn.textContent = 'Extracting...';
        btn.disabled = true;
    }
    const cs = document.getElementById('conflictSection'); if (cs) cs.style.display = 'none';
    const rs = document.getElementById('resultSection'); if (rs) rs.style.display = 'none';

    if (v) {
        setDetectedVendorDisplay(v.name, 'Manual vendor selected. Auto-detection will be skipped.');
    } else {
        setDetectedVendorDisplay(null, 'Detecting vendor from page 1...');
    }
    showStopButton('extractBtn');
    activeJobId = 'pending';  // sentinel: prevents renderBottomBar() from re-showing Extract button
    showPipelinePanel({ selectedVendorName: v ? v.name : null });
    const formData = new FormData();
    formData.append('file', loadedFile);
    if (v) formData.append('vendor_id', v.id);
    // Pass the selected client scope so the backend restricts vendor detection
    // to that client's aliases/templates only (prevents cross-tenant collisions).
    if (user && user.role === 'admin' && actAsClientId && actAsClientId !== 'ADMIN') {
        formData.append('act_as_client_id', actAsClientId);
    }

    try {
        const payload = await apiJSON('/ingest/ui', { method: 'POST', body: formData });
        // Vendor detection now happens in the worker, not in this response.
        // For auto-detect the vendor arrives over the SSE stream (handled in
        // updatePipelineFromSSE); show "detecting…" until then.
        if (v) {
            setDetectedVendorDisplay(v.name, 'Manual vendor selected. Auto-detection skipped.');
            setPipelineStage('detect', 'done', `Manual vendor selected: ${v.name}. Auto-detection skipped.`);
        } else {
            setPipelineStage('detect', 'active', 'Detecting vendor from page 1...');
        }
        activeExtractionId = payload.extraction_id;
        await streamJob(payload.job_id);
    } catch (err) {
        let errorMsg = 'Extraction failed: ' + err.message;
        let failedStage = 'detect'; // default: failed during vendor detection
        let actionButtons = '<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">Retry Extraction</button>';

        const apiDetail = err.error || {};
        if (err.status === 402 && apiDetail.code === 'QUOTA_EXCEEDED') {
            const limit = apiDetail.subscription_limit;
            const used = apiDetail.total_extracted_pages;
            const over = Math.abs(apiDetail.overage || 0);
            errorMsg = `Page limit exceeded — ${used} pages used, limit is ${limit} (${over} over). Contact your administrator to increase your limit.`;
            failedStage = 'upload';
        } else if (err.status === 409 && apiDetail.reason === 'unknown_vendor') {
            errorMsg = 'Unknown vendor — no alias matched the document. Add the vendor name as an alias, then retry.';
            failedStage = 'detect';
            actionButtons = '<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">Retry Extraction</button> <button class="small-btn" onclick="navigate(\'#/vendors\')" style="margin-top:8px">Manage Vendors</button>';
        }

        try {
            const match402 = err.message.match(/HTTP 402:\s*(.+)/s);
            if (match402) {
                const parsed = JSON.parse(match402[1]);
                const detail = parsed.detail || parsed;
                if (detail.code === 'QUOTA_EXCEEDED') {
                    const limit = detail.subscription_limit;
                    const used = detail.total_extracted_pages;
                    const over = Math.abs(detail.overage);
                    errorMsg = `Page limit exceeded — ${used} pages used, limit is ${limit} (${over} over). Contact your administrator to increase your limit.`;
                    failedStage = 'upload';
                }
            }
            const match409 = err.message.match(/HTTP 409:\s*(.+)/s);
            if (match409) {
                const parsed = JSON.parse(match409[1]);
                const detail = parsed.detail || parsed;
                if (detail.reason === 'unknown_vendor') {
                    errorMsg = 'Unknown vendor — no alias matched the document. Add the vendor name as an alias, then retry.';
                    failedStage = 'detect';
                    actionButtons = '<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">Retry Extraction</button> <button class="small-btn" onclick="navigate(\'#/vendors\')" style="margin-top:8px">Manage Vendors</button>';
                }
            }
        } catch (_) { }

        // Stop the spinning timer, mark the failed stage, and reset all later stages to blank
        if (_pipelineTimerInterval) { clearInterval(_pipelineTimerInterval); _pipelineTimerInterval = null; }
        setPipelineStage(failedStage, 'failed', errorMsg);
        const _stageOrder = PIPELINE_STAGES.map(s => s.id);
        const _failedIdx  = _stageOrder.indexOf(failedStage);
        _stageOrder.slice(_failedIdx + 1).forEach(sid => {
            const info = PIPELINE_STAGES.find(p => p.id === sid);
            setPipelineStage(sid, '', info ? info.detail : '');
        });

        const cs2 = document.getElementById('conflictSection'); if (cs2) cs2.style.display = 'block';
        const cm = document.getElementById('conflictMsg');
        if (cm) {
            cm.style.cssText = 'font-size:11px;line-height:1.6;color:var(--text);margin-bottom:8px;font-family:var(--mono)';
            cm.textContent = errorMsg;
        }
        const cc = document.getElementById('conflictCandidates'); if (cc) cc.innerHTML = actionButtons;
        setStatus('optimal');
        document.getElementById('rpBadge').className = 'rp-badge review';
        document.getElementById('rpBadge').textContent = 'NEEDS REVIEW';
    } finally {
        resetExtractButtons();
    }
}



async function resumeExtract(extractionId) {
    setStatus('processing');
    document.getElementById('rpBadge').className = 'rp-badge processing';
    document.getElementById('rpBadge').textContent = 'RESUMING';
    const cs = document.getElementById('conflictSection'); if (cs) cs.style.display = 'none';
    showStopButton(activeExtractButtonId || 'extractBtn');
    activeExtractionId = extractionId;

    try {
        const payload = await apiJSON(`/jobs/extractions/${extractionId}/resume`, { method: 'POST' });
        activeExtractionId = payload.extraction_id;
        await streamJob(payload.job_id);
    } catch (err) {
        if (err.message.includes('HTTP 409') && err.message.includes('draining')) {
            const cs2 = document.getElementById('conflictSection'); if (cs2) cs2.style.display = 'block';
            const cm = document.getElementById('conflictMsg'); if (cm) cm.textContent = 'Prior jobs still shutting down — retrying...';
            const cc = document.getElementById('conflictCandidates');
            if (cc) cc.innerHTML = `<button class="small-btn" disabled style="margin-top:8px;opacity:0.6">Shutting down prior jobs...</button>`;
            setTimeout(() => resumeExtract(extractionId), 2000);
            return;
        }
        const cs2 = document.getElementById('conflictSection'); if (cs2) cs2.style.display = 'block';
        const cm = document.getElementById('conflictMsg'); if (cm) cm.textContent = 'Resume failed: ' + err.message;
        const cc = document.getElementById('conflictCandidates'); if (cc) cc.innerHTML = `<button class="small-btn" onclick="resumeExtract(${extractionId})" style="margin-top:8px">▶ Retry Resume</button>`;
        setStatus('optimal');
        document.getElementById('rpBadge').className = 'rp-badge review';
        document.getElementById('rpBadge').textContent = 'NEEDS REVIEW';
    } finally {
        resetExtractButtons();
    }
}

function retryLastExtract() {
    const cs = document.getElementById('conflictSection'); if (cs) cs.style.display = 'none';
    const cm = document.getElementById('conflictMsg'); if (cm) cm.textContent = '';
    const cc = document.getElementById('conflictCandidates'); if (cc) cc.innerHTML = '';
    resetPipelinePanelMarkup();
    return runExtract();
}

function showResult(data) {
    const rs = document.getElementById('resultSection'); if (rs) rs.style.display = 'block';
    const rb = document.getElementById('resultBlock'); if (!rb) return;
    // A failure sentinel ({_all_pages_failed, errors}) is shown as a clean
    // message, not dumped as raw JSON.
    const errInfo = parseResultErrors(data);
    if (errInfo) { renderResultErrorBlock(rb, errInfo); return; }
    rb.textContent = JSON.stringify(data, null, 2);
}

async function loadExtractionPages(extractionId) {
    try {
        const pages = await apiJSON(`/extractions/${extractionId}/pages`);
        if (!pages || pages.length === 0) return;
        extractionPages = pages; totalPages = pages.length; currentPage = 1;
        updatePageNav(); renderCurrentPage();
    } catch (e) { console.warn('Failed loading pages:', e.message); }
}

function copyResult() {
    if (!lastResult) return;
    navigator.clipboard.writeText(JSON.stringify(lastResult, null, 2));
    showToast('Copied to clipboard');
}

function downloadResult() {
    if (!lastResult) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(lastResult, null, 2)], { type: 'application/json' }));
    a.download = `extraction_${Date.now()}.json`; a.click();
}

function setStatus(state) {
    const dot = document.getElementById('hdrDot');
    const lbl = document.getElementById('hdrStatus');
    if (!dot || !lbl) return;
    if (state === 'processing') { dot.className = 'status-dot processing'; lbl.className = 'status-label processing'; lbl.innerHTML = 'System Status: <span>PROCESSING</span>'; }
    else if (state === 'failed') { dot.className = 'status-dot failed'; lbl.className = 'status-label failed'; lbl.innerHTML = 'System Status: <span>FAILED</span>'; }
    else { dot.className = 'status-dot'; lbl.className = 'status-label'; lbl.innerHTML = 'System Status: <span>OPTIMAL</span>'; }
}
