/* ── Augmented OCR — Admin: User Management page ────────────────────── */

async function renderAdminUsersPage(app) {
    let users = [];
    try {
        users = await apiJSON('/admin/users');
    } catch (e) {
        showToast('Failed to load users: ' + e.message);
    }

    const clients = users.filter(u => u.role === 'client');
    const admins  = users.filter(u => u.role === 'admin');

    app.innerHTML = headerHTML() + `
    <div class="page-content">

        <!-- Header row -->
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px">
            <div>
                <div class="page-title" style="margin:0">User Management</div>
                <div style="font-size:10px;color:var(--text-dim);margin-top:4px;letter-spacing:0.06em">
                    ${clients.length} CLIENT${clients.length !== 1 ? 'S' : ''} &nbsp;·&nbsp; ${admins.length} ADMIN${admins.length !== 1 ? 'S' : ''}
                </div>
            </div>
            <button onclick="openCreateUserModal()" style="
                display:flex;align-items:center;gap:8px;
                background:var(--blue);color:#fff;border:none;cursor:pointer;
                padding:10px 20px;font-family:var(--mono);font-size:11px;
                font-weight:600;letter-spacing:0.1em;border-radius:3px;
                transition:opacity 0.15s" onmouseover="this.style.opacity='.85'" onmouseout="this.style.opacity='1'">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                NEW USER
            </button>
        </div>

        <!-- Stats strip -->
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px">
            ${_userStatCard('Total Users', users.length, 'var(--text)')}
            ${_userStatCard('Active', users.filter(u=>u.is_active).length, 'var(--green)')}
            ${_userStatCard('Clients', clients.length, 'var(--blue)')}
            ${_userStatCard('Total Pages Used', users.reduce((s,u)=>s+(u.pages_extracted||0),0).toLocaleString(), 'var(--amber,#e5c07b)')}
        </div>

        <!-- User cards -->
        <div id="adminUserTable">${_renderUserCards(users)}</div>
    </div>

    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${users.length} USER${users.length !== 1 ? 'S' : ''} REGISTERED</span>
    </div>
    ${_createUserModalHTML()}
    ${_resetPasswordModalHTML()}
    ${_pageLimitModalHTML()}`;

    updateNavActive();
}

function _userStatCard(label, value, color) {
    return `
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:16px 18px">
        <div style="font-size:9px;letter-spacing:0.12em;color:var(--text-dim);margin-bottom:8px">${label.toUpperCase()}</div>
        <div style="font-size:22px;font-weight:700;font-family:var(--mono);color:${color};line-height:1">${value}</div>
    </div>`;
}

function _renderUserCards(users) {
    if (!users.length) return '<div style="color:var(--text-dim);padding:20px">No users yet.</div>';
    const currentUser = _currentAuthUser();

    // Separate clients and admins, clients first
    const sorted = [
        ...users.filter(u => u.role === 'client' &&  u.is_active),
        ...users.filter(u => u.role === 'client' && !u.is_active),
        ...users.filter(u => u.role === 'admin'),
    ];

    return sorted.map(u => _renderUserCard(u, currentUser)).join('');
}

function _renderUserCard(u, currentUser) {
    const isSelf    = currentUser && u.id === currentUser.id;
    const isClient  = u.role === 'client';
    const isActive  = u.is_active;
    const limit     = u.subscription_limit != null ? u.subscription_limit : 0;

    // Avatar initials
    const initials = u.email.split('@')[0].slice(0,2).toUpperCase();
    const avatarColor = isClient
        ? (isActive ? 'var(--blue)' : 'var(--text-dim)')
        : 'var(--green,#98c379)';

    // Role badge
    const roleBadge = u.role === 'admin'
        ? `<span style="font-size:9px;font-weight:700;letter-spacing:0.12em;color:var(--green,#98c379);background:rgba(152,195,121,.12);border:1px solid var(--green,#98c379);border-radius:2px;padding:2px 7px">ADMIN</span>`
        : `<span style="font-size:9px;font-weight:700;letter-spacing:0.12em;color:var(--blue);background:var(--blue-bg,rgba(97,175,239,.1));border:1px solid var(--blue);border-radius:2px;padding:2px 7px">CLIENT</span>`;

    // Status badge
    const statusBadge = isActive
        ? `<span style="display:inline-flex;align-items:center;gap:4px;font-size:9px;font-weight:600;letter-spacing:0.1em;color:var(--green,#98c379)"><span style="width:6px;height:6px;border-radius:50%;background:var(--green,#98c379);display:inline-block"></span>ACTIVE</span>`
        : `<span style="display:inline-flex;align-items:center;gap:4px;font-size:9px;font-weight:600;letter-spacing:0.1em;color:var(--text-dim)"><span style="width:6px;height:6px;border-radius:50%;background:var(--text-dim);display:inline-block"></span>INACTIVE</span>`;

    // Page limit display
    const limitDisplay = !isClient ? '' : limit === 0
        ? `<span style="color:var(--red,#e06c75);font-size:11px;font-weight:600;font-family:var(--mono)">NOT SET</span>`
        : `<span style="font-size:13px;font-weight:700;font-family:var(--mono);color:var(--text)">${Number(limit).toLocaleString()}</span>`;

    // Edit limit button (pencil icon)
    const editLimitBtn = isClient
        ? `<button onclick="openPageLimitModal('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}',${limit})"
               title="Edit page limit"
               style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:4px 7px;color:var(--text-dim);display:inline-flex;align-items:center;gap:4px;font-size:9px;transition:all 0.15s"
               onmouseover="this.style.borderColor='var(--blue)';this.style.color='var(--blue)'"
               onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
               <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
               Edit
           </button>`
        : '';

    // Action buttons
    let deactivateBtn = '';
    if (isSelf) {
        deactivateBtn = `<span style="font-size:9px;letter-spacing:0.12em;color:var(--blue);font-weight:600;padding:6px 0">YOU</span>`;
    } else if (isActive) {
        deactivateBtn = `<button onclick="deactivateUser('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}')"
               style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:6px 12px;font-size:10px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.06em;transition:all 0.15s"
               onmouseover="this.style.borderColor='var(--red,#e06c75)';this.style.color='var(--red,#e06c75)'"
               onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
               Deactivate
           </button>`;
    } else {
        deactivateBtn = `<button onclick="reactivateUser('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}')"
               style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:6px 12px;font-size:10px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.06em;transition:all 0.15s"
               onmouseover="this.style.borderColor='var(--green,#98c379)';this.style.color='var(--green,#98c379)'"
               onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
               Reactivate
           </button>`;
    }

    const resetPwBtn = !isSelf
        ? `<button onclick="openResetPasswordModal('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}')"
               style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:6px 12px;font-size:10px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.06em;display:inline-flex;align-items:center;gap:5px;transition:all 0.15s"
               onmouseover="this.style.borderColor='var(--blue)';this.style.color='var(--blue)'"
               onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
               <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
               Reset PW
           </button>`
        : '';

    const opacity = isActive ? '1' : '0.55';

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

        <!-- Email + meta -->
        <div style="flex:1;min-width:0">
            <div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:4px;
                        overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                ${escapeHtml(u.email)}
                ${isSelf ? `<span style="font-size:9px;font-weight:700;letter-spacing:0.1em;color:var(--blue);margin-left:8px;background:var(--blue-bg,rgba(97,175,239,.1));border:1px solid var(--blue);border-radius:2px;padding:1px 5px">YOU</span>` : ''}
            </div>
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
                ${roleBadge}
                ${statusBadge}
                <span style="font-size:9px;color:var(--text-dim);letter-spacing:0.06em">
                    Joined ${u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}
                </span>
            </div>
        </div>

        <!-- Page limit (clients only) -->
        ${isClient ? `
        <div style="text-align:center;padding:0 16px;border-left:1px solid var(--border);border-right:1px solid var(--border);min-width:100px">
            <div style="font-size:9px;letter-spacing:0.1em;color:var(--text-dim);margin-bottom:4px">PAGE LIMIT</div>
            <div style="display:flex;align-items:center;justify-content:center;gap:8px">
                ${limitDisplay}
                ${editLimitBtn}
            </div>
        </div>` : `<div style="min-width:100px;padding:0 16px;border-left:1px solid var(--border);border-right:1px solid var(--border)"></div>`}

        <!-- Actions -->
        <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;min-width:160px;justify-content:flex-end">
            ${deactivateBtn}
            ${resetPwBtn}
        </div>
    </div>`;
}

// ── Create User Modal ──────────────────────────────────────────────────

function _createUserModalHTML() {
    return `
    <div class="modal-overlay" id="createUserModal">
        <div class="modal">
            <div class="modal-title">Create User</div>
            <div class="modal-field">
                <label class="modal-label">Email</label>
                <input class="modal-input" id="newUserEmail" type="email" placeholder="user@company.com" autocomplete="off">
            </div>
            <div class="modal-field">
                <label class="modal-label">Password</label>
                <div style="position:relative">
                    <input class="modal-input" id="newUserPassword" type="password" placeholder="Minimum 8 characters" autocomplete="new-password" style="padding-right:60px">
                    <button type="button" onclick="_togglePw('newUserPassword','newUserPwToggle')" id="newUserPwToggle"
                        style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:10px;letter-spacing:0.05em;padding:2px 4px">SHOW</button>
                </div>
            </div>
            <div class="modal-field">
                <label class="modal-label">Confirm Password</label>
                <div style="position:relative">
                    <input class="modal-input" id="newUserConfirmPassword" type="password" placeholder="Re-enter password" autocomplete="new-password" style="padding-right:60px">
                    <button type="button" onclick="_togglePw('newUserConfirmPassword','newUserConfirmPwToggle')" id="newUserConfirmPwToggle"
                        style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:10px;letter-spacing:0.05em;padding:2px 4px">SHOW</button>
                </div>
            </div>
            <div class="modal-field">
                <label class="modal-label">Role</label>
                <select class="modal-input" id="newUserRole" style="cursor:pointer">
                    <option value="client">Client</option>
                    <option value="admin">Admin</option>
                </select>
            </div>
            <div id="createUserError" style="color:var(--red,#e06c75);font-size:11px;min-height:16px;margin-top:4px"></div>
            <div class="modal-actions">
                <button class="modal-btn secondary" onclick="closeModal('createUserModal')">Cancel</button>
                <button class="modal-btn primary" onclick="submitCreateUser()">Create</button>
            </div>
        </div>
    </div>`;
}

function openCreateUserModal() {
    document.getElementById('newUserEmail').value = '';
    document.getElementById('newUserPassword').value = '';
    document.getElementById('newUserConfirmPassword').value = '';
    document.getElementById('newUserRole').value = 'client';
    document.getElementById('createUserError').textContent = '';
    document.getElementById('createUserModal').classList.add('open');
    setTimeout(() => document.getElementById('newUserEmail').focus(), 50);
}

async function submitCreateUser() {
    const email = document.getElementById('newUserEmail').value.trim();
    const password = document.getElementById('newUserPassword').value;
    const confirm = document.getElementById('newUserConfirmPassword').value;
    const role = document.getElementById('newUserRole').value;
    const errEl = document.getElementById('createUserError');

    if (!email) { errEl.textContent = 'Email is required.'; return; }
    if (password.length < 8) { errEl.textContent = 'Password must be at least 8 characters.'; return; }
    if (password !== confirm) { errEl.textContent = 'Passwords do not match.'; return; }
    errEl.textContent = '';

    try {
        await apiJSON('/admin/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password, role }),
        });
        closeModal('createUserModal');
        showToast(`User ${email} created`);
        await _refreshUserTable();
    } catch (e) {
        errEl.textContent = e.message.includes('409') ? 'Email already registered.' : ('Error: ' + e.message);
    }
}

// ── Reset Password Modal ───────────────────────────────────────────────

function _resetPasswordModalHTML() {
    return `
    <div class="modal-overlay" id="resetPwModal">
        <div class="modal">
            <div class="modal-title">Reset Password</div>
            <div id="resetPwTarget" style="font-size:11px;color:var(--text-dim);margin-bottom:14px"></div>
            <div class="modal-field">
                <label class="modal-label">New Password</label>
                <div style="position:relative">
                    <input class="modal-input" id="resetPwNew" type="password" placeholder="Minimum 8 characters" autocomplete="new-password" style="padding-right:60px">
                    <button type="button" onclick="_togglePw('resetPwNew','resetPwNewToggle')" id="resetPwNewToggle"
                        style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:10px;letter-spacing:0.05em;padding:2px 4px">SHOW</button>
                </div>
            </div>
            <div class="modal-field">
                <label class="modal-label">Confirm New Password</label>
                <div style="position:relative">
                    <input class="modal-input" id="resetPwConfirm" type="password" placeholder="Re-enter new password" autocomplete="new-password" style="padding-right:60px">
                    <button type="button" onclick="_togglePw('resetPwConfirm','resetPwConfirmToggle')" id="resetPwConfirmToggle"
                        style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:10px;letter-spacing:0.05em;padding:2px 4px">SHOW</button>
                </div>
            </div>
            <div id="resetPwError" style="color:var(--red,#e06c75);font-size:11px;min-height:16px;margin-top:4px"></div>
            <div class="modal-actions">
                <button class="modal-btn secondary" onclick="closeModal('resetPwModal')">Cancel</button>
                <button class="modal-btn primary" onclick="submitResetPassword()">Reset Password</button>
            </div>
        </div>
    </div>`;
}

let _resetPwUserId = null;

function openResetPasswordModal(userId, email) {
    _resetPwUserId = userId;
    document.getElementById('resetPwTarget').textContent = `User: ${email}`;
    document.getElementById('resetPwNew').value = '';
    document.getElementById('resetPwConfirm').value = '';
    document.getElementById('resetPwError').textContent = '';
    document.getElementById('resetPwModal').classList.add('open');
    setTimeout(() => document.getElementById('resetPwNew').focus(), 50);
}

async function submitResetPassword() {
    const newPw = document.getElementById('resetPwNew').value;
    const confirm = document.getElementById('resetPwConfirm').value;
    const errEl = document.getElementById('resetPwError');

    if (newPw.length < 8) { errEl.textContent = 'Password must be at least 8 characters.'; return; }
    if (newPw !== confirm) { errEl.textContent = 'Passwords do not match.'; return; }
    errEl.textContent = '';

    try {
        await apiJSON(`/admin/users/${_resetPwUserId}/password`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ new_password: newPw }),
        });
        closeModal('resetPwModal');
        showToast('Password reset successfully');
    } catch (e) {
        errEl.textContent = 'Error: ' + e.message;
    }
}

// ── Page Limit Modal ──────────────────────────────────────────────────

function _pageLimitModalHTML() {
    return `
    <div class="modal-overlay" id="pageLimitModal">
        <div class="modal">
            <div class="modal-title">Set Page Limit</div>
            <div id="pageLimitTarget" style="font-size:11px;color:var(--text-dim);margin-bottom:14px"></div>
            <div class="modal-field">
                <label class="modal-label">Subscription Page Limit</label>
                <input class="modal-input" id="pageLimitInput" type="number" min="0" step="1" placeholder="e.g. 1000" autocomplete="off"
                    style="font-family:var(--mono);font-size:14px;letter-spacing:0.04em">
            </div>
            <div style="font-size:10px;color:var(--text-dim);margin-top:2px;line-height:1.5">
                Set to <strong>0</strong> to block all uploads. The client can go slightly over this limit
                on their last allowed PDF (soft overage), but the next upload will be blocked.
            </div>
            <div id="pageLimitError" style="color:var(--red,#e06c75);font-size:11px;min-height:16px;margin-top:8px"></div>
            <div class="modal-actions">
                <button class="modal-btn secondary" onclick="closeModal('pageLimitModal')">Cancel</button>
                <button class="modal-btn primary" id="pageLimitSaveBtn" onclick="submitPageLimit()">Save Limit</button>
            </div>
        </div>
    </div>`;
}

let _pageLimitUserId = null;

function openPageLimitModal(userId, email, currentLimit) {
    _pageLimitUserId = userId;
    document.getElementById('pageLimitTarget').textContent = `User: ${email}`;
    document.getElementById('pageLimitInput').value = currentLimit || '';
    document.getElementById('pageLimitError').textContent = '';
    document.getElementById('pageLimitModal').classList.add('open');
    setTimeout(() => document.getElementById('pageLimitInput').focus(), 50);
}

async function submitPageLimit() {
    const input = document.getElementById('pageLimitInput');
    const errEl = document.getElementById('pageLimitError');
    const saveBtn = document.getElementById('pageLimitSaveBtn');
    const raw = input.value.trim();

    if (raw === '') { errEl.textContent = 'Page limit is required.'; return; }
    const limit = parseInt(raw, 10);
    if (isNaN(limit) || limit < 0) { errEl.textContent = 'Must be a non-negative integer.'; return; }
    errEl.textContent = '';

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
    try {
        await apiJSON(`/admin/users/${_pageLimitUserId}/subscription-limit`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subscription_limit: limit }),
        });
        closeModal('pageLimitModal');
        showToast(`Page limit set to ${limit.toLocaleString()}`);
        await _refreshUserTable();
    } catch (e) {
        errEl.textContent = 'Error: ' + e.message;
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save Limit';
    }
}

// ── Shared helpers ─────────────────────────────────────────────────────

function _togglePw(inputId, btnId) {
    const input = document.getElementById(inputId);
    const btn = document.getElementById(btnId);
    if (!input || !btn) return;
    const isHidden = input.type === 'password';
    input.type = isHidden ? 'text' : 'password';
    btn.textContent = isHidden ? 'HIDE' : 'SHOW';
}

async function deactivateUser(userId, email) {
    if (!confirm(`Deactivate "${email}"?\n\nThey will no longer be able to log in.`)) return;
    try {
        await apiJSON(`/admin/users/${userId}`, { method: 'DELETE' });
        showToast(`${email} deactivated`);
        await _refreshUserTable();
    } catch (e) {
        showToast('Failed: ' + e.message);
    }
}

async function reactivateUser(userId, email) {
    if (!confirm(`Reactivate "${email}"?\n\nThey will be able to log in again.`)) return;
    try {
        await apiJSON(`/admin/users/${userId}/reactivate`, { method: 'PATCH' });
        showToast(`${email} reactivated`);
        await _refreshUserTable();
    } catch (e) {
        showToast('Failed: ' + e.message);
    }
}

async function _refreshUserTable() {
    try {
        const users = await apiJSON('/admin/users');
        const el = document.getElementById('adminUserTable');
        if (el) el.innerHTML = _renderUserCards(users);
        const bar = document.querySelector('.bottom-bar span');
        if (bar) bar.textContent = `${users.length} USER${users.length !== 1 ? 'S' : ''}`;
    } catch (e) { showToast('Refresh failed: ' + e.message); }
}

function _currentAuthUser() {
    try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch (e) { return null; }
}
