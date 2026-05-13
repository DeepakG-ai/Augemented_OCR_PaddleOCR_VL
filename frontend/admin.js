/* ── Augmented OCR — Admin: User Management page ────────────────────── */

async function renderAdminUsersPage(app) {
    let users = [];
    try {
        users = await apiJSON('/admin/users');
    } catch (e) {
        showToast('Failed to load users: ' + e.message);
    }

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
            <div class="page-title" style="margin:0">User Management</div>
            <button class="add-vendor-btn" onclick="openCreateUserModal()">+ New User</button>
        </div>
        <div id="adminUserTable">${_renderUserTable(users)}</div>
    </div>
    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${users.length} USER${users.length !== 1 ? 'S' : ''}</span>
    </div>
    ${_createUserModalHTML()}
    ${_resetPasswordModalHTML()}
    ${_pageLimitModalHTML()}`;

    updateNavActive();
}

function _renderUserTable(users) {
    if (!users.length) {
        return '<div style="color:var(--text-dim);padding:20px">No users yet.</div>';
    }
    const currentUser = _currentAuthUser();
    return `
    <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead>
            <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.08em">
                <th style="text-align:left;padding:8px 12px;font-weight:500">EMAIL</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">ROLE</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">STATUS</th>
                <th style="text-align:right;padding:8px 12px;font-weight:500">PAGE LIMIT</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">CREATED</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">ACTIONS</th>
            </tr>
        </thead>
        <tbody>
            ${users.map(u => _renderUserRow(u, currentUser)).join('')}
        </tbody>
    </table>`;
}

function _renderUserRow(u, currentUser) {
    const isSelf = currentUser && u.id === currentUser.id;
    const statusBadge = u.is_active
        ? `<span style="color:var(--green);font-weight:500">ACTIVE</span>`
        : `<span style="color:var(--text-dim)">INACTIVE</span>`;
    const roleBadge = u.role === 'admin'
        ? `<span style="color:var(--blue);font-weight:500">ADMIN</span>`
        : `<span style="color:var(--text-dim)">CLIENT</span>`;
    const deactivateBtn = u.is_active && !isSelf
        ? `<button class="del-btn" onclick="deactivateUser('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}')">Deactivate</button>`
        : `<span style="color:var(--text-dim);font-size:10px">${isSelf ? 'YOU' : '—'}</span>`;
    const resetBtn = !isSelf
        ? `<button class="small-btn" style="margin-left:6px" onclick="openResetPasswordModal('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}')">Reset PW</button>`
        : '';

    const limit = u.subscription_limit != null ? u.subscription_limit : 0;
    const limitDisplay = limit === 0
        ? `<span style="color:var(--red,#e06c75);font-weight:500">NOT SET</span>`
        : `<span style="font-weight:500;font-family:var(--mono)">${Number(limit).toLocaleString()}</span>`;
    const editLimitBtn = u.role === 'client'
        ? `<button class="small-btn" style="margin-left:8px;padding:2px 8px;font-size:9px" onclick="openPageLimitModal('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}',${limit})">Edit</button>`
        : '';

    return `
        <tr style="border-bottom:1px solid var(--border);transition:background 0.15s" onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background=''">
            <td style="padding:10px 12px;font-weight:500">${escapeHtml(u.email)}</td>
            <td style="padding:10px 12px">${roleBadge}</td>
            <td style="padding:10px 12px">${statusBadge}</td>
            <td style="padding:10px 12px;text-align:right">${limitDisplay}${editLimitBtn}</td>
            <td style="padding:10px 12px;color:var(--text-dim)">${u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}</td>
            <td style="padding:10px 12px;white-space:nowrap">${deactivateBtn}${resetBtn}</td>
        </tr>`;
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
    const email    = document.getElementById('newUserEmail').value.trim();
    const password = document.getElementById('newUserPassword').value;
    const confirm  = document.getElementById('newUserConfirmPassword').value;
    const role     = document.getElementById('newUserRole').value;
    const errEl    = document.getElementById('createUserError');

    if (!email)                      { errEl.textContent = 'Email is required.'; return; }
    if (password.length < 8)         { errEl.textContent = 'Password must be at least 8 characters.'; return; }
    if (password !== confirm)        { errEl.textContent = 'Passwords do not match.'; return; }
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
    const newPw   = document.getElementById('resetPwNew').value;
    const confirm = document.getElementById('resetPwConfirm').value;
    const errEl   = document.getElementById('resetPwError');

    if (newPw.length < 8)    { errEl.textContent = 'Password must be at least 8 characters.'; return; }
    if (newPw !== confirm)   { errEl.textContent = 'Passwords do not match.'; return; }
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

    if (raw === '')          { errEl.textContent = 'Page limit is required.'; return; }
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
    const btn   = document.getElementById(btnId);
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

async function _refreshUserTable() {
    try {
        const users = await apiJSON('/admin/users');
        const el = document.getElementById('adminUserTable');
        if (el) el.innerHTML = _renderUserTable(users);
        const bar = document.querySelector('.bottom-bar span');
        if (bar) bar.textContent = `${users.length} USER${users.length !== 1 ? 'S' : ''}`;
    } catch (e) { showToast('Refresh failed: ' + e.message); }
}

function _currentAuthUser() {
    try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch (e) { return null; }
}
