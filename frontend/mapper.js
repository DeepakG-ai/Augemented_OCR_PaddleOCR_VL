/* ── Augmented OCR — ERP Field Mapper page ─────────────────────────────
   Maps a vendor's extracted (Qwen) field names to the program's fixed
   canonical "custom" fields. One mapping per vendor; applied in postprocess. */

// ── STATE ──────────────────────────────────────────────────────────────
let mpVendors = [];
let mpVendorId = null;
let mpTemplateId = null;
let mpHasTemplate = false;
let mpHeaderMap = {};            // {sourceField: targetField}
let mpLineMap = {};
let mpSourceHeader = [];         // [{name, value}]
let mpSourceLine = [];           // [{name, value}]
let mpTargetHeader = [];         // [targetName]
let mpTargetLine = [];
let mpNotices = [];
let mpSampleInfo = null;         // {filename, line_item_count, ...} or null
let mpCompareMode = false;
let mpLoading = false;
let mpSchemaId = null;           // currently assigned schema id
let mpSchemas = [];              // [{id, name, slug, is_system}]

// admin "act-as-client" state
let mpIsAdmin = false;
let mpClients = [];              // [{id, email}, ...] for admin selector
let mpClientId = null;           // UUID of selected client (null = admin's own)

// drag state
let mpDragging = false;
let mpDragFrom = null;           // {section, field, portEl}
let mpListenersBound = false;

function mpIsInternalSourceField(name) {
    return typeof name === 'string' && name.startsWith('_');
}

function mpCleanSourceMap(map) {
    return Object.fromEntries(
        Object.entries(map || {}).filter(([src]) => !mpIsInternalSourceField(src))
    );
}

// ── ENTRY ──────────────────────────────────────────────────────────────
async function renderMapperPage(app) {
    mpInjectStyles();
    app.className = 'app';
    app.innerHTML = headerHTML()
        + `<div id="mapperPage">${mpLoadingShellHTML()}</div>`
        + mpBottomBarHTML();
    updateNavActive();
    mpBindGlobalListeners();

    // Detect admin role from local auth state
    try {
        const u = JSON.parse(localStorage.getItem('auth_user') || 'null');
        mpIsAdmin = u && u.role === 'admin';
    } catch (e) { mpIsAdmin = false; }

    // Admin: load client list so they can scope vendor list to a specific client
    if (mpIsAdmin) {
        try {
            const users = await apiJSON('/admin/users');
            mpClients = (users || []).filter(u => u.role !== 'admin');
        } catch (e) { mpClients = []; }
        // Restore last-used client from storage
        const savedClient = localStorage.getItem('mapperClientId');
        mpClientId = (savedClient && mpClients.find(c => c.id === savedClient))
            ? savedClient : (mpClients.length ? mpClients[0].id : null);
    } else {
        mpClientId = null;
        mpClients = [];
    }

    await mpReloadVendors();
}

async function mpReloadVendors() {
    try {
        const qs = (mpIsAdmin && mpClientId) ? `?user_id=${encodeURIComponent(mpClientId)}` : '';
        mpVendors = await apiJSON('/vendors' + qs);
    } catch (e) {
        mpVendors = [];
    }

    const saved = localStorage.getItem('mapperVendor');
    if (saved && mpVendors.find(v => v.id === saved)) {
        mpVendorId = saved;
    } else {
        mpVendorId = mpVendors.length ? mpVendors[0].id : null;
    }

    mpRenderBottomBar();
    if (mpVendorId) {
        await mpLoadVendor(mpVendorId);
    } else {
        mpRenderPage();
    }
}

// ── DATA LOADING ───────────────────────────────────────────────────────
async function mpLoadVendor(vendorId) {
    mpVendorId = vendorId;
    localStorage.setItem('mapperVendor', vendorId);
    mpLoading = true;
    mpCompareMode = false;
    const page = document.getElementById('mapperPage');
    if (page) page.innerHTML = mpLoadingShellHTML();

    try {
        const [mapping, sample] = await Promise.all([
            apiJSON(`/vendors/${vendorId}/mapping`),
            apiJSON(`/vendors/${vendorId}/mapping/sample`).catch(() => ({ has_data: false })),
        ]);

        mpTemplateId = mapping.template_id || null;
        mpHasTemplate = !!mapping.has_template;
        mpHeaderMap = mpCleanSourceMap(mapping.header_map);
        mpLineMap = mpCleanSourceMap(mapping.line_map);
        mpTargetHeader = mapping.target_header_fields || [];
        mpTargetLine = mapping.target_line_fields || [];
        mpNotices = mapping.pending_notices || [];
        mpSchemaId = mapping.schema_id || null;
        mpSchemas = mapping.schemas || [];

        // Source fields: prefer the latest real extraction; fall back to the
        // template field names. Always show every mapped field even if the
        // sample no longer contains it.
        if (sample && sample.has_data) {
            mpSampleInfo = {
                filename: sample.filename,
                line_item_count: sample.line_item_count,
                extraction_id: sample.extraction_id,
            };
            mpSourceHeader = mpFieldsFromObject(sample.header, Object.keys(mpHeaderMap));
            mpSourceLine = mpFieldsFromObject(sample.line_item, Object.keys(mpLineMap));
        } else {
            mpSampleInfo = null;
            mpSourceHeader = mpFieldsFromNames(mapping.source_header_fields, Object.keys(mpHeaderMap));
            mpSourceLine = mpFieldsFromNames(mapping.source_line_fields, Object.keys(mpLineMap));
        }
    } catch (e) {
        showToast('Failed to load mapping: ' + e.message);
        mpHasTemplate = false;
        mpSourceHeader = []; mpSourceLine = [];
        mpHeaderMap = {}; mpLineMap = {}; mpNotices = [];
    }
    mpLoading = false;
    mpRenderPage();
}

// Build [{name,value}] from an object, appending any extra mapped keys.
function mpFieldsFromObject(obj, extraKeys) {
    const out = [];
    const seen = new Set();
    Object.entries(obj || {}).forEach(([k, v]) => {
        if (k === 'line_items' || mpIsInternalSourceField(k)) return;
        seen.add(k);
        out.push({ name: k, value: v });
    });
    (extraKeys || []).forEach(k => {
        if (mpIsInternalSourceField(k)) return;
        if (!seen.has(k)) { seen.add(k); out.push({ name: k, value: undefined }); }
    });
    return out;
}
function mpFieldsFromNames(names, extraKeys) {
    const out = [];
    const seen = new Set();
    (names || []).forEach(n => {
        if (!mpIsInternalSourceField(n) && !seen.has(n)) {
            seen.add(n); out.push({ name: n, value: undefined });
        }
    });
    (extraKeys || []).forEach(k => {
        if (!mpIsInternalSourceField(k) && !seen.has(k)) {
            seen.add(k); out.push({ name: k, value: undefined });
        }
    });
    return out;
}

// ── PAGE RENDER ────────────────────────────────────────────────────────
function mpRenderPage() {
    const page = document.getElementById('mapperPage');
    if (!page) return;

    if (!mpVendorId) {
        page.innerHTML = mpEmptyStateHTML('▦', 'No Vendor Selected',
            'Choose a vendor from the bar below to start mapping its fields.');
        mpRenderBottomBar();
        return;
    }
    if (!mpHasTemplate) {
        page.innerHTML = mpEmptyStateHTML('▤', 'No Template Configured',
            'This vendor has no extraction template yet. Configure one on the '
            + 'Extraction page before mapping its fields.');
        mpRenderBottomBar();
        return;
    }

    page.innerHTML =
        mpNoticesHTML()
        + (mpCompareMode ? mpCompareHTML() : mpMapperBodyHTML());

    if (!mpCompareMode) {
        mpRenderSource();
        mpRenderTarget();
        // Draw connections once layout settles.
        requestAnimationFrame(() => requestAnimationFrame(mpRedrawConnections));
        mpBindPanelScroll();
    }
    mpRenderBottomBar();
}

// ── MAPPER BODY (3-panel) ──────────────────────────────────────────────
function mpMapperBodyHTML() {
    return `
    <div class="mp-body">
        <div class="mp-panel mp-panel-source">
            <div class="mp-panel-head">
                <span class="mp-panel-title">Source</span>
                <span class="mp-panel-tag">AI OUTPUT</span>
            </div>
            <div class="mp-panel-scroll" id="mpSourceScroll"></div>
        </div>
        <div class="mp-canvas" id="mpCanvas">
            <div class="mp-canvas-hint" id="mpCanvasHint">
                <div class="mp-canvas-arrow">→</div>
                <div>DRAG A SOURCE FIELD ONTO A CUSTOM FIELD</div>
            </div>
        </div>
        <div class="mp-panel mp-panel-target">
            <div class="mp-panel-head">
                ${mpIsAdmin
                    ? `<select id="mpSchemaSelect" class="mp-schema-select" onchange="mpSchemaChange(this.value)"></select>`
                    : `<span class="mp-panel-title" id="mpSchemaLabel">Schema</span>`}
                <span class="mp-panel-tag accent">SCHEMA</span>
            </div>
            <div class="mp-panel-scroll" id="mpTargetScroll"></div>
        </div>
    </div>
    <svg class="mp-svg" id="mpSvg" xmlns="http://www.w3.org/2000/svg">
        <g id="mpConns"></g>
        <path id="mpLivePath" class="mp-live-path" style="display:none"/>
    </svg>`;
}

// ── SOURCE PANEL ───────────────────────────────────────────────────────
function mpRenderSource() {
    const el = document.getElementById('mpSourceScroll');
    if (!el) return;
    const note = mpSampleInfo
        ? `<div class="mp-src-note">▸ ${escapeHtml(mpSampleInfo.filename || 'latest extraction')}`
            + (mpSampleInfo.line_item_count > 1
                ? ` · showing 1 of ${mpSampleInfo.line_item_count} line rows` : '')
            + `</div>`
        : `<div class="mp-src-note warn">▸ no extraction yet — showing template field names</div>`;
    el.innerHTML = note
        + mpSourceSectionHTML('HEADER', mpSourceHeader, 'header')
        + mpSourceSectionHTML('LINE ITEM', mpSourceLine, 'line');
}

function mpSourceSectionHTML(label, fields, section) {
    const rows = fields.length
        ? fields.map((f, i) => {
            const map = section === 'header' ? mpHeaderMap : mpLineMap;
            const mapped = !!map[f.name];
            return `
            <div class="mp-field mp-src-field${mapped ? ' mapped' : ''}" style="animation-delay:${i * 22}ms">
                <span class="mp-field-name">${escapeHtml(f.name)}</span>
                <span class="mp-field-val">${mpFmtValue(f.value)}</span>
                <span class="mp-port mp-port-src${mapped ? ' mapped' : ''}${mpIsAdmin ? '' : ' mp-port-readonly'}"
                      data-section="${section}" data-field="${escapeHtml(f.name)}"
                      ${mpIsAdmin ? `onmousedown="mpStartDrag(event,'${section}','${escapeInlineJsString(f.name)}')"` : ''}></span>
            </div>`;
        }).join('')
        : `<div class="mp-empty-row">No ${section} fields</div>`;
    return `<div class="mp-section">
        <div class="mp-section-head"><span>${label}</span><span class="mp-section-count">${fields.length}</span></div>
        ${rows}
    </div>`;
}

// ── TARGET PANEL ───────────────────────────────────────────────────────
function mpRenderTarget() {
    const el = document.getElementById('mpTargetScroll');
    if (!el) return;

    // Populate schema dropdown (admin) or update label (user)
    if (mpIsAdmin) {
        const sel = document.getElementById('mpSchemaSelect');
        if (sel) {
            sel.innerHTML = mpSchemas.map(s =>
                `<option value="${s.id}" ${s.id === mpSchemaId ? 'selected' : ''}>${escapeHtml(s.name)}</option>`
            ).join('');
        }
    } else {
        const lbl = document.getElementById('mpSchemaLabel');
        if (lbl) {
            const schema = mpSchemas.find(s => s.id === mpSchemaId);
            lbl.textContent = schema ? schema.name : 'Schema';
        }
    }

    el.innerHTML =
        mpTargetSectionHTML('HEADER', mpTargetHeader, 'header')
        + mpTargetSectionHTML('LINE ITEMS', mpTargetLine, 'line');
}

function mpSchemaChange(schemaIdStr) {
    const newId = parseInt(schemaIdStr, 10);
    if (newId === mpSchemaId) return;
    mpSchemaId = newId;
    const schema = mpSchemas.find(s => s.id === newId);
    if (!schema) return;
    const newHeader = schema.header_fields || [];
    const newLine = schema.line_fields || [];
    // Drop connections that target fields no longer in the new schema
    const hSet = new Set(newHeader);
    const lSet = new Set(newLine);
    for (const k of Object.keys(mpHeaderMap)) if (!hSet.has(mpHeaderMap[k])) delete mpHeaderMap[k];
    for (const k of Object.keys(mpLineMap)) if (!lSet.has(mpLineMap[k])) delete mpLineMap[k];
    mpTargetHeader = newHeader;
    mpTargetLine = newLine;
    mpRenderSource();
    mpRenderTarget();
    requestAnimationFrame(() => requestAnimationFrame(mpRedrawConnections));
    showToast(`Schema changed to ${schema.name}`);
}

function mpTargetSectionHTML(label, targets, section) {
    const map = section === 'header' ? mpHeaderMap : mpLineMap;
    const rows = targets.map((t, i) => {
        const src = mpSourceForTarget(section, t);
        const mapped = !!src;
        let badge;
        if (mapped) {
            badge = `<span class="mp-tgt-src" title="${escapeHtml(src)}">${escapeHtml(src)}</span>`
                + (mpIsAdmin
                    ? `<button class="mp-tgt-x" onclick="mpDisconnect('${section}','${escapeInlineJsString(t)}')" title="Remove mapping">✕</button>`
                    : '');
        } else {
            badge = `<span class="mp-tgt-empty">unmapped</span>`;
        }
        const dropAttr = mpIsAdmin
            ? `onmouseup="mpEndDragOnTarget(event,'${section}','${escapeInlineJsString(t)}')"` : '';
        return `
        <div class="mp-field mp-tgt-field${mapped ? ' mapped' : ''}" style="animation-delay:${i * 22}ms">
            <span class="mp-port mp-port-tgt${mapped ? ' mapped' : ''}${mpIsAdmin ? '' : ' mp-port-readonly'}"
                  data-section="${section}" data-target="${escapeHtml(t)}"
                  ${dropAttr}></span>
            <span class="mp-field-name">${escapeHtml(t)}</span>
            ${badge}
        </div>`;
    }).join('');
    const mappedCount = targets.filter(t => mpSourceForTarget(section, t)).length;
    return `<div class="mp-section">
        <div class="mp-section-head"><span>${label}</span>
            <span class="mp-section-count">${mappedCount}/${targets.length}</span></div>
        ${rows}
    </div>`;
}

function mpSourceForTarget(section, target) {
    const map = section === 'header' ? mpHeaderMap : mpLineMap;
    for (const s of Object.keys(map)) if (map[s] === target) return s;
    return null;
}

// ── CONNECTIONS (SVG) ──────────────────────────────────────────────────
function mpRedrawConnections() {
    const g = document.getElementById('mpConns');
    if (!g) return;
    g.innerHTML = '';
    let any = false;
    ['header', 'line'].forEach(section => {
        const map = section === 'header' ? mpHeaderMap : mpLineMap;
        Object.entries(map).forEach(([src, tgt]) => {
            const srcEl = document.querySelector(
                `.mp-port-src[data-section="${section}"][data-field="${CSS.escape(src)}"]`);
            const tgtEl = document.querySelector(
                `.mp-port-tgt[data-section="${section}"][data-target="${CSS.escape(tgt)}"]`);
            if (!srcEl || !tgtEl) return;
            any = true;
            mpDrawConn(g, srcEl, tgtEl, section, src, tgt);
        });
    });
    const hint = document.getElementById('mpCanvasHint');
    if (hint) hint.style.opacity = any ? '0' : '0.6';
}

function mpPortCenter(el) {
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

function mpBezier(sx, sy, tx, ty) {
    const dx = Math.max(Math.abs(tx - sx) * 0.45, 60);
    return `M ${sx},${sy} C ${sx + dx},${sy} ${tx - dx},${ty} ${tx},${ty}`;
}

function mpDrawConn(g, srcEl, tgtEl, section, src, tgt) {
    const s = mpPortCenter(srcEl);
    const t = mpPortCenter(tgtEl);
    const d = mpBezier(s.x, s.y, t.x, t.y);
    const ns = 'http://www.w3.org/2000/svg';

    const line = document.createElementNS(ns, 'path');
    line.setAttribute('d', d);
    line.setAttribute('class', 'mp-conn-line');

    const hit = document.createElementNS(ns, 'path');
    hit.setAttribute('d', d);
    hit.setAttribute('class', 'mp-conn-hit');
    hit.addEventListener('click', () => mpDisconnect(section, tgt));

    g.appendChild(line);
    g.appendChild(hit);
}

// ── DRAG TO CONNECT ────────────────────────────────────────────────────
function mpStartDrag(ev, section, field) {
    ev.preventDefault();
    ev.stopPropagation();
    mpDragging = true;
    mpDragFrom = { section, field, portEl: ev.currentTarget };
    document.body.classList.add('mp-dragging');
    const live = document.getElementById('mpLivePath');
    if (live) live.style.display = '';
    const c = mpPortCenter(ev.currentTarget);
    mpUpdateLive(c.x, c.y, ev.clientX, ev.clientY);
    // highlight valid targets
    document.querySelectorAll(`.mp-port-tgt[data-section="${section}"]`)
        .forEach(p => p.classList.add('valid'));
}

function mpUpdateLive(sx, sy, ex, ey) {
    const live = document.getElementById('mpLivePath');
    if (live) live.setAttribute('d', mpBezier(sx, sy, ex, ey));
}

function mpEndDragOnTarget(ev, section, target) {
    if (!mpDragging || !mpDragFrom) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (mpDragFrom.section !== section) {
        showToast(`Cannot map a ${mpDragFrom.section} field to a ${section} field`);
        mpCancelDrag();
        return;
    }
    mpConnect(section, mpDragFrom.field, target);
    mpCancelDrag();
}

function mpConnect(section, src, target) {
    const map = section === 'header' ? mpHeaderMap : mpLineMap;
    // one source -> one target, one target -> one source
    for (const s of Object.keys(map)) if (map[s] === target) delete map[s];
    map[src] = target;
    mpRenderSource();
    mpRenderTarget();
    requestAnimationFrame(mpRedrawConnections);
    showToast(`${src}  →  ${target}`);
}

function mpDisconnect(section, target) {
    const map = section === 'header' ? mpHeaderMap : mpLineMap;
    for (const s of Object.keys(map)) if (map[s] === target) delete map[s];
    mpRenderSource();
    mpRenderTarget();
    requestAnimationFrame(mpRedrawConnections);
}

function mpCancelDrag() {
    mpDragging = false;
    mpDragFrom = null;
    document.body.classList.remove('mp-dragging');
    const live = document.getElementById('mpLivePath');
    if (live) live.style.display = 'none';
    document.querySelectorAll('.mp-port-tgt.valid').forEach(p => p.classList.remove('valid'));
}

function mpBindGlobalListeners() {
    if (mpListenersBound) return;
    mpListenersBound = true;
    document.addEventListener('mousemove', e => {
        if (!mpDragging || !mpDragFrom) return;
        const c = mpPortCenter(mpDragFrom.portEl);
        mpUpdateLive(c.x, c.y, e.clientX, e.clientY);
    });
    document.addEventListener('mouseup', () => { if (mpDragging) mpCancelDrag(); });
    window.addEventListener('resize', () => {
        if (getRoute() === '/mapper' && !mpCompareMode) mpRedrawConnections();
    });
}

function mpBindPanelScroll() {
    ['mpSourceScroll', 'mpTargetScroll'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('scroll', mpRedrawConnections);
    });
}

// ── COMPARE VIEW ───────────────────────────────────────────────────────
function mpComputeMapped() {
    const header = {};
    mpTargetHeader.forEach(t => { header[t] = null; });
    Object.entries(mpHeaderMap).forEach(([src, tgt]) => {
        const f = mpSourceHeader.find(x => x.name === src);
        if (tgt in header) header[tgt] = f ? mpNullable(f.value) : null;
    });
    const item = {};
    mpTargetLine.forEach(t => { item[t] = null; });
    Object.entries(mpLineMap).forEach(([src, tgt]) => {
        const f = mpSourceLine.find(x => x.name === src);
        if (tgt in item) item[tgt] = f ? mpNullable(f.value) : null;
    });
    return { ...header, items: [item] };
}

function mpComputeRaw() {
    const header = {};
    mpSourceHeader.forEach(f => { header[f.name] = mpNullable(f.value); });
    const item = {};
    mpSourceLine.forEach(f => { item[f.name] = mpNullable(f.value); });
    return { ...header, line_items: [item] };
}

function mpNullable(v) { return v === undefined ? null : v; }

function mpCompareHTML() {
    const raw = mpComputeRaw();
    const mapped = mpComputeMapped();
    const mappedCount = Object.keys(mpHeaderMap).length + Object.keys(mpLineMap).length;
    return `
    <div class="mp-compare">
        <div class="mp-cmp-col">
            <div class="mp-cmp-head">
                <span class="mp-cmp-title">AI Result</span>
                <span class="mp-panel-tag">RAW</span>
            </div>
            <pre class="mp-json">${mpHighlightJson(raw)}</pre>
        </div>
        <div class="mp-cmp-divider"><div class="mp-cmp-arrow">▶</div></div>
        <div class="mp-cmp-col">
            <div class="mp-cmp-head">
                <span class="mp-cmp-title">Mapped Custom Fields</span>
                <span class="mp-panel-tag accent">${mappedCount} MAPPED</span>
            </div>
            <pre class="mp-json">${mpHighlightJson(mapped)}</pre>
        </div>
    </div>`;
}

function mpHighlightJson(obj) {
    const json = JSON.stringify(obj, null, 2);
    return escapeHtml(json).replace(
        /(&quot;(\\.|[^&])*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+\.?\d*)/g,
        (m, str, _c, colon, kw, num) => {
            if (str !== undefined && colon) return `<span class="jk">${str}</span>${colon}`;
            if (str !== undefined) return `<span class="js">${str}</span>`;
            if (kw !== undefined) return `<span class="jn">${kw}</span>`;
            if (num !== undefined) return `<span class="jnum">${num}</span>`;
            return m;
        });
}

// ── NOTICES ────────────────────────────────────────────────────────────
function mpNoticesHTML() {
    if (!mpNotices.length) return '';
    const items = mpNotices.map(n => {
        const arrow = `${escapeHtml(n.old)} <span class="mp-n-arrow">→</span> ${escapeHtml(n.new)}`;
        const tail = n.remapped
            ? `<span class="mp-n-ok">mapping moved automatically</span>`
            : `<span class="mp-n-dim">field was not mapped</span>`;
        return `<div class="mp-notice-row">
            <span class="mp-n-tag">${escapeHtml((n.section || '').toUpperCase())}</span>
            <span class="mp-n-text">Field renamed: ${arrow}</span>
            ${tail}
        </div>`;
    }).join('');
    return `<div class="mp-notices">
        <div class="mp-notices-head">
            <span>⚠ ${mpNotices.length} TEMPLATE FIELD CHANGE${mpNotices.length > 1 ? 'S' : ''} DETECTED</span>
            <button class="mp-n-dismiss" onclick="mpDismissNotices()">Dismiss</button>
        </div>
        ${items}
    </div>`;
}

async function mpDismissNotices() {
    try {
        await apiFetch(`/vendors/${mpVendorId}/mapping/notices`, { method: 'DELETE' });
        mpNotices = [];
        mpRenderPage();
        showToast('Notices dismissed');
    } catch (e) {
        showToast('Failed: ' + e.message);
    }
}

// ── SAVE / TOGGLE ──────────────────────────────────────────────────────
async function mpSaveMapping() {
    if (!mpVendorId || !mpHasTemplate || !mpIsAdmin) return;
    const btn = document.getElementById('mpSaveBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
    try {
        await apiJSON(`/vendors/${mpVendorId}/mapping`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                header_map: mpHeaderMap,
                line_map: mpLineMap,
                schema_id: mpSchemaId,
            }),
        });
        const flash = document.getElementById('mapperPage');
        if (flash) { flash.classList.remove('mp-flash'); void flash.offsetWidth; flash.classList.add('mp-flash'); }
        showToast('Mapping saved — applied automatically to future extractions');
    } catch (e) {
        showToast('Save failed: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Save Mapping'; }
    }
}

function mpToggleCompare() {
    mpCompareMode = !mpCompareMode;
    mpRenderPage();
}

function mpClearAll() {
    if (!Object.keys(mpHeaderMap).length && !Object.keys(mpLineMap).length) {
        showToast('Nothing to clear');
        return;
    }
    mpHeaderMap = {};
    mpLineMap = {};
    mpRenderPage();
    showToast('All connections cleared — save to persist');
}

function mpSelectVendor(id) {
    if (id && id !== mpVendorId) mpLoadVendor(id);
}

// ── BOTTOM BAR ─────────────────────────────────────────────────────────
function mpBottomBarHTML() {
    return `<div class="bottom-bar" id="mapperBottomBar"></div>`;
}

function mpRenderBottomBar() {
    const bar = document.getElementById('mapperBottomBar');
    if (!bar) return;

    // Vendor options — disambiguate duplicates by appending client seq when admin sees all
    const vendorOpts = mpVendors.length
        ? mpVendors.map(v => {
            const seq = (mpIsAdmin && !mpClientId && v.client_seq != null)
                ? ` [${v.client_seq}]` : '';
            return `<option value="${escapeHtml(v.id)}" ${v.id === mpVendorId ? 'selected' : ''}>`
                + `${escapeHtml(v.name)}${escapeHtml(seq)}</option>`;
          }).join('')
        : '<option value="">No vendors</option>';

    const total = mpTargetHeader.length + mpTargetLine.length;
    const mapped = Object.keys(mpHeaderMap).length + Object.keys(mpLineMap).length;
    const canEdit = mpHasTemplate && !mpLoading;

    // Admin client selector block
    let clientBlock = '';
    if (mpIsAdmin) {
        const clientOpts = mpClients.map(c =>
            `<option value="${escapeHtml(c.id)}" ${c.id === mpClientId ? 'selected' : ''}>`
            + `${escapeHtml(c.email)}</option>`).join('');
        const actingAs = mpClients.find(c => c.id === mpClientId);
        const badge = actingAs
            ? `<span class="mp-acting-badge">ACTING AS: ${escapeHtml(actingAs.email)}</span>`
            : '';
        clientBlock = `
            <span class="mp-bb-label">CLIENT</span>
            <select class="mp-select mp-client-select" onchange="mpSelectClient(this.value)">
                ${clientOpts}
            </select>
            ${badge}
            <div class="mp-bb-sep"></div>`;
    }

    bar.innerHTML = `
        ${clientBlock}
        <span class="mp-bb-label">VENDOR</span>
        <select class="mp-select" onchange="mpSelectVendor(this.value)">${vendorOpts}</select>
        <div class="mp-bb-sep"></div>
        <span class="mp-bb-stat">${mapped} <span>/ ${total} MAPPED</span></span>
        <div class="mp-bb-right">
            <button class="small-btn" onclick="mpToggleCompare()" ${canEdit ? '' : 'disabled'}>
                ${mpCompareMode ? '✎ Edit Mapping' : '⇄ Compare'}
            </button>
            ${mpIsAdmin ? `
            <button class="small-btn" onclick="mpClearAll()" ${canEdit ? '' : 'disabled'}>Clear</button>
            <button class="small-btn mp-primary" id="mpSaveBtn" onclick="mpSaveMapping()" ${canEdit ? '' : 'disabled'}>
                Save Mapping
            </button>` : `<span class="mp-readonly-badge">VIEW ONLY</span>`}
        </div>`;
}

async function mpSelectClient(clientId) {
    if (clientId === mpClientId) return;
    mpClientId = clientId || null;
    localStorage.setItem('mapperClientId', mpClientId || '');
    mpVendorId = null;
    mpHeaderMap = {}; mpLineMap = {};
    mpHasTemplate = false; mpNotices = []; mpCompareMode = false;
    await mpReloadVendors();
}

// ── SHELLS / EMPTY STATES ──────────────────────────────────────────────
function mpLoadingShellHTML() {
    return `<div class="mp-loading"><div class="mp-spinner"></div>
        <div>Loading mapping…</div></div>`;
}

function mpEmptyStateHTML(icon, title, body) {
    return `<div class="mp-empty">
        <div class="mp-empty-icon">${icon}</div>
        <div class="mp-empty-title">${escapeHtml(title)}</div>
        <div class="mp-empty-body">${escapeHtml(body)}</div>
    </div>`;
}

// ── HELPERS ────────────────────────────────────────────────────────────
function mpFmtValue(v) {
    if (v === undefined) return '<span class="mp-v-none">—</span>';
    if (v === null) return '<span class="mp-v-none">null</span>';
    if (typeof v === 'number') return `<span class="mp-v-num">${escapeHtml(String(v))}</span>`;
    let s = String(v);
    if (s.length > 22) s = s.slice(0, 21) + '…';
    return `<span class="mp-v-str">${escapeHtml(s)}</span>`;
}

// ── STYLES ─────────────────────────────────────────────────────────────
function mpInjectStyles() {
    if (document.getElementById('mapperStyles')) return;
    const style = document.createElement('style');
    style.id = 'mapperStyles';
    style.textContent = `
#mapperPage{grid-column:1/-1;grid-row:2;display:flex;flex-direction:column;overflow:hidden;background:var(--bg);}
#mapperPage.mp-flash{animation:mpFlash .7s ease;}
@keyframes mpFlash{50%{box-shadow:inset 0 0 0 2px var(--green);}}

.mp-body{flex:1;display:flex;overflow:hidden;}
.mp-panel{display:flex;flex-direction:column;overflow:hidden;background:var(--bg1);}
.mp-panel-source{width:300px;flex-shrink:0;border-right:1px solid var(--border);}
.mp-panel-target{width:328px;flex-shrink:0;border-left:1px solid var(--border);}
.mp-canvas{flex:1;position:relative;overflow:hidden;background:var(--bg);
  background-image:linear-gradient(var(--border) 1px,transparent 1px),
    linear-gradient(90deg,var(--border) 1px,transparent 1px);
  background-size:30px 30px;}
.mp-canvas::after{content:'';position:absolute;inset:0;
  background:radial-gradient(ellipse at center,transparent 55%,var(--bg) 100%);pointer-events:none;}
.mp-canvas-hint{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  text-align:center;color:var(--text-dim);font-size:9px;letter-spacing:0.18em;
  transition:opacity .3s;pointer-events:none;}
.mp-canvas-arrow{font-size:26px;opacity:0.35;margin-bottom:8px;}

.mp-panel-head{display:flex;align-items:center;justify-content:space-between;
  padding:9px 14px;border-bottom:1px solid var(--border);background:var(--bg2);flex-shrink:0;}
.mp-panel-title{font-family:var(--display);font-size:13px;font-weight:700;
  letter-spacing:0.06em;color:var(--text);text-transform:uppercase;}
.mp-panel-tag{font-size:8px;letter-spacing:0.14em;color:var(--text-dim);
  border:1px solid var(--border);border-radius:2px;padding:2px 6px;}
.mp-panel-tag.accent{color:var(--blue);border-color:var(--blue-dim);}
.mp-panel-scroll{flex:1;overflow-y:auto;padding:8px;}

.mp-src-note{font-size:9px;color:var(--text-dim);letter-spacing:0.06em;
  padding:4px 6px 8px;}
.mp-src-note.warn{color:var(--amber);}

.mp-section{margin-bottom:12px;}
.mp-section-head{display:flex;align-items:center;justify-content:space-between;
  padding:5px 8px;background:var(--bg3);border:1px solid var(--border);
  border-radius:3px;margin-bottom:4px;
  font-size:9px;font-weight:700;letter-spacing:0.14em;color:var(--text-mid);}
.mp-section-count{color:var(--text-dim);font-weight:400;}

.mp-field{position:relative;display:flex;align-items:center;gap:7px;
  min-height:30px;padding:3px 4px;border-radius:3px;
  border-left:2px solid transparent;
  animation:mpRowIn .32s ease both;}
@keyframes mpRowIn{from{opacity:0;transform:translateY(4px);}to{opacity:1;transform:none;}}
.mp-field:hover{background:var(--bg3);}
.mp-field.mapped{border-left-color:var(--blue);background:var(--blue-bg);}
.mp-field-name{font-size:11px;font-weight:500;color:var(--text);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.mp-src-field .mp-field-name{flex:0 1 auto;}
.mp-tgt-field .mp-field-name{flex:1;}
.mp-field-val{flex:1;text-align:right;font-size:9px;color:var(--text-dim);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.mp-v-str{color:var(--green);}
.mp-v-num{color:var(--amber);}
.mp-v-none{color:var(--text-dim);font-style:italic;}

.mp-tgt-src{font-size:8px;letter-spacing:0.04em;color:var(--blue);
  background:var(--blue-bg);border:1px solid var(--blue-dim);border-radius:2px;
  padding:2px 5px;max-width:104px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;flex-shrink:0;}
.mp-tgt-empty{font-size:8px;color:var(--text-dim);font-style:italic;flex-shrink:0;}
.mp-tgt-x{background:transparent;border:none;color:var(--text-dim);cursor:pointer;
  font-size:9px;padding:2px 3px;flex-shrink:0;}
.mp-tgt-x:hover{color:var(--red);}
.mp-empty-row{font-size:10px;color:var(--text-dim);padding:6px 8px;}

.mp-port{width:10px;height:10px;border-radius:50%;flex-shrink:0;
  border:2px solid var(--bg1);box-sizing:content-box;transition:transform .15s,box-shadow .15s;}
.mp-port-src{background:var(--text-dim);cursor:grab;margin-left:auto;}
.mp-port-src.mapped{background:var(--blue);}
.mp-port-src:hover{transform:scale(1.35);box-shadow:0 0 8px var(--blue);}
.mp-port-tgt{background:var(--text-dim);}
.mp-port-tgt.mapped{background:var(--blue);box-shadow:0 0 6px var(--blue-dim);}
.mp-port-tgt.valid{background:var(--green);box-shadow:0 0 9px var(--green);
  animation:mpPulse .7s ease infinite alternate;}
@keyframes mpPulse{to{box-shadow:0 0 14px var(--green);}}
body.mp-dragging,body.mp-dragging *{cursor:grabbing!important;}

.mp-svg{position:fixed;inset:0;width:100vw;height:100vh;z-index:40;
  pointer-events:none;overflow:visible;}
.mp-conn-line{fill:none;stroke:var(--blue);stroke-width:1.6;
  stroke-dasharray:7 4;animation:mpFlow 1.4s linear infinite;}
@keyframes mpFlow{to{stroke-dashoffset:-22;}}
.mp-conn-hit{fill:none;stroke:transparent;stroke-width:14;
  pointer-events:stroke;cursor:pointer;}
.mp-conn-hit:hover+.mp-conn-line,.mp-conn-line:hover{stroke:var(--red);}
.mp-live-path{fill:none;stroke:var(--blue);stroke-width:1.8;
  stroke-dasharray:6 4;opacity:0.8;}

/* compare view */
.mp-compare{flex:1;display:flex;overflow:hidden;}
.mp-cmp-col{flex:1;display:flex;flex-direction:column;overflow:hidden;background:var(--bg1);}
.mp-cmp-head{display:flex;align-items:center;justify-content:space-between;
  padding:9px 14px;border-bottom:1px solid var(--border);background:var(--bg2);}
.mp-cmp-title{font-family:var(--display);font-size:13px;font-weight:700;
  letter-spacing:0.06em;text-transform:uppercase;color:var(--text);}
.mp-cmp-divider{width:46px;flex-shrink:0;display:flex;align-items:center;
  justify-content:center;background:var(--bg);border-left:1px solid var(--border);
  border-right:1px solid var(--border);}
.mp-cmp-arrow{color:var(--blue);font-size:13px;}
.mp-json{flex:1;overflow:auto;margin:0;padding:14px 16px;font-family:var(--mono);
  font-size:11px;line-height:1.7;color:var(--text-mid);white-space:pre;}
.mp-json .jk{color:var(--blue);}
.mp-json .js{color:var(--green);}
.mp-json .jn{color:var(--text-dim);}
.mp-json .jnum{color:var(--amber);}

/* notices */
.mp-notices{background:var(--amber-bg);border-bottom:1px solid var(--amber);
  padding:8px 14px;flex-shrink:0;}
.mp-notices-head{display:flex;align-items:center;justify-content:space-between;
  font-size:9px;font-weight:700;letter-spacing:0.12em;color:var(--amber);margin-bottom:5px;}
.mp-n-dismiss{background:transparent;border:1px solid var(--amber);color:var(--amber);
  font-family:var(--mono);font-size:8px;letter-spacing:0.1em;padding:3px 9px;
  border-radius:2px;cursor:pointer;}
.mp-n-dismiss:hover{background:var(--amber);color:var(--bg);}
.mp-notice-row{display:flex;align-items:center;gap:9px;padding:3px 0;font-size:10px;}
.mp-n-tag{font-size:8px;letter-spacing:0.1em;color:var(--text-dim);
  border:1px solid var(--border);border-radius:2px;padding:1px 5px;}
.mp-n-text{color:var(--text);}
.mp-n-arrow{color:var(--amber);}
.mp-n-ok{color:var(--green);font-size:9px;letter-spacing:0.04em;}
.mp-n-dim{color:var(--text-dim);font-size:9px;letter-spacing:0.04em;}

/* states */
.mp-loading,.mp-empty{flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:10px;color:var(--text-dim);font-size:11px;
  letter-spacing:0.06em;}
.mp-empty-icon{font-size:42px;opacity:0.4;}
.mp-empty-title{font-family:var(--display);font-size:18px;font-weight:700;
  color:var(--text);letter-spacing:0.04em;text-transform:uppercase;}
.mp-empty-body{max-width:340px;text-align:center;line-height:1.6;color:var(--text-dim);}
.mp-spinner{width:26px;height:26px;border:2px solid var(--border);
  border-top-color:var(--blue);border-radius:50%;animation:mpSpin .8s linear infinite;}
@keyframes mpSpin{to{transform:rotate(360deg);}}

/* bottom bar */
#mapperBottomBar{gap:10px;}
.mp-bb-label{font-size:9px;letter-spacing:0.14em;color:var(--text-dim);}
.mp-select{background:var(--bg3);border:1px solid var(--border2);color:var(--text);
  font-family:var(--mono);font-size:11px;padding:5px 9px;border-radius:3px;
  cursor:pointer;outline:none;min-width:170px;}
.mp-select:focus{border-color:var(--blue);}
.mp-bb-sep{width:1px;height:20px;background:var(--border);}
.mp-bb-stat{font-size:13px;font-weight:700;font-family:var(--mono);color:var(--blue);}
.mp-bb-stat span{font-size:9px;font-weight:400;color:var(--text-dim);letter-spacing:0.08em;}
.mp-bb-right{margin-left:auto;display:flex;gap:8px;align-items:center;}
.small-btn.mp-primary{background:var(--blue);border-color:var(--blue);color:#fff;}
.small-btn.mp-primary:hover:not(:disabled){background:var(--blue-dim);}
.small-btn:disabled{opacity:0.4;cursor:not-allowed;}
.mp-client-select{min-width:200px;}
.mp-acting-badge{font-size:9px;letter-spacing:0.1em;padding:3px 8px;
  border:1px solid var(--amber,#f59e0b);color:var(--amber,#f59e0b);
  border-radius:2px;white-space:nowrap;background:rgba(245,158,11,.08);}
.mp-schema-select{background:var(--bg3);border:1px solid var(--border2);color:var(--text);
  font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:0.04em;
  padding:3px 8px;border-radius:2px;cursor:pointer;outline:none;flex:1;
  text-transform:uppercase;}
.mp-schema-select:focus{border-color:var(--blue);}
.mp-port-readonly{cursor:default!important;opacity:0.5;}
.mp-port-readonly:hover{transform:none!important;box-shadow:none!important;}
.mp-readonly-badge{font-size:8px;letter-spacing:0.12em;padding:3px 8px;
  border:1px solid var(--border);color:var(--text-dim);border-radius:2px;}
`;
    document.head.appendChild(style);
}
