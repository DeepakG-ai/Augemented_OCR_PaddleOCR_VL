/* ── Augmented OCR — SPA Core (state, utils, router, init) ──────────── */

const API = (
    window.__AUGMENTED_OCR_API__
    || document.querySelector('meta[name="api-base"]')?.content
    || ''
).replace(/\/$/, '');

// ── SHARED STATE (top-level `let` is shared across <script> tags) ──────
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
let detectedVendorName = null;
let activeFormatType = 'single_po_multipage';
let activePromptInstructions = null;

// ── REVIEW STATE (cross-file: written by extract.js, read by review.js)
let reviewFieldLocations = {};   // {fieldName: {page, box, matched_text, score, strategy}}
let reviewExtractionId = null;
let reviewResult = null;
let reviewPages = [];
let activeMapField = null;       // currently hovered/selected field name

// SSE stream abort handle — owned by extract.js, but router needs to read it
let _activeStreamAbort = null;

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

// ── NAV HELPER (shared by every page render) ───────────────────────────
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
