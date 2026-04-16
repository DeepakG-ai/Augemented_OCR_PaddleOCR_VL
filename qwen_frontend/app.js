/* ── Augmented OCR — SPA Application ────────────────────────────────── */

const API = (
    window.__AUGMENTED_OCR_API__
    || document.querySelector('meta[name="api-base"]')?.content
    || ''
).replace(/\/$/, '');

// ── STATE ──────────────────────────────────────────────────────────────
let db = { vendors: [], activeVendorId: null };
let headerFields = [];
let lineItemFields = [];
let extractionRules = [];
let loadedFile = null;
let currentPage = 1;
let totalPages = 1;
let lastResult = null;
let extractionPages = [];
let zoomLevel = 100;
let isDragging = false;
let dragStartX = 0, dragStartY = 0, scrollStartX = 0, scrollStartY = 0;
let activeExtractionId = null;
let activeJobId = null;
let activeExtractButtonId = 'extractBtn';
let lastExtractionMode = 'fields';

// ── REVIEW STATE ──────────────────────────────────────────────────────
let reviewFieldLocations = {};   // {fieldName: {page, box, matched_text, score, strategy}}
let reviewExtractionId = null;
let reviewResult = null;
let reviewPages = [];
let activeMapField = null;       // currently hovered/selected field name

// ── THEME ──────────────────────────────────────────────────────────────
function toggleTheme() {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    if (isLight) {
        document.documentElement.removeAttribute('data-theme');
        localStorage.setItem('theme', 'dark');
        document.getElementById('themeToggleBtn').textContent = 'LIGHT';
    } else {
        document.documentElement.setAttribute('data-theme', 'light');
        localStorage.setItem('theme', 'light');
        document.getElementById('themeToggleBtn').textContent = 'DARK';
    }
}
if (localStorage.getItem('theme') === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
}

// ── API HELPERS ────────────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
    const res = await fetch(`${API}${path}`, opts);
    if (!res.ok) { const b = await res.text(); throw new Error(`HTTP ${res.status}: ${b}`); }
    return res;
}
async function apiJSON(path, opts = {}) { return (await apiFetch(path, opts)).json(); }

function formatDurationMs(durationMs) {
    return durationMs ? `${(durationMs / 1000).toFixed(1)}s` : '';
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value ?? '';
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escapeJsString(value) {
    return String(value ?? '')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/\r/g, '\\r')
        .replace(/\n/g, '\\n')
        .replace(/</g, '\\x3C')
        .replace(/>/g, '\\x3E');
}

function escapeInlineJsString(value) {
    return escapeHtml(escapeJsString(value));
}

function safeClassToken(value) {
    return String(value ?? '').replace(/[^a-zA-Z0-9_-]/g, '-');
}

function safeMimeType(value) {
    const mime = String(value ?? '').trim();
    return /^[a-zA-Z0-9.+-]+\/[a-zA-Z0-9.+-]+$/.test(mime) ? mime : 'application/octet-stream';
}

// ── TOAST ──────────────────────────────────────────────────────────────
function showToast(msg) {
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:70px;right:20px;background:var(--bg2);border:1px solid var(--blue-dim);color:var(--blue);padding:8px 14px;border-radius:3px;font-size:11px;letter-spacing:0.08em;z-index:999;transition:opacity 0.3s;';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 2000);
}

// ── ROUTER ─────────────────────────────────────────────────────────────
function navigate(hash) {
    window.location.hash = hash;
}

function getRoute() {
    const h = window.location.hash.slice(1) || '/';
    return h;
}

async function router() {
    const route = getRoute();
    const app = document.getElementById('appRoot');

    // Clean up review SVG overlay when leaving the review page
    if (!route.startsWith('/review/')) {
        const svg = document.getElementById('rvMappingSvg');
        if (svg) svg.remove();
    }

    // Abort active SSE stream when leaving the extract page
    if (!route.startsWith('/extract') && _activeStreamAbort) {
        _activeStreamAbort.abort();
        _activeStreamAbort = null;
        activeJobId = null;
    }

    // Update nav active state
    document.querySelectorAll('.nav-tab').forEach(t => {
        t.classList.remove('active');
        if (t.dataset.route && route.startsWith(t.dataset.route)) t.classList.add('active');
    });

    if (route === '/' || route === '/vendors') {
        app.className = 'app';
        await renderVendorsPage(app);
    } else if (route.startsWith('/template/')) {
        app.className = 'app';
        const vendorId = route.split('/template/')[1];
        await renderTemplatePage(app, vendorId);
    } else if (route === '/saved-templates') {
        app.className = 'app';
        await renderSavedTemplatesPage(app);
    } else if (route === '/extract') {
        app.className = 'app extract-layout';
        await renderExtractPage(app);
    } else if (route === '/history') {
        app.className = 'app';
        await renderHistoryPage(app);
    } else if (route === '/review') {
        // Bare /review (no ID) — redirect to last extraction's review
        if (reviewExtractionId) {
            window.location.hash = `#/review/${reviewExtractionId}`;
            return;
        }
        // Try to fetch most recent extraction
        try {
            const exts = await apiJSON('/extractions?limit=1');
            if (exts.length) {
                window.location.hash = `#/review/${exts[0].id}`;
                return;
            }
        } catch (e) { }
        app.className = 'app';
        app.innerHTML = headerHTML() + `
            <div class="page-content" style="text-align:center;padding-top:60px">
                <div style="font-size:48px;margin-bottom:16px">📋</div>
                <div class="page-title">No Extraction to Review</div>
                <p style="color:var(--text-dim);margin:12px 0">Run an extraction first, then come here to review the field mapping.</p>
                <button class="small-btn" onclick="navigate('#/extract')" style="margin-top:12px">Go to Extraction</button>
            </div>`;
        updateNavActive();
    } else if (route.startsWith('/review/')) {
        app.className = 'app review-layout';
        const extractionId = route.split('/review/')[1];
        await renderReviewPage(app, extractionId);
    } else {
        app.className = 'app';
        await renderVendorsPage(app);
    }
}

window.addEventListener('hashchange', router);

// ── HEADER HTML ────────────────────────────────────────────────────────
function headerHTML() {
    const themeLabel = localStorage.getItem('theme') === 'light' ? 'DARK' : 'LIGHT';
    return `
    <header class="header">
        <div class="logo" onclick="navigate('#/')">Augmented <span>OCR</span></div>
        <nav class="nav-tabs">
            <a class="nav-tab" data-route="/vendors" href="#/vendors">Vendors</a>
            <a class="nav-tab" data-route="/saved-templates" href="#/saved-templates">Templates</a>
            <a class="nav-tab" data-route="/extract" href="#/extract">Extraction</a>
            <a class="nav-tab" data-route="/history" href="#/history">History</a>
            <a class="nav-tab" data-route="/review" href="#/review">Review</a>
        </nav>
        <div class="header-right">
            <button class="theme-toggle-btn" id="themeToggleBtn" onclick="toggleTheme()">${themeLabel}</button>
            <div style="display:flex;align-items:center;gap:6px">
                <div class="status-dot" id="hdrDot"></div>
                <span class="status-label" id="hdrStatus">System Status: <span>OPTIMAL</span></span>
            </div>
        </div>
    </header>`;
}

// ══════════════════════════════════════════════════════════════════════
// PAGE 1: VENDORS
// ══════════════════════════════════════════════════════════════════════
async function renderVendorsPage(app) {
    let vendors = [];
    try { vendors = await apiJSON('/vendors'); } catch (e) { console.warn(e); }
    db.vendors = vendors;

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">⚡ Active Vendors</div>
        <div id="vendorCards"></div>
        <button class="add-vendor-btn" style="max-width:300px;margin-top:12px" onclick="openAddVendor()">+ Add New Vendor</button>
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${vendors.length} VENDOR${vendors.length !== 1 ? 'S' : ''} REGISTERED</span>
    </div>` + vendorModalHTML();

    renderVendorCards(vendors);
    updateNavActive();
}

function renderVendorCards(vendors) {
    const c = document.getElementById('vendorCards');
    if (!c) return;
    if (!vendors.length) {
        c.innerHTML = '<div style="color:var(--text-dim);padding:20px">No vendors yet. Add one to get started.</div>';
        return;
    }
    c.innerHTML = vendors.map(v => `
        <div class="vendor-card">
            <div class="vendor-card-info" onclick="navigate('#/template/${escapeInlineJsString(v.id)}')">
                <div class="vendor-card-name">${escapeHtml(v.name)}</div>
                <div class="vendor-card-id">ID: ${escapeHtml(v.id)} · Created: ${new Date(v.created_at).toLocaleDateString()}</div>
            </div>
            <div class="vendor-card-actions">
                <a class="link-btn" href="#/template/${escapeHtml(v.id)}">⚙ Template</a>
                <a class="link-btn" href="#/extract" onclick="localStorage.setItem('extractVendor','${escapeInlineJsString(v.id)}')">▶ Extract</a>
                <button class="del-btn" onclick="event.stopPropagation();deleteVendor('${escapeInlineJsString(v.id)}','${escapeInlineJsString(v.name)}')">✕ Delete</button>
            </div>
        </div>
    `).join('');
}

async function deleteVendor(id, name) {
    if (!confirm(`Delete vendor "${name}" and all its data?`)) return;
    try {
        await apiFetch(`/vendors/${id}`, { method: 'DELETE' });
        showToast(`Vendor ${name} deleted`);
        router();
    } catch (e) { showToast('Delete failed: ' + e.message); }
}

function vendorModalHTML() {
    return `
    <div class="modal-overlay" id="vendorModal">
        <div class="modal">
            <div class="modal-title">New Vendor</div>
            <div class="modal-field">
                <label class="modal-label">Vendor Name</label>
                <input class="modal-input" id="newVendorName" placeholder="e.g. Robert Scott">
            </div>
            <div class="modal-field">
                <label class="modal-label">Vendor ID</label>
                <input class="modal-input" id="newVendorId" placeholder="e.g. RS001" style="text-transform:uppercase">
            </div>
            <div class="modal-actions">
                <button class="modal-btn secondary" onclick="closeModal('vendorModal')">Cancel</button>
                <button class="modal-btn primary" onclick="saveNewVendor()">Create Vendor</button>
            </div>
        </div>
    </div>`;
}

function openAddVendor() { document.getElementById('vendorModal').classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

async function saveNewVendor() {
    const name = document.getElementById('newVendorName').value.trim().toUpperCase();
    const id = document.getElementById('newVendorId').value.trim().toUpperCase() || Math.random().toString(36).slice(2, 10).toUpperCase();
    if (!name) return;
    try {
        await apiJSON('/vendors', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, name }) });
        showToast(`Vendor ${name} created`);
    } catch (e) { showToast('Failed: ' + e.message); }
    closeModal('vendorModal');
    router();
}

// ══════════════════════════════════════════════════════════════════════
// PAGE 2: TEMPLATE
// ══════════════════════════════════════════════════════════════════════
let tplVendorId = null;
let tplVendorName = '';

async function renderTemplatePage(app, vendorId) {
    tplVendorId = vendorId;
    let vendor = null, tmpl = null;
    try { const vendors = await apiJSON('/vendors'); vendor = vendors.find(v => v.id === vendorId); } catch (e) { }
    tplVendorName = vendor ? vendor.name : vendorId;
    try { tmpl = await apiJSON(`/vendors/${vendorId}/template`); } catch (e) { }

    headerFields = tmpl ? [...(tmpl.header_fields || [])] : [];
    lineItemFields = tmpl ? [...(tmpl.line_item_fields || [])] : [];
    extractionRules = tmpl ? [...(tmpl.extraction_rules || [])] : [];
    const fmt = tmpl ? tmpl.format_type : 'single_po_multipage';
    const instructions = tmpl ? (tmpl.prompt_instructions || '') : '';
    const hash = tmpl ? (tmpl.prompt_hash || '') : '';

    window.tplSysPrompt = tmpl && tmpl.system_prompt ? tmpl.system_prompt : 'No system prompt generated yet. Run an extraction.';
    window.tplUsrPrompt = tmpl && tmpl.user_prompt ? tmpl.user_prompt : 'No user message available.';
    window.tplShowPrompt = function (type) {
        const el = document.getElementById('tplPromptPreview');
        if (!el) return;
        el.value = type === 'system' ? window.tplSysPrompt : window.tplUsrPrompt;
        document.getElementById('btnSysPrompt').style = type === 'system' ? 'background:var(--blue-bg);color:var(--blue);border-color:var(--blue)' : '';
        document.getElementById('btnUsrPrompt').style = type === 'user' ? 'background:var(--blue-bg);color:var(--blue);border-color:var(--blue)' : '';
        document.getElementById('tplPromptDesc').textContent = type === 'system' ?
            'This is the exact system prompt sent to Qwen3-VL.' :
            'This is the dynamic user message sent for page 1.';
    };

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">⚙ Template — ${tplVendorName}</div>
        ${hash ? `<div style="font-size:9px;color:var(--text-dim);margin-bottom:12px">PROMPT HASH: ${hash.slice(0, 16)}...</div>` : ''}
        <div class="tpl-grid">
            <div class="tpl-panel">
                <div class="tpl-panel-title">Format Type</div>
                <select class="format-select" id="tplFormat" onchange="updateTplFormatHint()">
                    <option value="single_po_multipage" ${fmt === 'single_po_multipage' ? 'selected' : ''}>Single PO — multi-page (header on pg 1)</option>
                    <option value="po_per_page" ${fmt === 'po_per_page' ? 'selected' : ''}>Different PO per page</option>
                    <option value="single_page" ${fmt === 'single_page' ? 'selected' : ''}>Single page document</option>
                </select>
                <div id="tplFormatHint" style="font-size:9px;color:var(--text-dim);line-height:1.5;margin-bottom:12px"></div>
                <div class="tpl-panel-title">Header Fields</div>
                <div class="add-rule-row"><input class="add-rule-input" id="tplHeaderInput" placeholder="e.g. supplier, po_number" onkeydown="if(event.key==='Enter')tplAddHeader()"><button class="small-btn" onclick="tplAddHeader()">+ Add</button></div>
                <div id="tplHeaderList" class="rules-list" style="margin-top:4px"></div>
                <div style="margin-top:12px" class="tpl-panel-title">Line Item Columns</div>
                <div class="add-rule-row"><input class="add-rule-input" id="tplLineInput" placeholder="e.g. no, description, qty" onkeydown="if(event.key==='Enter')tplAddLine()"><button class="small-btn" onclick="tplAddLine()">+ Add</button></div>
                <div id="tplLineList" class="rules-list" style="margin-top:4px"></div>
            </div>
            <div class="tpl-panel">
                <div class="tpl-panel-title">Prompt Instructions</div>
                <textarea class="prompt-area" id="tplPrompt" style="min-height:120px" placeholder="e.g. Supplier address is always in the top-left block. PO number starts with PO and is 5 digits...">${instructions}</textarea>
                <div style="margin-top:12px" class="tpl-panel-title">Extraction Rules</div>
                <div id="tplRulesList" class="rules-list"></div>
                <div class="add-rule-row"><input class="add-rule-input" id="tplRuleInput" placeholder="Add extraction rule..." onkeydown="if(event.key==='Enter')tplAddRule()"><button class="small-btn" onclick="tplAddRule()">+ Add</button></div>
                <div class="rule-hint">Examples — click to add:
                    <span class="rule-example" onclick="tplAddRuleText('Merge line items across pages')">Merge line items across pages</span>
                    <span class="rule-example" onclick="tplAddRuleText('Numbers must be numeric, not strings')">Numbers must be numeric</span>
                    <span class="rule-example" onclick="tplAddRuleText('Dates in DD/MM/YYYY format')">Dates DD/MM/YYYY</span>
                    <span class="rule-example" onclick="tplAddRuleText('Skip rows with empty description')">Skip empty rows</span>
        </div>
        <div class="tpl-panel" style="margin-top:16px; grid-column: 1 / -1;">
            <div style="display:flex; gap:8px; margin-bottom:8px; align-items:center;">
                <div class="tpl-panel-title" style="margin-bottom:0">Prompt Previews</div>
                <div style="flex:1"></div>
                <button id="btnSysPrompt" class="small-btn" onclick="tplShowPrompt('system')">System Prompt</button>
                <button id="btnUsrPrompt" class="small-btn" onclick="tplShowPrompt('user')">User Message (Page 1)</button>
            </div>
            <textarea id="tplPromptPreview" class="prompt-area" style="min-height:400px;font-family:monospace;font-size:11px;background:var(--bg);color:var(--text);border:1px solid var(--border);" readonly></textarea>
            <div id="tplPromptDesc" style="font-size:10px;color:var(--text-dim);margin-top:8px">This is the exact system prompt sent to Qwen3-VL.</div>
        </div>
        <button class="tpl-save-btn" style="margin-top:16px" onclick="saveTplConfig()">💾 Save Template</button>
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">VENDOR: ${tplVendorName} · ${headerFields.length} HEADER FIELDS · ${lineItemFields.length} LINE COLUMNS · ${extractionRules.length} RULES</span>
    </div>`;

    tplRenderHeaders(); tplRenderLines(); tplRenderRules(); updateTplFormatHint(); updateNavActive();
    tplShowPrompt('system');
}

function tplAddHeader() {
    const inp = document.getElementById('tplHeaderInput');
    const val = inp.value.trim().toLowerCase().replace(/\s+/g, '_');
    if (!val || headerFields.includes(val)) return;
    headerFields.push(val); inp.value = ''; tplRenderHeaders();
}
function tplRemoveHeader(i) { headerFields.splice(i, 1); tplRenderHeaders(); }
function tplRenderHeaders() {
    const el = document.getElementById('tplHeaderList');
    if (!el) return;
    el.innerHTML = headerFields.length ? headerFields.map((f, i) => `<div class="rule-item"><div class="rule-dot" style="background:var(--blue)"></div><span style="flex:1;text-transform:uppercase;letter-spacing:0.08em">${escapeHtml(f)}</span><button class="rule-del" onclick="tplRemoveHeader(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim)">No header fields</div>';
}

function tplAddLine() {
    const inp = document.getElementById('tplLineInput');
    const val = inp.value.trim().toLowerCase().replace(/\s+/g, '_');
    if (!val || lineItemFields.includes(val)) return;
    lineItemFields.push(val); inp.value = ''; tplRenderLines();
}
function tplRemoveLine(i) { lineItemFields.splice(i, 1); tplRenderLines(); }
function tplRenderLines() {
    const el = document.getElementById('tplLineList');
    if (!el) return;
    el.innerHTML = lineItemFields.length ? lineItemFields.map((f, i) => `<div class="rule-item"><div class="rule-dot" style="background:var(--green)"></div><span style="flex:1;text-transform:uppercase;letter-spacing:0.08em">${escapeHtml(f)}</span><button class="rule-del" onclick="tplRemoveLine(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim)">No line item columns</div>';
}

function tplAddRule() {
    const inp = document.getElementById('tplRuleInput');
    const val = inp.value.trim(); if (!val) return;
    extractionRules.push(val); inp.value = ''; tplRenderRules();
}
function tplAddRuleText(text) { extractionRules.push(text); tplRenderRules(); }
function tplRemoveRule(i) { extractionRules.splice(i, 1); tplRenderRules(); }
function tplRenderRules() {
    const el = document.getElementById('tplRulesList');
    if (!el) return;
    el.innerHTML = extractionRules.length ? extractionRules.map((r, i) => `<div class="rule-item"><div class="rule-dot"></div><span style="flex:1">${escapeHtml(r)}</span><button class="rule-del" onclick="tplRemoveRule(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim)">No rules yet</div>';
}

function updateTplFormatHint() {
    const hints = {
        single_po_multipage: 'Page 1: header + line items. Pages 2-N: line items only, same PO.',
        po_per_page: 'Each page is a self-contained PO with its own header and line items.',
        single_page: 'Entire document is a single page. Extract all fields at once.',
    };
    const el = document.getElementById('tplFormatHint');
    if (el) el.textContent = hints[document.getElementById('tplFormat').value] || '';
}

async function saveTplConfig() {
    const payload = {
        format_type: document.getElementById('tplFormat').value,
        vendor_name: tplVendorName,
        header_fields: headerFields,
        line_item_fields: lineItemFields,
        prompt_instructions: document.getElementById('tplPrompt').value || null,
        extraction_rules: extractionRules,
    };
    try {
        const resp = await apiJSON(`/vendors/${tplVendorId}/template`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        showToast(`Template saved — hash: ${(resp.prompt_hash || 'none').slice(0, 12)}...`);
    } catch (e) { showToast('Save failed: ' + e.message); }
}

// ══════════════════════════════════════════════════════════════════════
// PAGE 3: SAVED TEMPLATES
// ══════════════════════════════════════════════════════════════════════
async function renderSavedTemplatesPage(app) {
    let templates = [];
    try { templates = await apiJSON('/templates'); } catch (e) { console.warn(e); }

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">📋 Saved Templates</div>
        ${templates.length === 0 ? '<div style="color:var(--text-dim);padding:20px">No templates saved yet. Create one from a vendor page.</div>' : `
        <table class="tpl-table">
            <thead><tr>
                <th>Vendor</th><th>ID</th><th>Format</th><th>Header Fields</th><th>Line Items</th><th>Rules</th><th>Actions</th>
            </tr></thead>
            <tbody>${templates.map(t => `<tr>
                <td style="color:var(--text);font-weight:500">${escapeHtml(t.vendor_name)}</td>
                <td>${escapeHtml(t.vendor_id)}</td>
                <td><span class="tag-chip">${escapeHtml(t.format_type)}</span></td>
                <td>${(t.header_fields || []).map(f => `<span class="tag-chip">${escapeHtml(f)}</span>`).join(' ')}</td>
                <td>${(t.line_item_fields || []).map(f => `<span class="tag-chip">${escapeHtml(f)}</span>`).join(' ')}</td>
                <td>${(t.extraction_rules || []).length} rule${(t.extraction_rules || []).length !== 1 ? 's' : ''}</td>
                <td>
                    <a class="link-btn" href="#/template/${escapeHtml(t.vendor_id)}">⚙ Edit</a>
                    <a class="link-btn" href="#/extract" onclick="localStorage.setItem('extractVendor','${escapeInlineJsString(t.vendor_id)}')">▶ Use</a>
                </td>
            </tr>`).join('')}</tbody>
        </table>`}
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${templates.length} TEMPLATE${templates.length !== 1 ? 'S' : ''} SAVED</span>
    </div>`;
    updateNavActive();
}

// ══════════════════════════════════════════════════════════════════════
// PAGE 4: EXTRACTION (preserving old UI exactly)
// ══════════════════════════════════════════════════════════════════════
async function renderExtractPage(app) {
    let vendors = [];
    try { vendors = await apiJSON('/vendors'); db.vendors = vendors; } catch (e) { }

    const savedVid = localStorage.getItem('extractVendor');
    if (savedVid && vendors.find(v => v.id === savedVid)) {
        db.activeVendorId = savedVid;
        localStorage.removeItem('extractVendor');
    }
    if (!db.activeVendorId && vendors.length) db.activeVendorId = vendors[0].id;

    app.innerHTML = headerHTML() + `
    <aside class="sidebar">
        <div class="sidebar-section"><div class="section-title">Select Vendor</div></div>
        <div style="padding:0 12px 8px">
            <select class="format-select" id="vendorSelect" onchange="extSetVendor(this.value)" style="width:100%">
                ${vendors.map(v => `<option value="${escapeHtml(v.id)}" ${v.id === db.activeVendorId ? 'selected' : ''}>${escapeHtml(v.name)} (${escapeHtml(v.id)})</option>`).join('')}
            </select>
        </div>
        <div class="divider"></div>
        <div class="sidebar-section"><div class="section-title">Document</div></div>
        <div class="upload-zone" id="dropzone" onclick="document.getElementById('fileInput').click()">
            <div class="upload-icon">⬆</div>
            <div class="upload-text"><strong>Drop file or browse</strong><br>PDF, JPEG, PNG</div>
        </div>
        <input type="file" id="fileInput" accept="image/*,.pdf" style="display:none" onchange="handleFile(this.files[0])">
        <div id="fileBadge" style="display:none" class="file-badge"><span>✓</span><span id="fileNameLabel" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span></div>
        <div class="divider"></div>
        <div class="sidebar-section"><div class="section-title">Header Fields</div></div>
        <div style="padding:0 12px 4px">
            <div class="add-rule-row"><input class="add-rule-input" id="headerFieldInput" placeholder="e.g. supplier, po_number" onkeydown="if(event.key==='Enter')addHeaderField()"><button class="small-btn" onclick="addHeaderField()">+ Add</button></div>
            <div id="headerFieldsList" class="rules-list" style="margin-top:4px"></div>
        </div>
        <div class="divider"></div>
        <div class="sidebar-section"><div class="section-title">Line Item Columns</div></div>
        <div style="padding:0 12px 4px">
            <div class="add-rule-row"><input class="add-rule-input" id="lineItemFieldInput" placeholder="e.g. no, description, qty" onkeydown="if(event.key==='Enter')addLineItemField()"><button class="small-btn" onclick="addLineItemField()">+ Add</button></div>
            <div id="lineItemFieldsList" class="rules-list" style="margin-top:4px"></div>
        </div>
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
        <div class="rp-section">
            <div class="rp-title">Format Type</div>
            <select class="format-select" id="formatType">
                <option value="single_po_multipage">Single PO — multi-page (header on pg 1)</option>
                <option value="po_per_page">Different PO per page</option>
                <option value="single_page">Single page document</option>
            </select>
            <div style="font-size:9px;color:var(--text-dim);line-height:1.5" id="formatHint">Page 1 → header + line items. Pages 2–N → line items only.</div>
        </div>
        <div class="rp-section">
            <div class="rp-title">Prompt Instructions</div>
            <textarea class="prompt-area" id="promptInstructions" placeholder="e.g. Supplier address is always in the top-left block..."></textarea>
        </div>
        <div class="rp-section">
            <div class="rp-title">Extraction Rules</div>
            <div class="rules-list" id="rulesList"></div>
            <div class="add-rule-row"><input class="add-rule-input" id="newRuleInput" placeholder="Add extraction rule..." onkeydown="if(event.key==='Enter')addRule()"><button class="small-btn" onclick="addRule()">+ Add</button></div>
            <button class="save-tpl-btn" onclick="saveTemplate()">💾 Save template for this vendor</button>
        </div>
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
            <div style="display:flex;gap:6px;margin-top:6px">
                <button class="small-btn" style="flex:1;background:var(--blue-bg);border-color:var(--blue);color:var(--blue)" onclick="downloadCsv()">Export CSV</button>
                <button class="small-btn" style="flex:1;background:var(--green);border-color:var(--green)" onclick="downloadExcel()">Export Excel</button>
            </div>
        </div>
    </aside>
    <div class="bottom-bar">
        <button class="extract-btn secondary" id="autoExtractBtn" onclick="autoExtract()" disabled>Auto Extract</button>
        <button class="extract-btn" id="extractBtn" onclick="runExtract()" disabled>Extract Fields</button>
    </div>` + vendorModalHTML();

    await extLoadVendorConfig();
    setupDragZoom();
    setupDropzone();
    updateNavActive();
}

// ── Extract page helpers ──────────────────────────────────────────────
async function extSetVendor(id) {
    db.activeVendorId = id;
    await extLoadVendorConfig();
}

async function extLoadVendorConfig() {
    const v = db.vendors.find(v => v.id === db.activeVendorId);
    const nameEl = document.getElementById('rpEntityName');
    if (nameEl) nameEl.textContent = v ? v.name : '---';

    if (v) {
        try {
            const tmpl = await apiJSON(`/vendors/${v.id}/template`);
            document.getElementById('formatType').value = tmpl.format_type || 'single_po_multipage';
            document.getElementById('promptInstructions').value = tmpl.prompt_instructions || '';
            extractionRules = [...(tmpl.extraction_rules || [])];
            headerFields = [...(tmpl.header_fields || [])];
            lineItemFields = [...(tmpl.line_item_fields || [])];
        } catch (e) {
            document.getElementById('formatType').value = 'single_po_multipage';
            document.getElementById('promptInstructions').value = '';
            extractionRules = []; headerFields = []; lineItemFields = [];
        }
    } else { extractionRules = []; headerFields = []; lineItemFields = []; }

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
    const autoBtn = document.getElementById('autoExtractBtn');
    if (!btn || !autoBtn) return;
    const total = headerFields.length + lineItemFields.length;
    btn.textContent = total === 0 ? 'Extract Fields' : `Extract ${total} Field${total !== 1 ? 's' : ''}`;
    btn.disabled = !loadedFile || total === 0;
    btn.className = 'extract-btn';
    btn.onclick = runExtract;

    autoBtn.textContent = 'Auto Extract';
    autoBtn.disabled = !loadedFile;
    autoBtn.className = 'extract-btn secondary';
    autoBtn.onclick = autoExtract;
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
    if (!v) return;
    const payload = { format_type: document.getElementById('formatType').value, vendor_name: v.name, header_fields: headerFields, line_item_fields: lineItemFields, prompt_instructions: document.getElementById('promptInstructions').value || null, extraction_rules: extractionRules };
    try {
        const resp = await apiJSON(`/vendors/${v.id}/template`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        showToast(`Template saved — hash: ${(resp.prompt_hash || 'none').slice(0, 12)}...`);
    } catch (e) { showToast('Failed: ' + e.message); }
}

function extUpdateFormatHint() {
    const hints = { single_po_multipage: 'Page 1: header + line items. Pages 2-N: line items only, same PO.', po_per_page: 'Each page is a self-contained PO with its own header and line items.', single_page: 'Entire document is a single page. Extract all fields at once.' };
    const el = document.getElementById('formatHint');
    if (el) el.textContent = hints[document.getElementById('formatType').value] || '';
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
        const resp = await apiJSON('/upload-preview', { method: 'POST', body: formData });
        extractionPages = resp.pages;
        totalPages = resp.total_pages;
        currentPage = 1;
        updatePageNav();
        renderCurrentPage();
    } catch (e) {
        // Fallback: client-side preview
        const reader = new FileReader();
        reader.onload = ev => {
            const b64 = ev.target.result;
            const container = document.getElementById('docImgContainer');
            if (!container) return;
            container.replaceChildren();
            if (file.name.toLowerCase().endsWith('.pdf') || b64.startsWith('data:application/pdf')) {
                const embed = document.createElement('embed');
                embed.src = b64;
                embed.type = 'application/pdf';
                embed.style.cssText = 'width:100%;height:600px;border:none;';
                container.appendChild(embed);
            } else {
                const img = document.createElement('img');
                img.src = b64;
                img.alt = 'document';
                img.className = 'doc-img';
                container.appendChild(img);
            }
            totalPages = 1; currentPage = 1; updatePageNav();
        };
        reader.readAsDataURL(file);
    }
    renderBottomBar();
    setStatus('optimal');
}

function updatePageNav() {
    const ind = document.getElementById('pageIndicator');
    if (ind) ind.textContent = `PAGE ${currentPage} / ${totalPages}`;
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
    { id: 'upload', label: 'Uploading', detail: 'Data stream verified' },
    { id: 'normalize', label: 'Normalization', detail: 'Page render engine' },
    { id: 'ocr', label: 'OCR Execution', detail: 'PaddleOCR v5' },
    { id: 'llm', label: 'Qwen VL', detail: 'Vision extraction' },
    { id: 'postprocess', label: 'Post-Processing', detail: 'Field mapping' },
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
        <div class="pipeline-panel" id="pipelinePanel">
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

function showPipelinePanel() {
    const panel = document.getElementById('pipelinePanel');
    if (!panel) {
        // Inject pipeline HTML into the right panel
        const rp = document.querySelector('.right-panel');
        if (rp) rp.insertAdjacentHTML('beforeend', buildPipelineHTML());
    }

    // Hide config sections (format type, instructions, rules, result)
    document.querySelectorAll('.right-panel > .rp-section').forEach(sec => {
        // Keep the Active Entity section (first one) visible
        const title = sec.querySelector('.rp-title');
        if (title && title.textContent.trim() === 'Active Entity') return;
        sec.style.display = 'none';
    });

    // Show pipeline
    const pp = document.getElementById('pipelinePanel');
    if (pp) pp.classList.add('active');

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
    setPipelineStage('upload', 'done', 'Data stream verified');

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
    // Stop timer
    if (_pipelineTimerInterval) { clearInterval(_pipelineTimerInterval); _pipelineTimerInterval = null; }

    // Hide pipeline
    const pp = document.getElementById('pipelinePanel');
    if (pp) pp.classList.remove('active');

    // Restore config sections
    document.querySelectorAll('.right-panel > .rp-section').forEach(sec => {
        sec.style.display = '';
    });
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

function updatePipelineFromSSE(jobState) {
    const extraction = jobState.extraction || {};
    const progress = extraction.progress || {};
    const stage = progress.stage;
    const message = progress.message || '';
    const event = jobState.event;

    if (!stage) return;

    // Mark upload as done always
    setPipelineStage('upload', 'done', 'Data stream verified');

    // Define stage order for sequential markings
    const stageOrder = ['upload', 'normalize', 'ocr', 'llm', 'postprocess'];
    const currentIdx = stageOrder.indexOf(stage);

    // Mark all stages before current as done
    for (let i = 1; i < currentIdx; i++) {
        const s = stageOrder[i];
        const el = document.getElementById(`pipeStage_${s}`);
        if (el && !el.classList.contains('done')) {
            const stageInfo = PIPELINE_STAGES.find(p => p.id === s);
            setPipelineStage(s, 'done', stageInfo ? stageInfo.detail + ' — complete' : 'Complete');
        }
    }

    // Handle terminal events
    if (event === 'done') {
        stageOrder.forEach(s => {
            setPipelineStage(s, 'done', null);
        });
        // Stop timer
        if (_pipelineTimerInterval) { clearInterval(_pipelineTimerInterval); _pipelineTimerInterval = null; }
        // Final elapsed
        const el = document.getElementById('pipelineTimer');
        if (el && _pipelineStartTime) {
            const elapsed = ((Date.now() - _pipelineStartTime) / 1000).toFixed(1);
            el.textContent = `${elapsed}s — COMPLETE`;
        }
        return;
    }

    if (event === 'failed') {
        setPipelineStage(stage, 'failed', message);
        if (_pipelineTimerInterval) { clearInterval(_pipelineTimerInterval); _pipelineTimerInterval = null; }
        return;
    }

    // Set current stage as active
    _pipelineSeenStages.add(stage);
    const detail = message || PIPELINE_STAGES.find(p => p.id === stage)?.detail || '';
    setPipelineStage(stage, 'active', detail);

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
    if (!btn) return;
    btn.className = 'extract-btn processing';
    btn.textContent = '⏹ STOP';
    btn.disabled = false;
    btn.onclick = cancelExtract;
    const otherBtn = document.getElementById(buttonId === 'extractBtn' ? 'autoExtractBtn' : 'extractBtn');
    if (otherBtn) otherBtn.disabled = true;
}

function resetExtractButtons() {
    activeJobId = null;
    activeExtractButtonId = 'extractBtn';
    _pipelineSeenStages = new Set();
    hidePipelinePanel();
    renderBottomBar();
}

async function cancelExtract() {
    if (!activeExtractionId) return;
    const btn = document.getElementById(activeExtractButtonId);
    if (btn) { btn.textContent = 'Cancelling...'; btn.disabled = true; }
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
        const activeBtn = document.getElementById(activeExtractButtonId);
        if (activeBtn) activeBtn.textContent = `Page ${progress.page}/${progress.total_pages}`;
        totalPages = progress.total_pages;
    } else if (progress.message) {
        const activeBtn = document.getElementById(activeExtractButtonId);
        if (activeBtn) activeBtn.textContent = progress.message;
    }

    if (!extraction) return;

    if (extraction.status === 'done') {
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
        if (extraction.id) {
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

let _activeStreamAbort = null;

async function streamJob(jobId) {
    // Abort any previous stream
    if (_activeStreamAbort) { _activeStreamAbort.abort(); _activeStreamAbort = null; }
    const controller = new AbortController();
    _activeStreamAbort = controller;
    activeJobId = jobId;

    const response = await fetch(`${API}/jobs/${jobId}/stream`, { signal: controller.signal });
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
                            const message = extraction.error || (event.job && event.job.error) || 'Extraction failed';
                            const cs2 = document.getElementById('conflictSection'); if (cs2) cs2.style.display = 'block';
                            const cm = document.getElementById('conflictMsg'); if (cm) cm.textContent = 'Extraction failed: ' + message;
                            const cc = document.getElementById('conflictCandidates'); if (cc) cc.innerHTML = '<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">Retry Extraction</button>';
                            setStatus('optimal');
                            document.getElementById('rpBadge').className = 'rp-badge review';
                            document.getElementById('rpBadge').textContent = 'NEEDS REVIEW';
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
    const total = headerFields.length + lineItemFields.length;
    if (!loadedFile || total === 0) return;
    lastExtractionMode = 'fields';
    const v = db.vendors.find(v => v.id === db.activeVendorId);
    if (!v) { showToast('Select a vendor first'); return; }
    const format = document.getElementById('formatType').value;

    setStatus('processing');
    document.getElementById('rpBadge').className = 'rp-badge processing';
    document.getElementById('rpBadge').textContent = 'PROCESSING';
    document.getElementById('extractBtn').className = 'extract-btn processing';
    document.getElementById('extractBtn').textContent = 'Saving template...';
    document.getElementById('extractBtn').disabled = true;
    const autoBtn = document.getElementById('autoExtractBtn');
    if (autoBtn) autoBtn.disabled = true;
    const cs = document.getElementById('conflictSection'); if (cs) cs.style.display = 'none';
    const rs = document.getElementById('resultSection'); if (rs) rs.style.display = 'none';

    // Auto-save template
    try {
        const payload = { format_type: format, vendor_name: v.name, header_fields: headerFields, line_item_fields: lineItemFields, prompt_instructions: document.getElementById('promptInstructions').value || null, extraction_rules: extractionRules };
        await apiJSON(`/vendors/${v.id}/template`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    } catch (e) {
        showToast('Template save failed: ' + e.message);
        resetExtractButtons(); setStatus('optimal'); return;
    }

    showStopButton('extractBtn');
    showPipelinePanel();
    const formData = new FormData();
    formData.append('file', loadedFile);
    formData.append('vendor_id', v.id);
    formData.append('header_fields', JSON.stringify(headerFields));
    formData.append('line_item_fields', JSON.stringify(lineItemFields));
    formData.append('format_type', format);

    try {
        const payload = await apiJSON('/ingest/ui', { method: 'POST', body: formData });
        activeExtractionId = payload.extraction_id;
        await streamJob(payload.job_id);
    } catch (err) {
        const cs2 = document.getElementById('conflictSection'); if (cs2) cs2.style.display = 'block';
        const cm = document.getElementById('conflictMsg'); if (cm) cm.textContent = 'Extraction failed: ' + err.message;
        const cc = document.getElementById('conflictCandidates'); if (cc) cc.innerHTML = '<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">Retry Extraction</button>';
        setStatus('optimal');
        document.getElementById('rpBadge').className = 'rp-badge review';
        document.getElementById('rpBadge').textContent = 'NEEDS REVIEW';
    } finally {
        resetExtractButtons();
    }
}

async function autoExtract() {
    if (!loadedFile) return;
    lastExtractionMode = 'auto';
    const v = db.vendors.find(v => v.id === db.activeVendorId);
    if (!v) { showToast('Select a vendor first'); return; }
    const format = document.getElementById('formatType').value;

    setStatus('processing');
    document.getElementById('rpBadge').className = 'rp-badge processing';
    document.getElementById('rpBadge').textContent = 'PROCESSING';
    const autoBtn = document.getElementById('autoExtractBtn');
    if (autoBtn) {
        autoBtn.className = 'extract-btn secondary processing';
        autoBtn.textContent = 'Preparing...';
        autoBtn.disabled = true;
    }
    const extractBtn = document.getElementById('extractBtn');
    if (extractBtn) extractBtn.disabled = true;
    const cs = document.getElementById('conflictSection'); if (cs) cs.style.display = 'none';
    const rs = document.getElementById('resultSection'); if (rs) rs.style.display = 'none';

    showStopButton('autoExtractBtn');
    showPipelinePanel();
    const formData = new FormData();
    formData.append('file', loadedFile);
    formData.append('vendor_id', v.id);
    formData.append('format_type', format);

    try {
        const payload = await apiJSON('/ingest/ui', { method: 'POST', body: formData });
        activeExtractionId = payload.extraction_id;
        await streamJob(payload.job_id);
    } catch (err) {
        const cs2 = document.getElementById('conflictSection'); if (cs2) cs2.style.display = 'block';
        const cm = document.getElementById('conflictMsg');
        if (cm) cm.textContent = 'Auto extract failed: ' + err.message;
        const cc = document.getElementById('conflictCandidates'); if (cc) cc.innerHTML = '<button class="small-btn" onclick="retryLastExtract()" style="margin-top:8px">Retry Auto Extract</button>';
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
        const cs2 = document.getElementById('conflictSection'); if (cs2) cs2.style.display = 'block';
        const cm = document.getElementById('conflictMsg'); if (cm) cm.textContent = 'Resume failed: ' + err.message;
        const cc = document.getElementById('conflictCandidates'); if (cc) cc.innerHTML = `<button class="small-btn" onclick="resumeExtract(${extractionId})" style="margin-top:8px">Retry Resume</button>`;
        setStatus('optimal');
        document.getElementById('rpBadge').className = 'rp-badge review';
        document.getElementById('rpBadge').textContent = 'NEEDS REVIEW';
    } finally {
        resetExtractButtons();
    }
}

function retryLastExtract() {
    return lastExtractionMode === 'auto' ? autoExtract() : runExtract();
}

function showResult(data) {
    const rs = document.getElementById('resultSection'); if (rs) rs.style.display = 'block';
    const rb = document.getElementById('resultBlock'); if (rb) rb.textContent = JSON.stringify(data, null, 2);
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

function downloadExcel() {
    if (!activeExtractionId) {
        showToast('No active extraction selected.');
        return;
    }
    // The backend uses Content-Disposition headers for proper filename
    window.open(`${API}/extractions/${activeExtractionId}/export.xlsx`, '_blank');
}

function downloadCsv() {
    if (activeExtractionId) {
        window.open(`${API}/extractions/${activeExtractionId}/export.csv`, '_blank');
        return;
    }

    if (!lastResult) {
        showToast('No data to export.');
        return;
    }

    const headerKeys = Object.keys(lastResult).filter(k => k !== 'line_items');
    const items = Array.isArray(lastResult.line_items) ? lastResult.line_items : [];
    const rows = items.length > 0 ? items : [{}];

    const itemKeys = new Set();
    items.forEach(it => {
        if (it && typeof it === 'object') Object.keys(it).forEach(k => itemKeys.add(k));
    });
    const itemCols = Array.from(itemKeys);
    const allCols = [...headerKeys, ...itemCols];

    let csvStr = allCols.map(v => `"${(v || '').toString().replace(/"/g, '""')}"`).join(',') + '\n';

    rows.forEach(item => {
        const rowData = allCols.map(col => {
            let val = headerKeys.includes(col) ? lastResult[col] : (item ? item[col] : '');
            if (val === null || val === undefined) val = '';
            if (typeof val === 'object') val = JSON.stringify(val);
            return `"${val.toString().replace(/"/g, '""')}"`;
        });
        csvStr += rowData.join(',') + '\n';
    });

    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csvStr], { type: 'text/csv' }));
    a.download = `extraction_${activeExtractionId || Date.now()}.csv`;
    a.click();
}

function setStatus(state) {
    const dot = document.getElementById('hdrDot');
    const lbl = document.getElementById('hdrStatus');
    if (!dot || !lbl) return;
    if (state === 'processing') { dot.className = 'status-dot processing'; lbl.className = 'status-label processing'; lbl.innerHTML = 'System Status: <span>PROCESSING</span>'; }
    else { dot.className = 'status-dot'; lbl.className = 'status-label'; lbl.innerHTML = 'System Status: <span>OPTIMAL</span>'; }
}

// ══════════════════════════════════════════════════════════════════════
// PAGE 5: HISTORY
// ══════════════════════════════════════════════════════════════════════
async function renderHistoryPage(app) {
    let extractions = [];
    try { extractions = await apiJSON('/extractions?limit=50'); } catch (e) { console.warn(e); }

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
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${extractions.length} EXTRACTION${extractions.length !== 1 ? 'S' : ''}</span>
    </div>
    <div class="detail-overlay" id="detailOverlay" onclick="if(event.target===this)this.classList.remove('open')">
        <div class="detail-box" id="detailBox"></div>
    </div>`;
    updateNavActive();
}


// ── NAV HELPER ─────────────────────────────────────────────────────────
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

function updateNavActive() {
    const route = getRoute();
    document.querySelectorAll('.nav-tab').forEach(t => {
        t.classList.remove('active');
        const r = t.dataset.route;
        if (r === '/vendors' && (route === '/' || route === '/vendors')) t.classList.add('active');
        else if (r && route.startsWith(r)) t.classList.add('active');
    });
}

// ══════════════════════════════════════════════════════════════════════
// PAGE 6: REVIEW (Human-in-the-Loop Field Mapping + Correction)
// ══════════════════════════════════════════════════════════════════════

let _rvCurrentPage = 1;
let _rvTotalPages = 1;
let _rvPages = [];
let _rvResult = {};
let _rvFieldLocs = {};
let _rvOcrData = [];          // PaddleOCR words per page (for click-to-select)
let _rvZoom = 100;
let _rvIsDragging = false;
let _rvDragStartX = 0, _rvDragStartY = 0, _rvScrollStartX = 0, _rvScrollStartY = 0;
let _rvResizeHandler = null;
let _rvExtractionId = null;

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
    // The in-memory reviewResult/reviewFieldLocations can be stale after
    // corrections are saved, or reviewPages may have been snapshotted before
    // the async page load finished.
    try {
        const data = await apiJSON(`/extractions/${extractionId}`);
        // Use corrected_result if available (previous corrections), else original result
        const effectiveResult = data.corrected_result || data.result || {};
        _rvResult = JSON.parse(JSON.stringify(effectiveResult));
        _rvOriginalResult = JSON.parse(JSON.stringify(data.result || {})); // always the original
        _rvFieldLocs = data.field_locations || {};
        try { _rvPages = await apiJSON(`/extractions/${extractionId}/pages`); } catch (e2) { _rvPages = []; }
    } catch (e) {
        // API failed — fall back to in-memory state if available
        console.warn('Failed to fetch extraction from API, using in-memory fallback:', e.message);
        _rvResult = JSON.parse(JSON.stringify(reviewResult || {}));
        _rvOriginalResult = JSON.parse(JSON.stringify(reviewResult || {}));
        _rvFieldLocs = JSON.parse(JSON.stringify(reviewFieldLocations || {}));
        _rvPages = extractionPages.length ? extractionPages : [];
    }
    _rvUndoStack = [];
    _rvPendingSelection = null;

    // Load OCR data for click-to-select
    _rvOcrData = [];
    try {
        const ocrResp = await apiJSON(`/extractions/${extractionId}/ocr`);
        _rvOcrData = ocrResp.ocr_pages || [];
    } catch (e) {
        console.warn('OCR data not available for click-to-select:', e.message);
    }

    _rvTotalPages = _rvPages.length || 1;
    _rvCurrentPage = 1;
    _rvZoom = 100;

    // Count matched vs total header fields
    const headerKeys = Object.keys(_rvResult).filter(k => k !== 'line_items' && _rvResult[k] != null);
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
    const conf = loc.confidence || 'high';
    if (conf === 'high') return 'found-high';
    if (conf === 'medium' || conf === 'low') return 'found-low';
    return 'found-high';
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

function rvRenderFields() {
    const el = document.getElementById('rvFieldsList');
    if (!el) return;

    const headerKeys = Object.keys(_rvResult).filter(k => k !== 'line_items' && _rvResult[k] != null);

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
    const cols = Object.keys(lineItems[0]);
    liWrap.innerHTML = `
    <table class="review-line-items">
        <thead><tr>${cols.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>
        <tbody>${lineItems.map((row, rowIdx) =>
        `<tr>${cols.map(c => {
            const compKey = `line_item_${rowIdx}_${c}`;
            const loc = _rvFieldLocs[compKey];
            const cellClass = loc ? (loc.strategy === 'manual' ? 'cell-manual' : 'cell-matched') : '';
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
        rect.className = 'mapping-rect';
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

        const fieldEl = document.getElementById(`rvField_${fieldName}`);
        const rectEl = document.getElementById(`rvRect_${fieldName}`);
        if (!fieldEl || !rectEl) continue;

        const fRect = fieldEl.getBoundingClientRect();
        const mRect = rectEl.getBoundingClientRect();

        const x1 = fRect.right;
        const y1 = fRect.top + fRect.height / 2;
        const x2 = mRect.left;
        const y2 = mRect.top + mRect.height / 2;

        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.classList.add('mapping-line');
        line.id = `rvLine_${fieldName}`;
        line.setAttribute('x1', x1);
        line.setAttribute('y1', y1);
        line.setAttribute('x2', x2);
        line.setAttribute('y2', y2);
        svg.appendChild(line);
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

    if (matchedWords.length === 0) {
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

    // Build precise bounding box from the matched OCR words
    const x0 = Math.min(...matchedWords.map(w => w.box[0]));
    const y0 = Math.min(...matchedWords.map(w => w.box[1]));
    const x1 = Math.max(...matchedWords.map(w => w.box[2]));
    const y1 = Math.max(...matchedWords.map(w => w.box[3]));
    const avgScore = matchedWords.reduce((s, w) => s + (w.score || 0), 0) / matchedWords.length;

    // Store pending selection — don't apply yet, show preview bar
    _rvPendingSelection = {
        fieldKey: _rvSelectionField,
        combinedText,
        box: [x0, y0, x1, y1],
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

    const fieldKey = sel.fieldKey;

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
            const numVal = parseFloat(sel.combinedText.replace(/[,\s]/g, ''));
            _rvResult.line_items[rowIdx][colName] = isNaN(numVal) ? sel.combinedText : numVal;
        }
    } else {
        _rvResult[fieldKey] = sel.combinedText;
    }

    // Update field_locations
    _rvFieldLocs[fieldKey] = {
        page: _rvCurrentPage,
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
    _rvCurrentPage = Math.max(1, Math.min(_rvTotalPages, _rvCurrentPage + dir));
    rvRenderCurrentPage();
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
    navigator.clipboard.writeText(JSON.stringify(_rvResult, null, 2));
    showToast('JSON copied to clipboard');
}

function rvDownloadJSON() {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(_rvResult, null, 2)], { type: 'application/json' }));
    a.download = `review_${_rvExtractionId || Date.now()}.json`;
    a.click();
}

async function rvConfirm() {
    // Save corrections to backend if changes were made
    if (_rvDirty && _rvExtractionId) {
        try {
            await apiJSON(`/extractions/${_rvExtractionId}/corrections`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    corrected_result: _rvResult,
                    field_locations: _rvFieldLocs,
                }),
            });
            // Update in-memory snapshots so reopening review shows saved state
            reviewResult = JSON.parse(JSON.stringify(_rvResult));
            reviewFieldLocations = JSON.parse(JSON.stringify(_rvFieldLocs));
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

// ── INIT ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    if (!window.location.hash) window.location.hash = '#/vendors';
    router();
});
