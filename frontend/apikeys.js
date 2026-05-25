/* ── Augmented OCR — Admin: API Key Management page ──────────────────── */

async function renderApiKeysPage(app) {
    let keys = [];
    try {
        keys = await apiJSON('/admin/api-keys');
    } catch (e) {
        showToast('Failed to load API keys: ' + e.message);
    }

    const active = keys.filter(k => k.is_active);
    const inactive = keys.filter(k => !k.is_active);

    app.innerHTML = headerHTML() + `
    <div class="page-content">

        <!-- Header row -->
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px">
            <div>
                <div class="page-title" style="margin:0">API Key Management</div>
                <div style="font-size:10px;color:var(--text-dim);margin-top:4px;letter-spacing:0.06em">
                    ${active.length} ACTIVE &nbsp;·&nbsp; ${inactive.length} INACTIVE
                </div>
            </div>
            <button onclick="openCreateApiKeyModal()" style="
                display:flex;align-items:center;gap:8px;
                background:var(--blue);color:#fff;border:none;cursor:pointer;
                padding:10px 20px;font-family:var(--mono);font-size:11px;
                font-weight:600;letter-spacing:0.1em;border-radius:3px;
                transition:opacity 0.15s" onmouseover="this.style.opacity='.85'" onmouseout="this.style.opacity='1'">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                NEW API KEY
            </button>
        </div>

        <!-- Stats strip -->
        <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:24px">
            ${_akStatCard('Total Keys', keys.length, 'var(--text)')}
            ${_akStatCard('Active', active.length, 'var(--green,#98c379)')}
            ${_akStatCard('Total Tokens', keys.reduce((s,k) => s + (k.total_tokens || 0), 0).toLocaleString(), 'var(--blue)')}
            ${_akStatCard('Total Pages', keys.reduce((s,k) => s + (k.total_pages || 0), 0).toLocaleString(), 'var(--purple,#c678dd)')}
            ${_akStatCard('Total Docs', keys.reduce((s,k) => s + (k.total_documents || 0), 0).toLocaleString(), 'var(--amber,#e5c07b)')}
        </div>

        <!-- Key cards -->
        <div id="apiKeyTable">${_renderApiKeyCards(keys)}</div>
    </div>

    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${keys.length} API KEY${keys.length !== 1 ? 'S' : ''} REGISTERED</span>
    </div>
    ${_createApiKeyModalHTML()}
    ${_showRawKeyModalHTML()}`;

    updateNavActive();
}

function _akStatCard(label, value, color) {
    return `
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:16px 18px">
        <div style="font-size:9px;letter-spacing:0.12em;color:var(--text-dim);margin-bottom:8px">${label.toUpperCase()}</div>
        <div style="font-size:22px;font-weight:700;font-family:var(--mono);color:${color};line-height:1">${value}</div>
    </div>`;
}

function _renderApiKeyCards(keys) {
    if (!keys.length) return '<div style="color:var(--text-dim);padding:20px">No API keys yet. Click NEW API KEY to create one.</div>';
    const sorted = [
        ...keys.filter(k => k.is_active),
        ...keys.filter(k => !k.is_active),
    ];
    return sorted.map(k => _renderApiKeyCard(k)).join('');
}

function _renderApiKeyCard(k) {
    const isActive = k.is_active;
    const opacity = isActive ? '1' : '0.55';

    // Label initials for avatar
    const initials = (k.label || 'AK').slice(0, 2).toUpperCase();
    const avatarColor = isActive ? 'var(--blue)' : 'var(--text-dim)';

    // Status badge
    const statusBadge = isActive
        ? `<span style="display:inline-flex;align-items:center;gap:4px;font-size:9px;font-weight:600;letter-spacing:0.1em;color:var(--green,#98c379)"><span style="width:6px;height:6px;border-radius:50%;background:var(--green,#98c379);display:inline-block"></span>ACTIVE</span>`
        : `<span style="display:inline-flex;align-items:center;gap:4px;font-size:9px;font-weight:600;letter-spacing:0.1em;color:var(--text-dim)"><span style="width:6px;height:6px;border-radius:50%;background:var(--text-dim);display:inline-block"></span>INACTIVE</span>`;

    // Action buttons
    let toggleBtn;
    if (isActive) {
        toggleBtn = `<button onclick="deactivateApiKey(${k.id},'${escapeInlineJsString(k.label)}')"
               style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:6px 12px;font-size:10px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.06em;transition:all 0.15s"
               onmouseover="this.style.borderColor='var(--red,#e06c75)';this.style.color='var(--red,#e06c75)'"
               onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
               Deactivate
           </button>`;
    } else {
        toggleBtn = `<button onclick="reactivateApiKey(${k.id},'${escapeInlineJsString(k.label)}')"
               style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:6px 12px;font-size:10px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.06em;transition:all 0.15s"
               onmouseover="this.style.borderColor='var(--green,#98c379)';this.style.color='var(--green,#98c379)'"
               onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
               Reactivate
           </button>`;
    }

    const deleteBtn = `<button onclick="deleteApiKey(${k.id},'${escapeInlineJsString(k.label)}')"
           title="Permanently delete this API key"
           style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:6px 8px;color:var(--text-dim);display:inline-flex;align-items:center;transition:all 0.15s"
           onmouseover="this.style.borderColor='var(--red,#e06c75)';this.style.color='var(--red,#e06c75)'"
           onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
           <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
       </button>`;

    const lastUsed = k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'Never';
    const tokens = (k.total_tokens || 0).toLocaleString();
    const pages = (k.total_pages || 0).toLocaleString();
    const docs = (k.total_documents || 0).toLocaleString();

    let expiryBadge = '';
    if (k.expires_at) {
        const exp = new Date(k.expires_at);
        const daysLeft = Math.ceil((exp - Date.now()) / 86400000);
        const expired = daysLeft <= 0;
        const expColor = expired ? 'var(--red,#e06c75)' : daysLeft <= 14 ? 'var(--amber,#e5c07b)' : 'var(--text-dim)';
        const expLabel = expired ? 'EXPIRED' : `EXP ${exp.toLocaleDateString()}`;
        expiryBadge = `<span style="font-size:9px;font-weight:600;letter-spacing:0.1em;color:${expColor}">${expLabel}</span>`;
    } else {
        expiryBadge = `<span style="font-size:9px;letter-spacing:0.08em;color:var(--text-dim)">NO EXPIRY</span>`;
    }

    return `
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:18px 20px;
                margin-bottom:8px;display:flex;align-items:center;gap:16px;opacity:${opacity};
                transition:background 0.15s,box-shadow 0.15s"
         onmouseover="this.style.background='var(--bg1)';this.style.boxShadow='0 1px 6px rgba(0,0,0,.12)'"
         onmouseout="this.style.background='var(--bg2)';this.style.boxShadow='none'">

        <!-- Avatar -->
        <div style="width:40px;height:40px;border-radius:50%;background:${avatarColor};
                    display:flex;align-items:center;justify-content:center;
                    font-size:13px;font-weight:700;color:#1e1e1e;flex-shrink:0;letter-spacing:0.05em">
            ${initials}
        </div>

        <!-- Label + meta -->
        <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
                <span style="font-size:13px;font-weight:600;color:var(--text);
                             overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0">
                    ${escapeHtml(k.label)}
                </span>
                <span style="flex-shrink:0;font-size:9px;font-weight:700;letter-spacing:0.12em;
                             color:var(--blue);background:var(--blue-bg,rgba(97,175,239,.1));
                             border:1px solid var(--blue);border-radius:2px;padding:2px 7px">API KEY</span>
            </div>
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
                <span style="font-size:10px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.04em">${escapeHtml(k.prefix)}</span>
                ${statusBadge}
                ${expiryBadge}
                <span style="font-size:9px;color:var(--text-dim);letter-spacing:0.06em">
                    Owner: ${escapeHtml(k.owner_email || '—')}
                </span>
            </div>
        </div>

        <!-- Usage stats -->
        <div style="text-align:center;padding:0 16px;border-left:1px solid var(--border);border-right:1px solid var(--border);min-width:200px">
            <div style="display:flex;gap:16px;justify-content:center">
                <div>
                    <div style="font-size:9px;letter-spacing:0.1em;color:var(--text-dim);margin-bottom:2px">TOKENS</div>
                    <div style="font-size:13px;font-weight:700;font-family:var(--mono);color:var(--text)">${tokens}</div>
                </div>
                <div>
                    <div style="font-size:9px;letter-spacing:0.1em;color:var(--text-dim);margin-bottom:2px">PAGES</div>
                    <div style="font-size:13px;font-weight:700;font-family:var(--mono);color:var(--text)">${pages}</div>
                </div>
                <div>
                    <div style="font-size:9px;letter-spacing:0.1em;color:var(--text-dim);margin-bottom:2px">DOCS</div>
                    <div style="font-size:13px;font-weight:700;font-family:var(--mono);color:var(--text)">${docs}</div>
                </div>
            </div>
            <div style="font-size:9px;color:var(--text-dim);margin-top:4px">Last used: ${lastUsed}</div>
        </div>

        <!-- Actions -->
        <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;min-width:160px;justify-content:flex-end">
            <button onclick="revealApiKey(${k.id},'${escapeInlineJsString(k.label)}')"
                   title="Reveal full API key (admin only)"
                   style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:6px 12px;font-size:10px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.06em;transition:all 0.15s"
                   onmouseover="this.style.borderColor='var(--blue)';this.style.color='var(--blue)'"
                   onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
                   Reveal
            </button>
            ${toggleBtn}
            ${deleteBtn}
        </div>
    </div>`;
}

// ── Create API Key Modal ──────────────────────────────────────────────

function _createApiKeyModalHTML() {
    return `
    <div class="modal-overlay" id="createApiKeyModal">
        <div class="modal">
            <div class="modal-title">Create API Key</div>
            <div class="modal-field">
                <label class="modal-label">Key Name</label>
                <input class="modal-input" id="newApiKeyLabel" type="text" placeholder="e.g. ap_automation" autocomplete="off">
            </div>
            <div class="modal-field" style="margin-top:12px">
                <label class="modal-label">Assign To Client</label>
                <select class="modal-input" id="newApiKeyOwner" style="cursor:pointer">
                    <option value="">Loading users...</option>
                </select>
            </div>
            <div class="modal-field" style="margin-top:12px">
                <label class="modal-label">Expiry</label>
                <select class="modal-input" id="newApiKeyExpiry" style="cursor:pointer">
                    <option value="">No Expiry</option>
                    <option value="30">30 Days</option>
                    <option value="90">90 Days</option>
                    <option value="365">1 Year</option>
                </select>
            </div>
            <div style="font-size:10px;color:var(--text-dim);margin-top:8px;line-height:1.5">
                The key will have access to all vendors belonging to the selected client.
            </div>
            <div id="createApiKeyError" style="color:var(--red,#e06c75);font-size:11px;min-height:16px;margin-top:8px"></div>
            <div class="modal-actions">
                <button class="modal-btn secondary" onclick="closeModal('createApiKeyModal')">Cancel</button>
                <button class="modal-btn primary" id="createApiKeySubmitBtn" onclick="submitCreateApiKey()">Create Key</button>
            </div>
        </div>
    </div>`;
}

function _showRawKeyModalHTML() {
    return `
    <div class="modal-overlay" id="showRawKeyModal">
        <div class="modal">
            <div class="modal-title">API Key Created</div>
            <div style="font-size:11px;color:var(--red,#e06c75);font-weight:600;margin-bottom:12px;letter-spacing:0.06em">
                ⚠ COPY THIS KEY NOW — IT WILL NOT BE SHOWN AGAIN
            </div>
            <div style="background:var(--bg1,#1e1e1e);border:1px solid var(--border);border-radius:4px;padding:14px 16px;position:relative">
                <code id="rawKeyDisplay" style="font-family:var(--mono);font-size:12px;color:var(--green,#98c379);word-break:break-all;line-height:1.6"></code>
                <button onclick="_copyRawKey()" id="copyKeyBtn"
                    style="position:absolute;right:8px;top:8px;background:var(--blue);color:#fff;border:none;cursor:pointer;
                           padding:4px 10px;font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:0.08em;border-radius:2px;
                           transition:opacity 0.15s"
                    onmouseover="this.style.opacity='.85'" onmouseout="this.style.opacity='1'">
                    COPY
                </button>
            </div>
            <div class="modal-actions" style="margin-top:16px">
                <button class="modal-btn primary" onclick="closeModal('showRawKeyModal')">Done</button>
            </div>
        </div>
    </div>
    <div class="modal-overlay" id="revealKeyModal">
        <div class="modal">
            <div class="modal-title">Reveal API Key</div>
            <div style="font-size:10px;color:var(--text-dim);margin-bottom:12px;letter-spacing:0.06em" id="revealKeyLabel"></div>
            <div style="background:var(--bg1,#1e1e1e);border:1px solid var(--border);border-radius:4px;padding:14px 16px;position:relative">
                <code id="revealKeyDisplay" style="font-family:var(--mono);font-size:12px;color:var(--green,#98c379);word-break:break-all;line-height:1.6"></code>
                <button onclick="_copyRevealedKey()" id="copyRevealedBtn"
                    style="position:absolute;right:8px;top:8px;background:var(--blue);color:#fff;border:none;cursor:pointer;
                           padding:4px 10px;font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:0.08em;border-radius:2px;
                           transition:opacity 0.15s"
                    onmouseover="this.style.opacity='.85'" onmouseout="this.style.opacity='1'">
                    COPY
                </button>
            </div>
            <div class="modal-actions" style="margin-top:16px">
                <button class="modal-btn primary" onclick="closeModal('revealKeyModal')">Done</button>
            </div>
        </div>
    </div>`;
}

async function openCreateApiKeyModal() {
    document.getElementById('newApiKeyLabel').value = '';
    document.getElementById('newApiKeyExpiry').value = '';
    document.getElementById('createApiKeyError').textContent = '';

    // Load client users for the owner dropdown
    const sel = document.getElementById('newApiKeyOwner');
    sel.innerHTML = '<option value="">Loading...</option>';
    try {
        const users = await apiJSON('/admin/users');
        const clients = users.filter(u => u.role === 'client' && u.is_active !== false);
        if (!clients.length) {
            sel.innerHTML = '<option value="">No client users found — create one first</option>';
        } else {
            sel.innerHTML = clients.map(u =>
                `<option value="${escapeHtml(u.id)}">${escapeHtml(u.email)}</option>`
            ).join('');
        }
    } catch (e) {
        sel.innerHTML = '<option value="">Failed to load users</option>';
    }

    document.getElementById('createApiKeyModal').classList.add('open');
    setTimeout(() => document.getElementById('newApiKeyLabel').focus(), 50);
}

async function submitCreateApiKey() {
    const label = document.getElementById('newApiKeyLabel').value.trim();
    const errEl = document.getElementById('createApiKeyError');
    const btn = document.getElementById('createApiKeySubmitBtn');

    if (!label) { errEl.textContent = 'Key name is required.'; return; }
    if (label.length < 2) { errEl.textContent = 'Key name must be at least 2 characters.'; return; }
    errEl.textContent = '';

    const owner_user_id = document.getElementById('newApiKeyOwner').value;
    if (!owner_user_id) { errEl.textContent = 'Please select a client user.'; return; }

    const expiryVal = document.getElementById('newApiKeyExpiry').value;
    const expires_days = expiryVal ? parseInt(expiryVal, 10) : null;

    btn.disabled = true;
    btn.textContent = 'Creating...';
    try {
        const result = await apiJSON('/admin/api-keys', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label, owner_user_id, expires_days }),
        });
        closeModal('createApiKeyModal');
        // Show the raw key once
        document.getElementById('rawKeyDisplay').textContent = result.raw_key;
        document.getElementById('showRawKeyModal').classList.add('open');
        showToast(`API key "${label}" created`);
        await _refreshApiKeyTable();
    } catch (e) {
        errEl.textContent = e.message.includes('409')
            ? `A key named "${label}" already exists for this user. Choose a different name.`
            : ('Error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Create Key';
    }
}

function _copyRawKey() {
    const key = document.getElementById('rawKeyDisplay').textContent;
    navigator.clipboard.writeText(key).then(() => {
        const btn = document.getElementById('copyKeyBtn');
        btn.textContent = 'COPIED!';
        setTimeout(() => { btn.textContent = 'COPY'; }, 1500);
    });
}

// ── Actions ───────────────────────────────────────────────────────────

async function deactivateApiKey(keyId, label) {
    if (!confirm(`Deactivate API key "${label}"?\n\nAny client using this key will immediately stop working.`)) return;
    try {
        await apiJSON(`/admin/api-keys/${keyId}/deactivate`, { method: 'PATCH' });
        showToast(`API key "${label}" deactivated`);
        await _refreshApiKeyTable();
    } catch (e) {
        showToast('Failed: ' + e.message);
    }
}

async function reactivateApiKey(keyId, label) {
    if (!confirm(`Reactivate API key "${label}"?`)) return;
    try {
        await apiJSON(`/admin/api-keys/${keyId}/reactivate`, { method: 'PATCH' });
        showToast(`API key "${label}" reactivated`);
        await _refreshApiKeyTable();
    } catch (e) {
        showToast('Failed: ' + e.message);
    }
}

async function deleteApiKey(keyId, label) {
    if (!confirm(`Permanently delete API key "${label}"?\n\nThis cannot be undone.`)) return;
    try {
        await apiJSON(`/admin/api-keys/${keyId}`, { method: 'DELETE' });
        showToast(`API key "${label}" deleted`);
        await _refreshApiKeyTable();
    } catch (e) {
        showToast('Failed: ' + e.message);
    }
}

async function _refreshApiKeyTable() {
    try {
        const keys = await apiJSON('/admin/api-keys');
        const el = document.getElementById('apiKeyTable');
        if (el) el.innerHTML = _renderApiKeyCards(keys);
        const bar = document.querySelector('.bottom-bar span');
        if (bar) bar.textContent = `${keys.length} API KEY${keys.length !== 1 ? 'S' : ''} REGISTERED`;
    } catch (e) { showToast('Refresh failed: ' + e.message); }
}

async function revealApiKey(keyId, label) {
    try {
        const result = await apiJSON(`/admin/api-keys/${keyId}/reveal`);
        document.getElementById('revealKeyLabel').textContent = label;
        document.getElementById('revealKeyDisplay').textContent = result.raw_key;
        document.getElementById('revealKeyModal').classList.add('open');
    } catch (e) {
        showToast('Could not reveal key: ' + e.message);
    }
}

function _copyRevealedKey() {
    const key = document.getElementById('revealKeyDisplay').textContent;
    navigator.clipboard.writeText(key).then(() => {
        const btn = document.getElementById('copyRevealedBtn');
        btn.textContent = 'COPIED!';
        setTimeout(() => { btn.textContent = 'COPY'; }, 1500);
    });
}
