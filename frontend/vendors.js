/* ── Augmented OCR — Vendors / Template / Saved-Templates pages ─────── */

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
    const isAdmin = (getAuthUser() || {}).role === 'admin';

    window.tplPrompts = {
        page1System: tmpl && tmpl.system_prompt_page1 ? tmpl.system_prompt_page1 : (tmpl && tmpl.system_prompt ? tmpl.system_prompt : 'No system prompt generated yet. Save the template.'),
        page1User: tmpl && tmpl.user_prompt_page1 ? tmpl.user_prompt_page1 : (tmpl && tmpl.user_prompt ? tmpl.user_prompt : 'No user message available.'),
        page2System: tmpl && tmpl.system_prompt_page2 ? tmpl.system_prompt_page2 : 'No page 2 system prompt available.',
        page2User: tmpl && tmpl.user_prompt_page2 ? tmpl.user_prompt_page2 : 'No page 2 user message available.',
    };
    window.tplShowPrompt = function (type) {
        const el = document.getElementById('tplPromptPreview');
        if (!el) return;
        const prompts = window.tplPrompts || {};
        const promptMeta = {
            page1System: ['btnPage1SystemPrompt', 'Page 1 system prompt: fields, vendor confirmation, and label/header boxes.'],
            page1User: ['btnPage1UserPrompt', 'Page 1 user message: fields plus the boxes response shape.'],
            page2System: ['btnPage2SystemPrompt', 'Page 2+ system prompt: fields only, using the same saved template instructions and rules.'],
            page2User: ['btnPage2UserPrompt', 'Page 2+ user message: fields-only response shape.'],
        };
        el.value = prompts[type] || 'Prompt preview unavailable.';
        Object.values(promptMeta).forEach(([id]) => {
            const btn = document.getElementById(id);
            if (btn) btn.style = '';
        });
        const activeBtn = document.getElementById((promptMeta[type] || [])[0]);
        if (activeBtn) activeBtn.style = 'background:var(--blue-bg);color:var(--blue);border-color:var(--blue)';
        document.getElementById('tplPromptDesc').textContent = (promptMeta[type] || [null, 'Prompt preview unavailable.'])[1];
    };
    const promptPreviewPanel = isAdmin ? `
        <div class="tpl-panel" style="margin-top:16px; grid-column: 1 / -1;">
            <div style="display:flex; gap:8px; margin-bottom:8px; align-items:center;">
                <div class="tpl-panel-title" style="margin-bottom:0">Prompt Previews</div>
                <div style="flex:1"></div>
                <button id="btnPage1SystemPrompt" class="small-btn" onclick="tplShowPrompt('page1System')">Page 1 System</button>
                <button id="btnPage1UserPrompt" class="small-btn" onclick="tplShowPrompt('page1User')">Page 1 User</button>
                <button id="btnPage2SystemPrompt" class="small-btn" onclick="tplShowPrompt('page2System')">Page 2+ System</button>
                <button id="btnPage2UserPrompt" class="small-btn" onclick="tplShowPrompt('page2User')">Page 2+ User</button>
            </div>
            <textarea id="tplPromptPreview" class="prompt-area" style="min-height:400px;font-family:monospace;font-size:11px;background:var(--bg);color:var(--text);border:1px solid var(--border);" readonly></textarea>
            <div id="tplPromptDesc" style="font-size:10px;color:var(--text-dim);margin-top:8px">This is the admin-only prompt preview.</div>
        </div>` : '';

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
                <div style="margin-top:16px" class="tpl-panel-title">Vendor Aliases <span style="font-size:9px;color:var(--text-dim);font-weight:normal;margin-left:4px">— words that identify this vendor in page 1</span></div>
                <div id="tplAliasList" class="rules-list" style="margin-top:4px"></div>
                <div class="add-rule-row" style="margin-top:4px"><input class="add-rule-input" id="tplAliasInput" placeholder="e.g. rd jet llc, jetro" onkeydown="if(event.key==='Enter')tplAddAlias()"><button class="small-btn" onclick="tplAddAlias()">+ Add</button></div>
            </div>
            <div class="tpl-panel">
                <div class="tpl-panel-title">Prompt Instructions</div>
                <textarea class="prompt-area" id="tplPrompt" style="min-height:120px" placeholder="e.g. Supplier address is always in the top-left block. PO number starts with PO and is 5 digits...">${escapeHtml(instructions)}</textarea>
                <div style="margin-top:12px" class="tpl-panel-title">Extraction Rules</div>
                <div id="tplRulesList" class="rules-list"></div>
                <div class="add-rule-row"><input class="add-rule-input" id="tplRuleInput" placeholder="Add extraction rule..." onkeydown="if(event.key==='Enter')tplAddRule()"><button class="small-btn" onclick="tplAddRule()">+ Add</button></div>
                <div class="rule-hint">Examples — click to add:
                    <span class="rule-example" onclick="tplAddRuleText('Merge line items across pages')">Merge line items across pages</span>
                    <span class="rule-example" onclick="tplAddRuleText('Numbers must be numeric, not strings')">Numbers must be numeric</span>
                    <span class="rule-example" onclick="tplAddRuleText('Dates in DD/MM/YYYY format')">Dates DD/MM/YYYY</span>
                    <span class="rule-example" onclick="tplAddRuleText('Skip rows with empty description')">Skip empty rows</span>
        </div>
        ${promptPreviewPanel}
        <button class="tpl-save-btn" style="margin-top:16px" onclick="saveTplConfig()">💾 Save Template</button>
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">VENDOR: ${tplVendorName} · ${headerFields.length} HEADER FIELDS · ${lineItemFields.length} LINE COLUMNS · ${extractionRules.length} RULES</span>
    </div>`;

    tplRenderHeaders(); tplRenderLines(); tplRenderRules(); updateTplFormatHint(); updateNavActive();
    if (isAdmin) tplShowPrompt('page1System');
    fetchVendorAliases(vendorId);
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

// -- Vendor Alias management ------------------------------------------------

async function fetchVendorAliases(vendorId) {
    try {
        const aliases = await apiJSON(`/vendors/${vendorId}/aliases`);
        tplRenderAliases(aliases);
    } catch (e) { console.warn('Failed to load aliases', e); }
}

function tplRenderAliases(aliases) {
    const el = document.getElementById('tplAliasList');
    if (!el) return;
    el.innerHTML = aliases.length
        ? aliases.map(a => `<div class="rule-item"><div class="rule-dot" style="background:var(--amber)"></div><span style="flex:1;letter-spacing:0.06em">${escapeHtml(a.pattern)}</span><button class="rule-del" onclick="tplDeleteAlias(${a.id})">x</button></div>`).join('')
        : '<div style="font-size:10px;color:var(--text-dim)">No aliases yet — add words unique to this vendor</div>';
}

async function tplAddAlias() {
    const inp = document.getElementById('tplAliasInput');
    const val = inp.value.trim();
    if (!val) return;
    try {
        await apiJSON(`/vendors/${tplVendorId}/aliases`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pattern: val, weight: 1 }),
        });
        inp.value = '';
        await fetchVendorAliases(tplVendorId);
        showToast('Alias added');
    } catch (e) { showToast('Failed: ' + e.message); }
}

async function tplDeleteAlias(aliasId) {
    try {
        await apiJSON(`/vendors/aliases/${aliasId}`, { method: 'DELETE' });
        await fetchVendorAliases(tplVendorId);
        showToast('Alias removed');
    } catch (e) { showToast('Failed: ' + e.message); }
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
