# Frontend SPA — Architecture, Routing, State

> Source files:
> - Core (router + apiFetch + state): [frontend/core.js](../../frontend/core.js)
> - Login: [frontend/login.js](../../frontend/login.js)
> - Vendors / Templates: [frontend/vendors.js](../../frontend/vendors.js)
> - Extraction page: [frontend/extract.js](../../frontend/extract.js)
> - Review page: [frontend/review.js](../../frontend/review.js)
> - History: [frontend/history.js](../../frontend/history.js)
> - Dashboard: [frontend/dashboard.js](../../frontend/dashboard.js)
> - Styles: [frontend/styles.css](../../frontend/styles.css)
> - Index: [frontend/index.html](../../frontend/index.html)

---

## Architectural choices

- **Vanilla JS** — no React, no Vue, no build step. Each `.js` file is a flat script loaded by `index.html`. Edits are immediately live in the browser; no transpile step.
- **Hash-based routing** — URLs like `/#/vendors`, `/#/extract`, `/#/review/123`. No server-side routing config; FastAPI just serves `index.html` for the root.
- **Top-level `let` variables shared across files** — because every script loads into the same global scope, `let foo = ...` in `core.js` is accessible from `extract.js`. This is intentional: it gives us cross-file state without an event bus or store framework.
- **Single render target**: all pages render into `<div id="appRoot">`. Each route's render function calls `app.innerHTML = ...` to replace the whole DOM. No virtual DOM diffing.

---

## Script load order (in `index.html`)

```html
<script src="login.js"></script>     <!-- defines renderLoginPage, getAuthToken, logout -->
<script src="core.js"></script>      <!-- defines apiFetch, router, state vars -->
<script src="vendors.js"></script>   <!-- defines renderVendorsPage, renderTemplatePage -->
<script src="extract.js"></script>   <!-- defines renderExtractPage, runExtract, streamJob -->
<script src="review.js"></script>    <!-- defines renderReviewPage -->
<script src="history.js"></script>
<script src="dashboard.js"></script>
```

Order matters: `core.js` references `renderLoginPage` (defined in `login.js`), so login must load first.

---

## Shared state — the top-level `let` variables (`core.js` lines 9–37)

```javascript
let db = { vendors: [], activeVendorId: null };
let headerFields = [];
let lineItemFields = [];
let extractionRules = [];
let loadedFile = null;            // current file being uploaded
let currentPage = 1;
let totalPages = 1;
let lastResult = null;
let extractionPages = [];         // current document's pages with base64 images
let zoomLevel = 100;
let isDragging = false;
let dragStartX, dragStartY, scrollStartX, scrollStartY;
let activeExtractionId = null;
let activeJobId = null;
let activeExtractButtonId = 'extractBtn';
let detectedVendorName = null;
let activeFormatType = 'single_po_multipage';
let activePromptInstructions = null;

// Cross-file: written by extract.js, read by review.js
let reviewFieldLocations = {};
let reviewExtractionId = null;
let reviewResult = null;
let reviewPages = [];
let activeMapField = null;

let _activeStreamAbort = null;    // SSE AbortController (owned by extract.js)
```

**Implication**: any file can read or write any of these. There's no encapsulation. The convention is:
- `extract.js` owns `loadedFile`, `extractionPages`, the SSE stream lifecycle.
- `review.js` owns `reviewFieldLocations`, `reviewResult` (but reads `reviewExtractionId` set elsewhere).
- `core.js` provides `db.vendors` (cached vendor list) and the `db.activeVendorId`.

This is fragile but very simple. The `logout()` function does a full page reload precisely because there's no good way to enumerate and clear all this state — the cheapest "clear everything" is `location.reload()`.

---

## `apiFetch` — the single network gateway (`core.js` lines 57–73)

```javascript
async function apiFetch(path, opts = {}) {
    const token = localStorage.getItem('auth_token');
    if (token) {
        opts.headers = { ...(opts.headers || {}), 'Authorization': `Bearer ${token}` };
    }
    const res = await fetch(`${API}${path}`, opts);
    if (res.status === 401) {
        // Token missing/expired — clear and bounce to login
        localStorage.removeItem('auth_token');
        localStorage.removeItem('auth_user');
        if (window.location.hash !== '#/login') window.location.hash = '#/login';
        throw new Error('HTTP 401: not authenticated');
    }
    if (!res.ok) { const b = await res.text(); throw new Error(`HTTP ${res.status}: ${b}`); }
    return res;
}

async function apiJSON(path, opts = {}) { return (await apiFetch(path, opts)).json(); }
```

Three responsibilities in nine lines:
1. **Inject Authorization header** (the JWT from localStorage).
2. **Auto-redirect on 401**: clear stale tokens and navigate to login. The `throw` ensures callers get an error and don't continue with stale data.
3. **Throw on non-2xx**: any other failure status produces a meaningful exception.

`apiJSON` is sugar for "expect JSON response" — covers 95% of calls.

**Two cases that bypass `apiFetch`**:
- Login itself (`fetch` directly in `login.js`) — can't use `apiFetch` because there's no token yet, and the 401 redirect would loop.
- SSE streaming (`fetch` with ReadableStream in `extract.js:streamJob`) — needs the response body as a stream, not auto-parsed.

Both replicate the auth-and-401 logic inline.

---

## `router()` — the heart of navigation (`core.js` lines 135–223)

```javascript
async function router() {
    const route = getRoute();        // e.g. "/extract", "/review/123", "/login"
    const app = document.getElementById('appRoot');
    
    // Auth guard — anything other than /login requires a token
    const token = localStorage.getItem('auth_token');
    if (!token && route !== '/login') {
        window.location.hash = '#/login';
        return;
    }
    if (token && route === '/login') {
        window.location.hash = '#/vendors';
        return;
    }
```

**Auth guard** runs first. Two redirects:
- No token + not on login → bounce to login.
- Has token + on login → bounce to default landing (vendors).

The second case prevents a logged-in user from seeing the login form by manually setting the hash.

```javascript
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
```

**Cleanup hooks**: navigating away from a page that owns a long-lived resource (SVG overlay, SSE stream) needs explicit teardown. This is fragile — every new long-lived resource needs a corresponding cleanup branch. A future refactor would attach cleanup to `pageleave` events on each page.

```javascript
    // Update nav active state
    document.querySelectorAll('.nav-tab').forEach(t => {
        t.classList.remove('active');
        if (t.dataset.route && route.startsWith(t.dataset.route)) t.classList.add('active');
    });
    
    if (route === '/login')         { await renderLoginPage(app); return; }
    if (route === '/' || route === '/vendors') { await renderVendorsPage(app); }
    else if (route.startsWith('/template/'))   { await renderTemplatePage(app, vendorId); }
    else if (route === '/saved-templates')     { await renderSavedTemplatesPage(app); }
    else if (route === '/extract')             { await renderExtractPage(app); }
    else if (route === '/history')             { await renderHistoryPage(app); }
    else if (route === '/dashboard')           { await renderDashboardPage(app); }
    else if (route === '/review')              { /* redirect to /review/{lastId} */ }
    else if (route.startsWith('/review/'))     { await renderReviewPage(app, extractionId); }
    else                                        { await renderVendorsPage(app); }
}

window.addEventListener('hashchange', router);
```

`hashchange` fires when the URL hash changes (programmatic via `window.location.hash = ...` or via clicked links). On every change, the entire page is re-rendered. Lightweight enough that the user doesn't notice.

The `/review` (no ID) case is a convenience shortcut: it redirects to the most recently completed extraction's review page so the user can click "Review" in the nav and just go.

---

## Boot sequence (`core.js` lines 273–279)

```javascript
document.addEventListener('DOMContentLoaded', () => {
    if (!window.location.hash) {
        const token = localStorage.getItem('auth_token');
        window.location.hash = token ? '#/vendors' : '#/login';
    }
    router();
});
```

On first load:
1. If no hash, set one based on auth state.
2. Call `router()` which renders the appropriate page.

After this, all navigation flows through `hashchange` events.

---

## Header (`core.js` lines 228–259)

```javascript
function headerHTML() {
    const themeLabel = localStorage.getItem('theme') === 'light' ? 'DARK' : 'LIGHT';
    let user = null;
    try { user = JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch (e) {}
    
    const userBlock = user ? `
        <div ...>
            <span ...>${escapeHtml(user.email)}${user.role === 'admin' ? ' · ADMIN' : ''}</span>
            <button onclick="logout()">Logout</button>
        </div>` : '';
    
    return `
    <header class="header">
        <div class="logo" onclick="navigate('#/')">Augmented <span>OCR</span></div>
        <nav class="nav-tabs">
            <a class="nav-tab" data-route="/vendors">Vendors</a>
            <a class="nav-tab" data-route="/saved-templates">Templates</a>
            <a class="nav-tab" data-route="/extract">Extraction</a>
            <a class="nav-tab" data-route="/history">History</a>
            <a class="nav-tab" data-route="/review">Review</a>
            <a class="nav-tab" data-route="/dashboard">Dashboard</a>
        </nav>
        <div class="header-right">
            <button id="themeToggleBtn">${themeLabel}</button>
            <div ...>system status</div>
            ${userBlock}
        </div>
    </header>`;
}
```

Every page render starts with `app.innerHTML = headerHTML() + ...`. The header is intentionally not a separate component — it's regenerated on every navigation. Keeps things simple, no stale state.

The user block (email + Logout button) appears only if `auth_user` is in localStorage. If `JSON.parse` fails (corrupt value), the catch silently treats the user as logged-out.

---

## Theme toggle (`core.js` lines 41–54)

```javascript
function toggleTheme() {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    if (isLight) {
        document.documentElement.removeAttribute('data-theme');
        localStorage.setItem('theme', 'dark');
    } else {
        document.documentElement.setAttribute('data-theme', 'light');
        localStorage.setItem('theme', 'light');
    }
}

if (localStorage.getItem('theme') === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
}
```

CSS variables in `styles.css` switch on `[data-theme="light"]`. Dark is the default (`:root { --bg: #...; }`); light overrides those vars when the attribute is set.

The bottom block runs at script load — applies the saved theme before the first render so there's no flash of dark on a light-themed reload.

---

## SSE streaming (`extract.js:streamJob` lines 661–729)

```javascript
async function streamJob(jobId) {
    if (_activeStreamAbort) { _activeStreamAbort.abort(); }
    const controller = new AbortController();
    _activeStreamAbort = controller;
    activeJobId = jobId;
    
    const token = localStorage.getItem('auth_token');
    const headers = token ? { 'Authorization': `Bearer ${token}` } : {};
    const response = await fetch(`${API}/jobs/${jobId}/stream`, { signal: controller.signal, headers });
    
    if (response.status === 401) { /* clear + redirect */ }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();    // last partial line stays in buffer
        
        for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith('data: ')) continue;
            const event = JSON.parse(trimmed.slice(6));
            applyJobStatus(event);    // updates pipeline UI
            if (['done', 'failed', 'partial'].includes(event.event)) return event;
        }
    }
}
```

**Why fetch + ReadableStream instead of EventSource?** `EventSource` cannot send custom headers — it only sends URL params and cookies. We use Bearer token in the Authorization header, so we have to use raw `fetch` and parse the SSE wire format ourselves.

The `AbortController` is the cancel mechanism. When the user navigates away, the router calls `_activeStreamAbort.abort()` which closes the connection cleanly (server-side, the FastAPI generator's `await asyncio.sleep` is interrupted).

The buffer trick: each `value` chunk may not align with line boundaries. `split('\n')` and `lines.pop()` keeps the trailing partial line for the next iteration.

---

## File structure summary

| File | What it owns | Lines |
|---|---|---|
| `index.html` | DOM root, script tags, CSP-relevant meta | small |
| `styles.css` | Terminal-themed dark/light theme; do not rewrite | ~1500 |
| `login.js` | Login page, `getAuthToken/User/logout` helpers | 88 |
| `core.js` | Shared state, `apiFetch`, `router`, header, theme | 280 |
| `vendors.js` | Vendor list, vendor template editor, alias mgmt | ~320 |
| `extract.js` | Upload, pipeline viz, SSE, JSON result actions | ~920 |
| `review.js` | Three-panel review, click/drag corrections, save | ~1500+ |
| `history.js` | Past extractions table | small |
| `dashboard.js` | Admin metrics view | small |

`review.js` is the largest file by far. It contains:
- The three-panel layout HTML.
- Field list rendering (with edit-in-place).
- PDF page viewer with overlay SVG.
- Click-to-select word lookup (uses `ocr_data`).
- Drag-to-create-box logic (with shift-modifier, snapping).
- JSON panel with live two-way binding.
- Save flow.

It's a candidate for extraction into smaller files but has been kept as one for now (single source for one page = easier to grep).

---

## Conventions used in render functions

Every page-render follows this template:

```javascript
async function renderXyzPage(app) {
    let data;
    try { data = await apiJSON('/some/path'); } catch (e) { /* handle */ }
    
    app.className = 'app';
    app.innerHTML = headerHTML() + `<...page content...>`;
    
    // wire up event handlers (declarative onclick is fine for SPA)
    // initialize page-specific state
    
    updateNavActive();    // highlight the right nav tab
}
```

- **innerHTML over createElement**: simpler, fewer lines, fast enough for the page sizes here.
- **Inline `onclick="funcName()"`**: works because all top-level functions are global. Easier to read than `addEventListener` for static markup.
- **escapeHtml for any user-controlled string**: see helpers in `core.js`. Mandatory whenever interpolating into innerHTML to prevent XSS.

---

## Helpers (`core.js` lines 84–115)

```javascript
function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function escapeJsString(value) {
    return String(value ?? '')
        .replace(/\\/g, '\\\\').replace(/'/g, "\\'")
        .replace(/\r/g, '\\r').replace(/\n/g, '\\n')
        .replace(/</g, '\\x3C').replace(/>/g, '\\x3E');
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
```

`escapeInlineJsString` is the sneaky one — for cases like `onclick="deleteVendor('${val}')"`. The value goes through both JS-string escaping (so quotes work in the JS context) and HTML escaping (so it's safe in the attribute). Without both, an attacker-controlled value could break out of the string and inject code.

---

## Common pitfalls

1. **Forgetting `escapeHtml` in innerHTML interpolation**: opens XSS. Always escape.
2. **Forgetting to abort old streams**: leaks SSE connections and tangles UI state. Router cleanup handles the navigate-away case; manual cancel handles user-initiated cancel.
3. **Mutating top-level state without re-rendering**: state and DOM drift. Always either call the page's render function again, or update specific DOM nodes explicitly.
4. **Using `apiFetch` for login**: would loop on 401. Use raw `fetch` and handle the response manually.
5. **Relying on `localStorage` for sensitive data**: tokens are vulnerable to XSS. Mitigate with input sanitization (above) and CSP (not yet enforced).

---

## What this design does NOT do

- **No build step.** No webpack, no bundler. Direct file serving.
- **No type system.** Plain JS. Future-proof: easy to migrate to TypeScript if desired.
- **No client-side routing library.** Hand-rolled.
- **No state management library** (Redux, MobX, etc). Direct module-global mutation.
- **No reactive rendering** (React, Vue). Manual `innerHTML` swap.
- **No service worker / offline support.** Always online.
- **No code-splitting.** All scripts loaded on every page (small enough to not matter).
