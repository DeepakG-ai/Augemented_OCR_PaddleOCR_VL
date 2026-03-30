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
let activeExtractButtonId = 'extractBtn';
let lastExtractionMode = 'fields';

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
    try { vendors = await apiJSON('/vendors'); } catch(e) { console.warn(e); }
    db.vendors = vendors;

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">⚡ Active Vendors</div>
        <div id="vendorCards"></div>
        <button class="add-vendor-btn" style="max-width:300px;margin-top:12px" onclick="openAddVendor()">+ Add New Vendor</button>
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${vendors.length} VENDOR${vendors.length!==1?'S':''} REGISTERED</span>
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
            <div class="vendor-card-info" onclick="navigate('#/template/${v.id}')">
                <div class="vendor-card-name">${v.name}</div>
                <div class="vendor-card-id">ID: ${v.id} · Created: ${new Date(v.created_at).toLocaleDateString()}</div>
            </div>
            <div class="vendor-card-actions">
                <a class="link-btn" href="#/template/${v.id}">⚙ Template</a>
                <a class="link-btn" href="#/extract" onclick="localStorage.setItem('extractVendor','${v.id}')">▶ Extract</a>
                <button class="del-btn" onclick="event.stopPropagation();deleteVendor('${v.id}','${v.name}')">✕ Delete</button>
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
    } catch(e) { showToast('Delete failed: ' + e.message); }
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
    const id = document.getElementById('newVendorId').value.trim().toUpperCase() || Math.random().toString(36).slice(2,10).toUpperCase();
    if (!name) return;
    try {
        await apiJSON('/vendors', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id,name}) });
        showToast(`Vendor ${name} created`);
    } catch(e) { showToast('Failed: '+e.message); }
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
    try { const vendors = await apiJSON('/vendors'); vendor = vendors.find(v=>v.id===vendorId); } catch(e) {}
    tplVendorName = vendor ? vendor.name : vendorId;
    try { tmpl = await apiJSON(`/vendors/${vendorId}/template`); } catch(e) {}

    headerFields = tmpl ? [...(tmpl.header_fields||[])] : [];
    lineItemFields = tmpl ? [...(tmpl.line_item_fields||[])] : [];
    extractionRules = tmpl ? [...(tmpl.extraction_rules||[])] : [];
    const fmt = tmpl ? tmpl.format_type : 'single_po_multipage';
    const instructions = tmpl ? (tmpl.prompt_instructions||'') : '';
    const hash = tmpl ? (tmpl.prompt_hash||'') : '';

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">⚙ Template — ${tplVendorName}</div>
        ${hash ? `<div style="font-size:9px;color:var(--text-dim);margin-bottom:12px">PROMPT HASH: ${hash.slice(0,16)}...</div>` : ''}
        <div class="tpl-grid">
            <div class="tpl-panel">
                <div class="tpl-panel-title">Format Type</div>
                <select class="format-select" id="tplFormat" onchange="updateTplFormatHint()">
                    <option value="single_po_multipage" ${fmt==='single_po_multipage'?'selected':''}>Single PO — multi-page (header on pg 1)</option>
                    <option value="po_per_page" ${fmt==='po_per_page'?'selected':''}>Different PO per page</option>
                    <option value="single_page" ${fmt==='single_page'?'selected':''}>Single page document</option>
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
            </div>
        </div>
        <button class="tpl-save-btn" onclick="saveTplConfig()">💾 Save Template</button>
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">VENDOR: ${tplVendorName} · ${headerFields.length} HEADER FIELDS · ${lineItemFields.length} LINE COLUMNS · ${extractionRules.length} RULES</span>
    </div>`;

    tplRenderHeaders(); tplRenderLines(); tplRenderRules(); updateTplFormatHint(); updateNavActive();
}

function tplAddHeader() {
    const inp = document.getElementById('tplHeaderInput');
    const val = inp.value.trim().toLowerCase().replace(/\s+/g,'_');
    if (!val || headerFields.includes(val)) return;
    headerFields.push(val); inp.value = ''; tplRenderHeaders();
}
function tplRemoveHeader(i) { headerFields.splice(i,1); tplRenderHeaders(); }
function tplRenderHeaders() {
    const el = document.getElementById('tplHeaderList');
    if (!el) return;
    el.innerHTML = headerFields.length ? headerFields.map((f,i)=>`<div class="rule-item"><div class="rule-dot" style="background:var(--blue)"></div><span style="flex:1;text-transform:uppercase;letter-spacing:0.08em">${f}</span><button class="rule-del" onclick="tplRemoveHeader(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim)">No header fields</div>';
}

function tplAddLine() {
    const inp = document.getElementById('tplLineInput');
    const val = inp.value.trim().toLowerCase().replace(/\s+/g,'_');
    if (!val || lineItemFields.includes(val)) return;
    lineItemFields.push(val); inp.value = ''; tplRenderLines();
}
function tplRemoveLine(i) { lineItemFields.splice(i,1); tplRenderLines(); }
function tplRenderLines() {
    const el = document.getElementById('tplLineList');
    if (!el) return;
    el.innerHTML = lineItemFields.length ? lineItemFields.map((f,i)=>`<div class="rule-item"><div class="rule-dot" style="background:var(--green)"></div><span style="flex:1;text-transform:uppercase;letter-spacing:0.08em">${f}</span><button class="rule-del" onclick="tplRemoveLine(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim)">No line item columns</div>';
}

function tplAddRule() {
    const inp = document.getElementById('tplRuleInput');
    const val = inp.value.trim(); if (!val) return;
    extractionRules.push(val); inp.value = ''; tplRenderRules();
}
function tplAddRuleText(text) { extractionRules.push(text); tplRenderRules(); }
function tplRemoveRule(i) { extractionRules.splice(i,1); tplRenderRules(); }
function tplRenderRules() {
    const el = document.getElementById('tplRulesList');
    if (!el) return;
    el.innerHTML = extractionRules.length ? extractionRules.map((r,i)=>`<div class="rule-item"><div class="rule-dot"></div><span style="flex:1">${r}</span><button class="rule-del" onclick="tplRemoveRule(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim)">No rules yet</div>';
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
        const resp = await apiJSON(`/vendors/${tplVendorId}/template`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
        showToast(`Template saved — hash: ${resp.prompt_hash.slice(0,12)}...`);
    } catch(e) { showToast('Save failed: '+e.message); }
}

// ══════════════════════════════════════════════════════════════════════
// PAGE 3: SAVED TEMPLATES
// ══════════════════════════════════════════════════════════════════════
async function renderSavedTemplatesPage(app) {
    let templates = [];
    try { templates = await apiJSON('/templates'); } catch(e) { console.warn(e); }

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">📋 Saved Templates</div>
        ${templates.length === 0 ? '<div style="color:var(--text-dim);padding:20px">No templates saved yet. Create one from a vendor page.</div>' : `
        <table class="tpl-table">
            <thead><tr>
                <th>Vendor</th><th>ID</th><th>Format</th><th>Header Fields</th><th>Line Items</th><th>Rules</th><th>Actions</th>
            </tr></thead>
            <tbody>${templates.map(t=>`<tr>
                <td style="color:var(--text);font-weight:500">${t.vendor_name}</td>
                <td>${t.vendor_id}</td>
                <td><span class="tag-chip">${t.format_type}</span></td>
                <td>${(t.header_fields||[]).map(f=>`<span class="tag-chip">${f}</span>`).join(' ')}</td>
                <td>${(t.line_item_fields||[]).map(f=>`<span class="tag-chip">${f}</span>`).join(' ')}</td>
                <td>${(t.extraction_rules||[]).length} rule${(t.extraction_rules||[]).length!==1?'s':''}</td>
                <td>
                    <a class="link-btn" href="#/template/${t.vendor_id}">⚙ Edit</a>
                    <a class="link-btn" href="#/extract" onclick="localStorage.setItem('extractVendor','${t.vendor_id}')">▶ Use</a>
                </td>
            </tr>`).join('')}</tbody>
        </table>`}
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${templates.length} TEMPLATE${templates.length!==1?'S':''} SAVED</span>
    </div>`;
    updateNavActive();
}

// ══════════════════════════════════════════════════════════════════════
// PAGE 4: EXTRACTION (preserving old UI exactly)
// ══════════════════════════════════════════════════════════════════════
async function renderExtractPage(app) {
    let vendors = [];
    try { vendors = await apiJSON('/vendors'); db.vendors = vendors; } catch(e) {}

    const savedVid = localStorage.getItem('extractVendor');
    if (savedVid && vendors.find(v=>v.id===savedVid)) {
        db.activeVendorId = savedVid;
        localStorage.removeItem('extractVendor');
    }
    if (!db.activeVendorId && vendors.length) db.activeVendorId = vendors[0].id;

    app.innerHTML = headerHTML() + `
    <aside class="sidebar">
        <div class="sidebar-section"><div class="section-title">Select Vendor</div></div>
        <div style="padding:0 12px 8px">
            <select class="format-select" id="vendorSelect" onchange="extSetVendor(this.value)" style="width:100%">
                ${vendors.map(v => `<option value="${v.id}" ${v.id===db.activeVendorId?'selected':''}>${v.name} (${v.id})</option>`).join('')}
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
                <button class="small-btn" style="flex:1" onclick="downloadResult()">Download</button>
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
    const v = db.vendors.find(v=>v.id===db.activeVendorId);
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
    const val = inp.value.trim().toLowerCase().replace(/\s+/g,'_');
    if (!val || headerFields.includes(val)) return;
    headerFields.push(val); inp.value = '';
    renderHeaderFields(); renderBottomBar();
}
function removeHeaderField(i) { headerFields.splice(i,1); renderHeaderFields(); renderBottomBar(); }
function renderHeaderFields() {
    const el = document.getElementById('headerFieldsList'); if (!el) return;
    el.innerHTML = headerFields.length ? headerFields.map((f,i)=>`<div class="rule-item"><div class="rule-dot" style="background:var(--blue)"></div><span style="flex:1;text-transform:uppercase;letter-spacing:0.08em">${f}</span><button class="rule-del" onclick="removeHeaderField(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim);padding:4px 0">No header fields added</div>';
}

function addLineItemField() {
    const inp = document.getElementById('lineItemFieldInput');
    const val = inp.value.trim().toLowerCase().replace(/\s+/g,'_');
    if (!val || lineItemFields.includes(val)) return;
    lineItemFields.push(val); inp.value = '';
    renderLineItemFields(); renderBottomBar();
}
function removeLineItemField(i) { lineItemFields.splice(i,1); renderLineItemFields(); renderBottomBar(); }
function renderLineItemFields() {
    const el = document.getElementById('lineItemFieldsList'); if (!el) return;
    el.innerHTML = lineItemFields.length ? lineItemFields.map((f,i)=>`<div class="rule-item"><div class="rule-dot" style="background:var(--green)"></div><span style="flex:1;text-transform:uppercase;letter-spacing:0.08em">${f}</span><button class="rule-del" onclick="removeLineItemField(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim);padding:4px 0">No line item columns added</div>';
}

function renderBottomBar() {
    const btn = document.getElementById('extractBtn');
    const autoBtn = document.getElementById('autoExtractBtn');
    if (!btn || !autoBtn) return;
    const total = headerFields.length + lineItemFields.length;
    btn.textContent = total === 0 ? 'Extract Fields' : `Extract ${total} Field${total!==1?'s':''}`;
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
function deleteRule(i) { extractionRules.splice(i,1); renderRules(); }
function renderRules() {
    const el = document.getElementById('rulesList'); if (!el) return;
    el.innerHTML = extractionRules.length ? extractionRules.map((r,i)=>`<div class="rule-item"><div class="rule-dot"></div><span style="flex:1">${r}</span><button class="rule-del" onclick="deleteRule(${i})">x</button></div>`).join('') : '<div style="font-size:10px;color:var(--text-dim);padding:4px 0">No rules yet</div>';
}

async function saveTemplate() {
    const v = db.vendors.find(v=>v.id===db.activeVendorId);
    if (!v) return;
    const payload = { format_type: document.getElementById('formatType').value, vendor_name: v.name, header_fields: headerFields, line_item_fields: lineItemFields, prompt_instructions: document.getElementById('promptInstructions').value || null, extraction_rules: extractionRules };
    try {
        const resp = await apiJSON(`/vendors/${v.id}/template`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
        showToast(`Template saved — hash: ${resp.prompt_hash.slice(0,12)}...`);
    } catch(e) { showToast('Failed: '+e.message); }
}

function extUpdateFormatHint() {
    const hints = { single_po_multipage:'Page 1: header + line items. Pages 2-N: line items only, same PO.', po_per_page:'Each page is a self-contained PO with its own header and line items.', single_page:'Entire document is a single page. Extract all fields at once.' };
    const el = document.getElementById('formatHint');
    if (el) el.textContent = hints[document.getElementById('formatType').value] || '';
}



// ── FILE UPLOAD ────────────────────────────────────────────────────────
function setupDropzone() {
    const dz = document.getElementById('dropzone'); if (!dz) return;
    dz.addEventListener('dragover', e => { e.preventDefault(); dz.style.background='var(--blue-bg)'; });
    dz.addEventListener('dragleave', () => { dz.style.background=''; });
    dz.addEventListener('drop', e => { e.preventDefault(); dz.style.background=''; if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });
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
        const resp = await apiJSON('/upload-preview', { method:'POST', body: formData });
        extractionPages = resp.pages;
        totalPages = resp.total_pages;
        currentPage = 1;
        updatePageNav();
        renderCurrentPage();
    } catch(e) {
        // Fallback: client-side preview
        const reader = new FileReader();
        reader.onload = ev => {
            const b64 = ev.target.result;
            const container = document.getElementById('docImgContainer');
            if (file.name.toLowerCase().endsWith('.pdf') || b64.startsWith('data:application/pdf')) {
                container.innerHTML = `<embed src="${b64}" type="application/pdf" style="width:100%;height:600px;border:none;">`;
            } else {
                container.innerHTML = `<img src="${b64}" alt="document" class="doc-img">`;
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
    const mime = page.mime_type || 'image/png';
    container.innerHTML = `<img src="data:${mime};base64,${page.image_b64}" alt="Page ${currentPage}" class="doc-img">`;
    zoomLevel = 100;
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
    if (frame) frame.style.transform = `scale(${zoomLevel/100})`;
    if (info) info.textContent = `ZOOM: ${zoomLevel}% · DRAG TO PAN`;
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
    activeExtractionId = null;
    activeExtractButtonId = 'extractBtn';
    renderBottomBar();
}

async function cancelExtract() {
    if (!activeExtractionId) return;
    const btn = document.getElementById(activeExtractButtonId);
    if (btn) { btn.textContent = 'Cancelling...'; btn.disabled = true; }
    try {
        await apiJSON(`/extract/cancel/${activeExtractionId}`, { method: 'POST' });
    } catch(e) { showToast('Cancel failed: ' + e.message); }
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

function handleSSEStream(reader) {
    const decoder = new TextDecoder();
    let buffer = '';
    return (async () => {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n'); buffer = lines.pop();
            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed.startsWith('data: ')) continue;
                try {
                    const event = JSON.parse(trimmed.slice(6));
                    if (event.event === 'progress') {
                        const activeBtn = document.getElementById(activeExtractButtonId);
                        if (activeBtn) activeBtn.textContent = `⏹ Page ${event.page}/${event.total_pages}`;
                        totalPages = event.total_pages;
                        if (event.extraction_id) activeExtractionId = event.extraction_id;
                    } else if (event.event === 'resume') {
                        showToast(`Resuming from page ${event.start_from_page}`);
                    } else if (event.event === 'done') {
                        activeExtractionId = null;
                        lastResult = event.result;
                        totalPages = event.total_pages || totalPages;
                        showResult(event.result);
                        showToast(`Extracted in ${(event.duration_ms / 1000).toFixed(1)}s`);
                        setStatus('optimal');
                        document.getElementById('rpBadge').className = 'rp-badge optimal';
                        document.getElementById('rpBadge').textContent = 'OPTIMAL';
                        if (event.extraction_id) loadExtractionPages(event.extraction_id);
                    } else if (event.event === 'cancelled') {
                        activeExtractionId = null;
                        if (event.result) { lastResult = event.result; showResult(event.result); }
                        setStatus('optimal');
                        document.getElementById('rpBadge').className = 'rp-badge review';
                        document.getElementById('rpBadge').textContent = 'PARTIAL';
                        showResumeButton(event.extraction_id, event.last_completed_page, event.total_pages);
                        showToast(`Stopped after page ${event.last_completed_page}`);
                    } else if (event.event === 'error') {
                        throw new Error(event.error || 'Extraction failed');
                    }
                } catch(pe) { if (pe.message.includes('Extraction failed') || pe.message.startsWith('HTTP')) throw pe; }
            }
        }
    })();
}

async function runExtract() {
    const total = headerFields.length + lineItemFields.length;
    if (!loadedFile || total === 0) return;
    lastExtractionMode = 'fields';
    const v = db.vendors.find(v=>v.id===db.activeVendorId);
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
        await apiJSON(`/vendors/${v.id}/template`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
    } catch(e) {
        showToast('Template save failed: '+e.message);
        resetExtractButtons(); setStatus('optimal'); return;
    }

    showStopButton('extractBtn');
    const formData = new FormData();
    formData.append('file', loadedFile);
    formData.append('vendor_id', v.id);
    formData.append('header_fields', JSON.stringify(headerFields));
    formData.append('line_item_fields', JSON.stringify(lineItemFields));
    formData.append('format_type', format);

    try {
        const response = await fetch(`${API}/extract`, { method:'POST', body:formData });
        if (!response.ok) { const err = await response.text(); throw new Error(`HTTP ${response.status}: ${err}`); }
        // Parse extraction_id from first event or use a heuristic
        await handleSSEStream(response.body.getReader());
    } catch(err) {
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
    const v = db.vendors.find(v=>v.id===db.activeVendorId);
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
    const formData = new FormData();
    formData.append('file', loadedFile);
    formData.append('vendor_id', v.id);
    formData.append('format_type', format);

    try {
        const response = await fetch(`${API}/extract`, { method:'POST', body:formData });
        if (!response.ok) { const err = await response.text(); throw new Error(`HTTP ${response.status}: ${err}`); }
        await handleSSEStream(response.body.getReader());
    } catch(err) {
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
        const response = await fetch(`${API}/extract/resume/${extractionId}`, { method: 'POST' });
        if (!response.ok) { const err = await response.text(); throw new Error(`HTTP ${response.status}: ${err}`); }
        await handleSSEStream(response.body.getReader());
    } catch(err) {
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
    } catch(e) { console.warn('Failed loading pages:', e.message); }
}

function copyResult() {
    if (!lastResult) return;
    navigator.clipboard.writeText(JSON.stringify(lastResult, null, 2));
    showToast('Copied to clipboard');
}

function downloadResult() {
    if (!lastResult) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(lastResult, null, 2)], { type:'application/json' }));
    a.download = `extraction_${Date.now()}.json`; a.click();
}

function setStatus(state) {
    const dot = document.getElementById('hdrDot');
    const lbl = document.getElementById('hdrStatus');
    if (!dot || !lbl) return;
    if (state === 'processing') { dot.className='status-dot processing'; lbl.className='status-label processing'; lbl.innerHTML='System Status: <span>PROCESSING</span>'; }
    else { dot.className='status-dot'; lbl.className='status-label'; lbl.innerHTML='System Status: <span>OPTIMAL</span>'; }
}

// ══════════════════════════════════════════════════════════════════════
// PAGE 5: HISTORY
// ══════════════════════════════════════════════════════════════════════
async function renderHistoryPage(app) {
    let extractions = [];
    try { extractions = await apiJSON('/extractions?limit=50'); } catch(e) { console.warn(e); }

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">📜 Extraction History</div>
        <div id="historyList">${extractions.length === 0 ? '<div style="color:var(--text-dim);padding:20px">No extractions yet.</div>' : extractions.map(e => `
            <div class="history-item" onclick="showHistoryDetailSafe(${e.id})">
                <div class="history-filename">${e.filename || 'Unknown'}</div>
                <div class="history-meta">
                    <span style="color:var(--blue);font-weight:500">${e.vendor_name || e.vendor_id}</span>
                    <span class="history-status status-${e.status}">${e.status}</span>
                    <span>${e.total_pages || 0} pages</span>
                    <span>${e.duration_ms ? (e.duration_ms/1000).toFixed(1)+'s' : ''}</span>
                    <span>${new Date(e.created_at).toLocaleString()}</span>
                </div>
            </div>`).join('')}
        </div>
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${extractions.length} EXTRACTION${extractions.length!==1?'S':''}</span>
    </div>
    <div class="detail-overlay" id="detailOverlay" onclick="if(event.target===this)this.classList.remove('open')">
        <div class="detail-box" id="detailBox"></div>
    </div>`;
    updateNavActive();
}

async function showHistoryDetail(id) {
    try {
        const data = await apiJSON(`/extractions/${id}`);
        const box = document.getElementById('detailBox');
        box.innerHTML = `
            <div class="modal-title">${data.filename || 'Extraction'} — ${data.vendor_name || data.vendor_id}</div>
            <div style="margin-bottom:8px">
                <span class="rp-badge ${data.status==='done'?'optimal':data.status==='failed'?'review':'processing'}">${data.status}</span>
                <span style="font-size:10px;color:var(--text-dim);margin-left:8px">${data.total_pages} pages · ${data.duration_ms ? (data.duration_ms/1000).toFixed(1)+'s' : ''} · ${new Date(data.created_at).toLocaleString()}</span>
            </div>
            ${data.error ? `<div style="color:var(--red);font-size:11px;margin-bottom:8px">Error: ${data.error}</div>` : ''}
            <div class="rp-title" style="margin-top:12px">Final Result</div>
            <div class="result-block" style="max-height:300px">${JSON.stringify(data.result, null, 2) || 'null'}</div>
            ${data.page_results ? `<div class="rp-title" style="margin-top:12px">Page Results</div><div class="result-block" style="max-height:200px">${JSON.stringify(data.page_results, null, 2)}</div>` : ''}
            <div style="display:flex;gap:6px;margin-top:10px">
                <button class="small-btn" id="historyCopyBtn">Copy JSON</button>
                <button class="small-btn" onclick="document.getElementById('detailOverlay').classList.remove('open')">Close</button>
            </div>`;
        const cb = document.getElementById('historyCopyBtn');
        if (cb) {
            cb.onclick = () => {
                navigator.clipboard.writeText(JSON.stringify(data.result, null, 2));
                showToast('Copied');
            };
        }
        document.getElementById('detailOverlay').classList.add('open');
    } catch(e) { showToast('Failed to load: '+e.message); }
}

// ── NAV HELPER ─────────────────────────────────────────────────────────
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
        setText('historyDetailResult', JSON.stringify(data.result, null, 2) || 'null');

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
                navigator.clipboard.writeText(JSON.stringify(data.result, null, 2));
                showToast('Copied');
            };
        }

        const closeBtn = document.getElementById('historyCloseBtnSafe');
        if (closeBtn) {
            closeBtn.onclick = () => document.getElementById('detailOverlay').classList.remove('open');
        }

        document.getElementById('detailOverlay').classList.add('open');
    } catch(e) { showToast('Failed to load: ' + e.message); }
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

// ── INIT ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    if (!window.location.hash) window.location.hash = '#/vendors';
    router();
});
