/* ── Augmented OCR — Admin: User Management page ────────────────────── */

function _adminSubTabsHTML(activeTab) {
    const tabStyle = (active) => `
        padding:7px 18px;font-family:var(--mono);font-size:10px;font-weight:600;
        letter-spacing:0.1em;border:none;cursor:pointer;transition:all 0.15s;
        background:${active ? 'var(--blue)' : 'transparent'};
        color:${active ? '#fff' : 'var(--text-dim)'};`;
    return `
    <div style="display:flex;border:1px solid var(--border);border-radius:3px;overflow:hidden;align-self:flex-end;margin-bottom:2px">
        <button onclick="navigate('#/admin/users')" style="${tabStyle(activeTab === 'users')}">USERS</button>
        <button onclick="navigate('#/admin/api-keys')" style="border-left:1px solid var(--border);${tabStyle(activeTab === 'apikeys')}">API KEYS</button>
        <button onclick="navigate('#/admin/quota-events')" style="border-left:1px solid var(--border);${tabStyle(activeTab === 'quota-events')}">QUOTA ALERTS</button>
    </div>`;
}

async function renderAdminUsersPage(app) {
    let users = [], topupReqs = [];
    try {
        [users, topupReqs] = await Promise.all([
            apiJSON('/admin/users'),
            apiJSON('/admin/topup-requests?status=pending').catch(() => []),
        ]);
    } catch (e) {
        showToast('Failed to load users: ' + e.message);
    }

    const clients = users.filter(u => u.role === 'client');
    const admins  = users.filter(u => u.role === 'admin');

    app.innerHTML = headerHTML() + `
    <div class="page-content">

        <!-- Header row -->
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px">
            <div style="display:flex;align-items:center;gap:20px">
                <div>
                    <div class="page-title" style="margin:0">User Management</div>
                    <div style="font-size:10px;color:var(--text-dim);margin-top:4px;letter-spacing:0.06em">
                        ${clients.length} CLIENT${clients.length !== 1 ? 'S' : ''} &nbsp;·&nbsp; ${admins.length} ADMIN${admins.length !== 1 ? 'S' : ''}
                    </div>
                </div>
                ${_adminSubTabsHTML('users')}
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

        <!-- Top-up Requests notification panel -->
        <div id="topupRequestsPanel">${_renderTopupRequestsPanel(topupReqs)}</div>

        <!-- Stats strip -->
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px">
            ${_userStatCard('Total Users', users.length, 'var(--text)')}
            ${_userStatCard('Active', users.filter(u=>u.is_active).length, 'var(--green)')}
            ${_userStatCard('Clients', clients.length, 'var(--blue)')}
            ${_userStatCard('Total Pages Used',
                users.reduce((s,u)=>s+(u.pages_used||0),0).toLocaleString(),
                'var(--amber,#e5c07b)')}
        </div>

        <!-- User cards -->
        <div id="adminUserTable">${_renderUserCards(users)}</div>
        ${_createUserModalHTML()}
        ${_resetPasswordModalHTML()}
        ${_setPeriodModalHTML()}
        ${_topupModalHTML()}
        ${_userHistoryModalHTML()}
        ${_topupReqRejectModalHTML()}
    </div>

    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">${users.length} USER${users.length !== 1 ? 'S' : ''} REGISTERED</span>
    </div>`;

    updateNavActive();
}


// ── Top-up Request Notification Panel ─────────────────────────────────────

function _renderTopupRequestsPanel(requests) {
    if (!requests || requests.length === 0) return '';

    const rows = requests.map(r => {
        const when = new Date(r.created_at).toLocaleString();
        const pagesLabel = Number(r.requested_pages).toLocaleString();
        return `
        <div id="topup-req-row-${r.id}"
             style="display:flex;align-items:center;gap:12px;padding:10px 14px;
                    border-bottom:1px solid var(--border);flex-wrap:wrap">
            <div style="flex:1;min-width:200px">
                <div style="font-size:11px;font-weight:600;color:var(--text)">${escapeHtml(r.user_email || r.user_id)}</div>
                <div style="font-size:9px;color:var(--text-dim);margin-top:2px;letter-spacing:0.04em">
                    Requests <span style="color:var(--amber,#e5c07b);font-weight:700">${pagesLabel} pages</span>
                    for <span style="color:var(--blue)">${escapeHtml(r.requested_period)}</span>
                    ${r.note ? `· <em style="color:var(--text-dim)">${escapeHtml(r.note)}</em>` : ''}
                </div>
            </div>
            <span style="font-size:9px;color:var(--text-dim);white-space:nowrap">${when}</span>
            <div style="display:flex;gap:6px;flex-shrink:0">
                <button onclick="approveTopupRequest(${r.id},'${escapeInlineJsString(r.user_email||'')}',${r.requested_pages},'${escapeInlineJsString(r.requested_period)}')"
                    style="background:var(--green,#98c379);color:#1e1e1e;border:none;border-radius:3px;
                           cursor:pointer;padding:5px 12px;font-size:9px;font-family:var(--mono);
                           font-weight:700;letter-spacing:0.08em;transition:opacity 0.15s"
                    onmouseover="this.style.opacity='.8'" onmouseout="this.style.opacity='1'">
                    ✓ APPROVE
                </button>
                <button onclick="openRejectTopupModal(${r.id},'${escapeInlineJsString(r.user_email||'')}',${r.requested_pages})"
                    style="background:none;border:1px solid var(--red,#e06c75);color:var(--red,#e06c75);
                           border-radius:3px;cursor:pointer;padding:5px 12px;font-size:9px;
                           font-family:var(--mono);font-weight:600;letter-spacing:0.08em;transition:opacity 0.15s"
                    onmouseover="this.style.opacity='.7'" onmouseout="this.style.opacity='1'">
                    ✕ REJECT
                </button>
            </div>
        </div>`;
    }).join('');

    return `
    <div style="background:rgba(229,192,123,.06);border:1px solid var(--amber,#e5c07b);
                border-radius:4px;margin-bottom:24px;overflow:hidden">
        <div style="display:flex;align-items:center;justify-content:space-between;
                    padding:10px 14px;border-bottom:1px solid rgba(229,192,123,.25)">
            <div style="display:flex;align-items:center;gap:8px">
                <span style="width:8px;height:8px;border-radius:50%;background:var(--amber,#e5c07b);
                             display:inline-block;animation:pulse 1.4s ease-in-out infinite"></span>
                <span style="font-size:9px;font-weight:700;letter-spacing:0.12em;color:var(--amber,#e5c07b)">
                    TOP-UP REQUESTS — ${requests.length} PENDING
                </span>
            </div>
            <button onclick="_refreshTopupRequestsPanel()"
                style="background:none;border:none;color:var(--text-dim);cursor:pointer;
                       font-size:9px;font-family:var(--mono);letter-spacing:0.06em;padding:2px 6px;
                       transition:color 0.15s"
                onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--text-dim)'">
                REFRESH
            </button>
        </div>
        ${rows}
    </div>`;
}

async function _refreshTopupRequestsPanel() {
    try {
        const reqs = await apiJSON('/admin/topup-requests?status=pending');
        const el = document.getElementById('topupRequestsPanel');
        if (el) el.innerHTML = _renderTopupRequestsPanel(reqs);
    } catch (e) {
        showToast('Refresh failed: ' + e.message);
    }
}

async function approveTopupRequest(requestId, userEmail, pages, period) {
    if (!confirm(
        `Approve top-up for ${userEmail}?\n\n` +
        `Pages: ${Number(pages).toLocaleString()}\nPeriod: ${period}\n\n` +
        `The pages will be added to their current active subscription immediately.`
    )) return;

    try {
        await apiJSON(`/admin/topup-requests/${requestId}/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resolution_note: null }),
        });
        showToast(`Approved: +${Number(pages).toLocaleString()} pages for ${userEmail}`);
        await Promise.all([_refreshTopupRequestsPanel(), _refreshUserTable()]);
    } catch (e) {
        showToast('Error: ' + e.message);
    }
}

// ── Reject Top-up Modal ────────────────────────────────────────────────

function _topupReqRejectModalHTML() {
    return `
    <div class="modal-overlay" id="topupReqRejectModal">
        <div class="modal">
            <div class="modal-title">Reject Top-up Request</div>
            <div id="topupReqRejectTarget" style="font-size:11px;color:var(--text-dim);margin-bottom:14px"></div>
            <div class="modal-field">
                <label class="modal-label">Reason (optional)</label>
                <input class="modal-input" id="topupReqRejectNote" type="text" maxlength="500"
                       placeholder="e.g. Please wait until next billing cycle" autocomplete="off">
            </div>
            <div id="topupReqRejectError" style="color:var(--red,#e06c75);font-size:11px;min-height:16px;margin-top:4px"></div>
            <div class="modal-actions">
                <button class="modal-btn secondary" onclick="closeModal('topupReqRejectModal')">Cancel</button>
                <button class="modal-btn primary" id="topupReqRejectBtn"
                        style="background:var(--red,#e06c75)"
                        onclick="submitRejectTopupRequest()">Reject</button>
            </div>
        </div>
    </div>`;
}

let _rejectTopupReqId = null;

function openRejectTopupModal(requestId, userEmail, pages) {
    _rejectTopupReqId = requestId;
    document.getElementById('topupReqRejectTarget').textContent =
        `User: ${userEmail} — ${Number(pages).toLocaleString()} pages`;
    document.getElementById('topupReqRejectNote').value = '';
    document.getElementById('topupReqRejectError').textContent = '';
    document.getElementById('topupReqRejectModal').classList.add('open');
    setTimeout(() => document.getElementById('topupReqRejectNote').focus(), 50);
}

async function submitRejectTopupRequest() {
    const btn = document.getElementById('topupReqRejectBtn');
    const errEl = document.getElementById('topupReqRejectError');
    const note = (document.getElementById('topupReqRejectNote').value || '').trim();

    btn.disabled = true;
    btn.textContent = 'Rejecting…';
    errEl.textContent = '';
    try {
        await apiJSON(`/admin/topup-requests/${_rejectTopupReqId}/reject`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resolution_note: note || null }),
        });
        closeModal('topupReqRejectModal');
        showToast('Top-up request rejected');
        await _refreshTopupRequestsPanel();
    } catch (e) {
        errEl.textContent = 'Error: ' + e.message;
    } finally {
        btn.disabled = false;
        btn.textContent = 'Reject';
    }
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

    // Subscription block (clients only)
    const subscriptionBlock = isClient ? _renderSubscriptionBlock(u) : '';

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

    const hardDeleteBtn = !isSelf
        ? `<button onclick="hardDeleteUser('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}')"
               title="Permanently delete this user"
               style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:6px 8px;color:var(--text-dim);display:inline-flex;align-items:center;transition:all 0.15s"
               onmouseover="this.style.borderColor='var(--red,#e06c75)';this.style.color='var(--red,#e06c75)'"
               onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
               <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
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

        <!-- Subscription block (clients only) -->
        ${subscriptionBlock}

        <!-- Actions -->
        <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;min-width:160px;justify-content:flex-end">
            ${deactivateBtn}
            ${resetPwBtn}
            ${hardDeleteBtn}
        </div>
    </div>`;
}

// Renders the inline subscription panel inside a client card: limit / used
// progress bar, period dates, days remaining, topup count, plus the three
// action buttons (Set Period / Add Topup / History).
function _renderSubscriptionBlock(u) {
    const base       = u.base_limit || 0;
    const topupTotal = u.topup_total || 0;
    const effective  = u.effective_limit || 0;
    const used       = u.pages_used || 0;
    const remaining  = u.pages_remaining || 0;
    const status     = u.period_status || 'none';
    const periodEnd  = u.period_end ? new Date(u.period_end) : null;
    const periodStart = u.period_start ? new Date(u.period_start) : null;

    const noSub = status === 'none' || effective === 0;
    const expired = status === 'expired';
    const percent = effective > 0 ? Math.min(100, Math.round((used / effective) * 100)) : 0;

    // Days-remaining string + color
    let daysStr = '—';
    let daysColor = 'var(--text-dim)';
    if (periodEnd) {
        const ms = periodEnd.getTime() - Date.now();
        const days = Math.ceil(ms / 86400000);
        if (days < 0) { daysStr = 'EXPIRED'; daysColor = 'var(--red,#e06c75)'; }
        else if (days === 0) { daysStr = 'TODAY'; daysColor = 'var(--amber,#e5c07b)'; }
        else if (days <= 14) { daysStr = `${days}d left`; daysColor = 'var(--amber,#e5c07b)'; }
        else { daysStr = `${days}d left`; daysColor = 'var(--text-dim)'; }
    }

    // Progress bar fill color: green → amber → red
    let barColor = 'var(--green,#98c379)';
    if (percent >= 100) barColor = 'var(--red,#e06c75)';
    else if (percent >= 80) barColor = 'var(--amber,#e5c07b)';

    const periodStr = (periodStart && periodEnd)
        ? `${periodStart.toLocaleDateString()} → ${periodEnd.toLocaleDateString()}`
        : 'No active period';

    const topupChip = topupTotal > 0
        ? `<span style="font-size:9px;color:var(--amber,#e5c07b);background:rgba(229,192,123,.10);border:1px solid var(--amber,#e5c07b);border-radius:2px;padding:1px 6px;letter-spacing:0.05em">+${topupTotal.toLocaleString()} TOPUP</span>`
        : '';

    const summaryLine = noSub
        ? `<span style="color:var(--red,#e06c75);font-size:11px;font-weight:600;font-family:var(--mono)">NO SUBSCRIPTION</span>`
        : `<span style="font-size:12px;font-family:var(--mono);color:var(--text);font-weight:600">
              ${used.toLocaleString()} / ${effective.toLocaleString()}
           </span>
           <span style="font-size:10px;color:var(--text-dim);margin-left:4px">pages</span>`;

    const expiredBanner = expired
        ? `<div style="font-size:9px;color:var(--red,#e06c75);letter-spacing:0.08em;margin-top:3px;font-weight:600">⚠ PERIOD EXPIRED — UPLOADS BLOCKED</div>`
        : '';

    return `
    <div style="flex:1;min-width:240px;padding:0 18px;border-left:1px solid var(--border);border-right:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <div style="font-size:9px;letter-spacing:0.1em;color:var(--text-dim)">SUBSCRIPTION</div>
            <div style="font-size:9px;color:${daysColor};letter-spacing:0.06em;font-weight:600">${daysStr}</div>
        </div>
        <div style="display:flex;align-items:baseline;gap:8px">
            ${summaryLine}
            ${topupChip}
        </div>
        ${noSub ? '' : `
        <div style="height:5px;background:var(--bg1,#1e1e1e);border-radius:2px;margin-top:6px;overflow:hidden">
            <div style="height:100%;width:${percent}%;background:${barColor};transition:width .3s"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:5px">
            <span style="font-size:9px;color:var(--text-dim)">${periodStr}</span>
            <span style="font-size:9px;color:var(--text-dim);font-family:var(--mono)">${percent}%</span>
        </div>`}
        ${expiredBanner}
        <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
            <button onclick="openSetPeriodModal('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}',${base},'${u.period_end || ''}')"
                style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:4px 9px;font-size:9px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.06em;transition:all 0.15s"
                onmouseover="this.style.borderColor='var(--blue)';this.style.color='var(--blue)'"
                onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
                ${noSub ? '+ SET PERIOD' : 'SET PERIOD'}
            </button>
            <button onclick="openTopupModal('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}',${remaining})"
                ${noSub ? 'disabled style="opacity:.4;cursor:not-allowed;' : 'style="'}background:none;border:1px solid var(--border);border-radius:3px;${noSub ? '' : 'cursor:pointer;'}padding:4px 9px;font-size:9px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.06em;transition:all 0.15s"
                ${noSub ? '' : `onmouseover="this.style.borderColor='var(--amber,#e5c07b)';this.style.color='var(--amber,#e5c07b)'"
                                onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'"`}>
                + TOPUP
            </button>
            <button onclick="openUserHistoryModal('${escapeInlineJsString(u.id)}','${escapeInlineJsString(u.email)}')"
                style="background:none;border:1px solid var(--border);border-radius:3px;cursor:pointer;padding:4px 9px;font-size:9px;font-family:var(--mono);color:var(--text-dim);letter-spacing:0.06em;transition:all 0.15s"
                onmouseover="this.style.borderColor='var(--blue)';this.style.color='var(--blue)'"
                onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
                HISTORY
            </button>
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

// ── Set Period Modal ─────────────────────────────────────────────────
// Creates a new active subscription (page_limit + period_start + period_end).
// Supersedes any prior active subscription on the server side.

function _setPeriodModalHTML() {
    return `
    <div class="modal-overlay" id="setPeriodModal">
        <div class="modal">
            <div class="modal-title">Set Subscription Period</div>
            <div id="setPeriodTarget" style="font-size:11px;color:var(--text-dim);margin-bottom:14px"></div>
            <div class="modal-field">
                <label class="modal-label">Page Limit</label>
                <input class="modal-input" id="setPeriodLimit" type="number" min="0" step="1" placeholder="e.g. 1000" autocomplete="off"
                    style="font-family:var(--mono);font-size:14px;letter-spacing:0.04em">
            </div>
            <div class="modal-field" style="margin-top:12px">
                <label class="modal-label">Duration</label>
                <select class="modal-input" id="setPeriodDuration" onchange="_setPeriodDurationChanged()" style="cursor:pointer">
                    <option value="30">1 Month</option>
                    <option value="90">3 Months</option>
                    <option value="180">6 Months</option>
                    <option value="365" selected>1 Year</option>
                    <option value="custom">Custom (pick end date)</option>
                </select>
            </div>
            <div class="modal-field" style="margin-top:12px">
                <label class="modal-label">Period Start</label>
                <input class="modal-input" id="setPeriodStart" type="date" autocomplete="off"
                    style="font-family:var(--mono);font-size:12px">
            </div>
            <div class="modal-field" id="setPeriodEndWrap" style="margin-top:12px;display:none">
                <label class="modal-label">Period End</label>
                <input class="modal-input" id="setPeriodEnd" type="date" autocomplete="off"
                    style="font-family:var(--mono);font-size:12px">
            </div>
            <div class="modal-field" style="margin-top:12px">
                <label class="modal-label">Note (optional)</label>
                <input class="modal-input" id="setPeriodNote" type="text" maxlength="500"
                       placeholder="e.g. Annual contract — renewed by John" autocomplete="off">
            </div>
            <div style="font-size:10px;color:var(--text-dim);margin-top:8px;line-height:1.5">
                Creating a new period <strong>replaces</strong> the current active subscription.
                Any unused base pages and top-ups from the previous period are forfeited.
            </div>
            <div id="setPeriodError" style="color:var(--red,#e06c75);font-size:11px;min-height:16px;margin-top:8px"></div>
            <div class="modal-actions">
                <button class="modal-btn secondary" onclick="closeModal('setPeriodModal')">Cancel</button>
                <button class="modal-btn primary" id="setPeriodSaveBtn" onclick="submitSetPeriod()">Save Period</button>
            </div>
        </div>
    </div>`;
}

let _setPeriodUserId = null;

function openSetPeriodModal(userId, email, currentLimit /*, currentEndIso*/) {
    _setPeriodUserId = userId;
    document.getElementById('setPeriodTarget').textContent = `User: ${email}`;
    document.getElementById('setPeriodLimit').value = currentLimit || '';
    document.getElementById('setPeriodDuration').value = '365';
    document.getElementById('setPeriodNote').value = '';
    document.getElementById('setPeriodError').textContent = '';
    const today = new Date();
    document.getElementById('setPeriodStart').value = today.toISOString().slice(0, 10);
    const oneYear = new Date(today.getTime() + 365 * 86400000);
    document.getElementById('setPeriodEnd').value = oneYear.toISOString().slice(0, 10);
    document.getElementById('setPeriodEndWrap').style.display = 'none';
    document.getElementById('setPeriodModal').classList.add('open');
    setTimeout(() => document.getElementById('setPeriodLimit').focus(), 50);
}

function _setPeriodDurationChanged() {
    const v = document.getElementById('setPeriodDuration').value;
    document.getElementById('setPeriodEndWrap').style.display = v === 'custom' ? 'block' : 'none';
}

async function submitSetPeriod() {
    const errEl = document.getElementById('setPeriodError');
    const saveBtn = document.getElementById('setPeriodSaveBtn');
    const limit = parseInt(document.getElementById('setPeriodLimit').value.trim(), 10);
    if (isNaN(limit) || limit < 0) { errEl.textContent = 'Page limit must be a non-negative integer.'; return; }

    const startStr = document.getElementById('setPeriodStart').value;
    if (!startStr) { errEl.textContent = 'Period start is required.'; return; }
    const start = new Date(startStr + 'T00:00:00Z');

    const durationVal = document.getElementById('setPeriodDuration').value;
    let end;
    if (durationVal === 'custom') {
        const endStr = document.getElementById('setPeriodEnd').value;
        if (!endStr) { errEl.textContent = 'Custom end date is required.'; return; }
        end = new Date(endStr + 'T23:59:59Z');
    } else {
        end = new Date(start.getTime() + parseInt(durationVal, 10) * 86400000);
    }
    if (end <= start) { errEl.textContent = 'End must be after start.'; return; }
    errEl.textContent = '';

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
    try {
        await apiJSON(`/admin/users/${_setPeriodUserId}/subscriptions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                page_limit: limit,
                period_start: start.toISOString(),
                period_end: end.toISOString(),
                note: document.getElementById('setPeriodNote').value.trim() || null,
            }),
        });
        closeModal('setPeriodModal');
        showToast(`Subscription set: ${limit.toLocaleString()} pages until ${end.toLocaleDateString()}`);
        await _refreshUserTable();
    } catch (e) {
        errEl.textContent = 'Error: ' + e.message;
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save Period';
    }
}


// ── Top-up Modal ─────────────────────────────────────────────────────
// Grants extra pages to the user's current active subscription.

function _topupModalHTML() {
    return `
    <div class="modal-overlay" id="topupModal">
        <div class="modal">
            <div class="modal-title">Add Top-up Pages</div>
            <div id="topupTarget" style="font-size:11px;color:var(--text-dim);margin-bottom:6px"></div>
            <div id="topupRemaining" style="font-size:10px;color:var(--text-dim);margin-bottom:14px"></div>
            <div class="modal-field">
                <label class="modal-label">Extra Pages</label>
                <input class="modal-input" id="topupPages" type="number" min="1" step="1" placeholder="e.g. 500" autocomplete="off"
                    style="font-family:var(--mono);font-size:14px;letter-spacing:0.04em">
            </div>
            <div class="modal-field" style="margin-top:12px">
                <label class="modal-label">Note (optional)</label>
                <input class="modal-input" id="topupNote" type="text" maxlength="500"
                       placeholder="e.g. Paid invoice INV-1234" autocomplete="off">
            </div>
            <div style="font-size:10px;color:var(--text-dim);margin-top:8px;line-height:1.5">
                Top-ups expire with the current subscription period — unused pages are forfeited
                when the period ends.
            </div>
            <div id="topupError" style="color:var(--red,#e06c75);font-size:11px;min-height:16px;margin-top:8px"></div>
            <div class="modal-actions">
                <button class="modal-btn secondary" onclick="closeModal('topupModal')">Cancel</button>
                <button class="modal-btn primary" id="topupSaveBtn" onclick="submitTopup()">Add Top-up</button>
            </div>
        </div>
    </div>`;
}

let _topupUserId = null;

function openTopupModal(userId, email, remaining) {
    _topupUserId = userId;
    document.getElementById('topupTarget').textContent = `User: ${email}`;
    document.getElementById('topupRemaining').textContent =
        `Current remaining: ${Number(remaining || 0).toLocaleString()} pages`;
    document.getElementById('topupPages').value = '';
    document.getElementById('topupNote').value = '';
    document.getElementById('topupError').textContent = '';
    document.getElementById('topupModal').classList.add('open');
    setTimeout(() => document.getElementById('topupPages').focus(), 50);
}

async function submitTopup() {
    const errEl = document.getElementById('topupError');
    const saveBtn = document.getElementById('topupSaveBtn');
    const pages = parseInt(document.getElementById('topupPages').value.trim(), 10);
    if (isNaN(pages) || pages <= 0) { errEl.textContent = 'Pages must be a positive integer.'; return; }
    errEl.textContent = '';

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
    try {
        await apiJSON(`/admin/users/${_topupUserId}/topups`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                pages,
                note: document.getElementById('topupNote').value.trim() || null,
            }),
        });
        closeModal('topupModal');
        showToast(`Added ${pages.toLocaleString()} top-up pages`);
        await _refreshUserTable();
    } catch (e) {
        if (e.message.includes('409')) {
            errEl.textContent = 'No active subscription — create a period first.';
        } else {
            errEl.textContent = 'Error: ' + e.message;
        }
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Add Top-up';
    }
}


// ── User History Modal ───────────────────────────────────────────────
// Chronological table of every subscription period + every top-up for a
// single user. Built on demand from GET /admin/users/{id}/history.

function _userHistoryModalHTML() {
    return `
    <div class="modal-overlay" id="userHistoryModal">
        <div class="modal" style="max-width:880px;width:90vw">
            <div class="modal-title">Subscription History</div>
            <div id="userHistoryTarget" style="font-size:11px;color:var(--text-dim);margin-bottom:14px"></div>
            <div id="userHistoryBody" style="max-height:60vh;overflow-y:auto">
                <div style="padding:20px;color:var(--text-dim);font-size:11px">Loading…</div>
            </div>
            <div class="modal-actions">
                <button class="modal-btn primary" onclick="closeModal('userHistoryModal')">Close</button>
            </div>
        </div>
    </div>`;
}

async function openUserHistoryModal(userId, email) {
    document.getElementById('userHistoryTarget').textContent = `User: ${email}`;
    document.getElementById('userHistoryBody').innerHTML =
        '<div style="padding:20px;color:var(--text-dim);font-size:11px">Loading…</div>';
    document.getElementById('userHistoryModal').classList.add('open');
    try {
        const data = await apiJSON(`/admin/users/${userId}/history`);
        document.getElementById('userHistoryBody').innerHTML = _renderUserHistory(data);
    } catch (e) {
        document.getElementById('userHistoryBody').innerHTML =
            `<div style="padding:20px;color:var(--red,#e06c75);font-size:11px">Failed to load: ${escapeHtml(e.message)}</div>`;
    }
}

function _renderUserHistory(data) {
    const subs  = data.subscriptions || [];
    const tops  = data.topups || [];

    // Build one merged chronological event list. Each row: when, type,
    // pages, period window, status, note, admin.
    const events = [];
    for (const s of subs) {
        events.push({
            ts:       s.created_at,
            type:     'subscription',
            pages:    s.page_limit,
            from:     s.period_start,
            to:       s.period_end,
            status:   s.status,
            note:     s.note || '',
            admin:    s.created_by_email || '—',
            extra:    `Used ${Number(s.pages_used || 0).toLocaleString()} · Topups +${Number(s.topup_total || 0).toLocaleString()}`,
        });
    }
    for (const t of tops) {
        events.push({
            ts:       t.created_at,
            type:     'topup',
            pages:    t.pages,
            from:     t.sub_period_start,
            to:       t.sub_period_end,
            status:   t.sub_status,
            note:     t.note || '',
            admin:    t.created_by_email || '—',
            extra:    `On subscription #${t.subscription_id}`,
        });
    }
    events.sort((a, b) => new Date(b.ts) - new Date(a.ts));

    if (!events.length) {
        return '<div style="padding:20px;color:var(--text-dim);font-size:11px;text-align:center">No subscription history yet.</div>';
    }

    const statBlock = `
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px">
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:3px;padding:10px 12px">
            <div style="font-size:9px;letter-spacing:0.1em;color:var(--text-dim);margin-bottom:4px">SUBSCRIPTIONS</div>
            <div style="font-size:16px;font-weight:700;font-family:var(--mono);color:var(--blue)">${subs.length}</div>
        </div>
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:3px;padding:10px 12px">
            <div style="font-size:9px;letter-spacing:0.1em;color:var(--text-dim);margin-bottom:4px">TOP-UPS</div>
            <div style="font-size:16px;font-weight:700;font-family:var(--mono);color:var(--amber,#e5c07b)">${tops.length}</div>
        </div>
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:3px;padding:10px 12px">
            <div style="font-size:9px;letter-spacing:0.1em;color:var(--text-dim);margin-bottom:4px">EXTRA PAGES GRANTED</div>
            <div style="font-size:16px;font-weight:700;font-family:var(--mono);color:var(--amber,#e5c07b)">+${tops.reduce((s,t)=>s+(t.pages||0),0).toLocaleString()}</div>
        </div>
    </div>`;

    const cols = '110px 90px 80px 180px 90px 1fr 120px';
    const rowStyle = `display:grid;grid-template-columns:${cols};align-items:center;gap:8px`;

    const header = `
    <div style="${rowStyle};padding:8px 10px;border-bottom:2px solid var(--border);
                font-size:9px;letter-spacing:0.1em;color:var(--text-dim);position:sticky;top:0;background:var(--bg1,#1e1e1e);z-index:1">
        <span>WHEN</span>
        <span>TYPE</span>
        <span style="text-align:right">PAGES</span>
        <span>PERIOD</span>
        <span>STATUS</span>
        <span>NOTE</span>
        <span>ADMIN</span>
    </div>`;

    const rows = events.map(ev => {
        const when = new Date(ev.ts).toLocaleString();
        const typeBadge = ev.type === 'subscription'
            ? `<span style="font-size:9px;font-weight:700;letter-spacing:0.1em;color:var(--blue);background:var(--blue-bg,rgba(97,175,239,.1));border:1px solid var(--blue);border-radius:2px;padding:1px 6px">SUB</span>`
            : `<span style="font-size:9px;font-weight:700;letter-spacing:0.1em;color:var(--amber,#e5c07b);background:rgba(229,192,123,.10);border:1px solid var(--amber,#e5c07b);border-radius:2px;padding:1px 6px">TOPUP</span>`;
        const pageStr = ev.type === 'topup'
            ? `+${Number(ev.pages).toLocaleString()}`
            : Number(ev.pages).toLocaleString();
        const pageColor = ev.type === 'topup' ? 'var(--amber,#e5c07b)' : 'var(--text)';
        const periodStr = (ev.from && ev.to)
            ? `${new Date(ev.from).toLocaleDateString()} → ${new Date(ev.to).toLocaleDateString()}`
            : '—';
        const statusColor = {
            active:     'var(--green,#98c379)',
            expired:    'var(--text-dim)',
            cancelled:  'var(--red,#e06c75)',
            superseded: 'var(--text-dim)',
        }[ev.status] || 'var(--text-dim)';

        return `
        <div style="${rowStyle};padding:8px 10px;border-bottom:1px solid var(--border);font-size:10px;
                    transition:background .12s"
             onmouseover="this.style.background='var(--bg2)'"
             onmouseout="this.style.background='transparent'">
            <span style="color:var(--text-dim);font-family:var(--mono)">${when}</span>
            <span>${typeBadge}</span>
            <span style="text-align:right;font-family:var(--mono);font-weight:600;color:${pageColor}">${pageStr}</span>
            <span style="font-family:var(--mono);color:var(--text-dim);font-size:9px">${periodStr}</span>
            <span style="text-transform:uppercase;font-size:9px;letter-spacing:0.08em;color:${statusColor};font-weight:600">${ev.status || '—'}</span>
            <span style="color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(ev.note)}">${escapeHtml(ev.note || ev.extra || '')}</span>
            <span style="color:var(--text-dim);font-size:9px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(ev.admin)}">${escapeHtml(ev.admin)}</span>
        </div>`;
    }).join('');

    return statBlock + header + rows;
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

async function hardDeleteUser(userId, email) {
    if (!confirm(`Permanently delete "${email}"?\n\nThis cannot be undone. All their data will remain but the account will be gone.`)) return;
    try {
        await apiJSON(`/admin/users/${userId}/hard`, { method: 'DELETE' });
        showToast(`${email} permanently deleted`);
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

// ── Admin: Spatial Memory (Manual Corrections) Management ────────────────

let _smEntries = [];
let _smTotal = 0;
const _smPageSize = 200;
let _smOffset = 0;
let _smFilter = '';

async function renderAdminSpatialMemoryPage(app) {
    _smOffset = 0;
    _smFilter = '';
    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px">
            <div>
                <div class="page-title" style="margin:0">Saved Regions</div>
                <div style="font-size:10px;color:var(--text-dim);margin-top:4px;letter-spacing:0.06em">
                    Manual corrections stored as spatial memory — grouped by client
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:10px">
                <input id="smFilterInput" type="text" placeholder="Filter by client, vendor or field…"
                    value="${escapeHtml(_smFilter)}"
                    oninput="_smApplyFilter(this.value)"
                    style="background:var(--bg2);border:1px solid var(--border);color:var(--text);
                           padding:7px 12px;font-family:var(--mono);font-size:11px;border-radius:3px;width:260px"/>
                <button onclick="_smReload()" style="
                    background:var(--bg2);border:1px solid var(--border);color:var(--text);
                    cursor:pointer;padding:8px 16px;font-family:var(--mono);font-size:11px;
                    letter-spacing:0.08em;border-radius:3px;transition:opacity .15s"
                    onmouseover="this.style.opacity='.7'" onmouseout="this.style.opacity='1'">
                    REFRESH
                </button>
            </div>
        </div>
        <div id="smStatsStrip" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px"></div>
        <div id="smTable"></div>
        <div id="smPager" style="display:flex;gap:10px;align-items:center;padding:12px 0;font-size:10px;color:var(--text-dim)"></div>
    </div>`;
    updateNavActive();
    await _smReload();
}

async function _smReload() {
    const tableEl = document.getElementById('smTable');
    const statsEl = document.getElementById('smStatsStrip');
    if (tableEl) tableEl.innerHTML = '<div style="padding:16px;font-size:10px;color:var(--text-dim)">Loading…</div>';
    try {
        const resp = await apiJSON(`/admin/spatial-memory?limit=${_smPageSize}&offset=${_smOffset}`);
        _smEntries = resp.entries || [];
        _smTotal = resp.total || 0;
        _smUpdateStats(statsEl, _smEntries, _smTotal);
        _smRenderTable();
        _smRenderPager();
    } catch (e) {
        if (tableEl) tableEl.innerHTML = `<div style="padding:16px;font-size:11px;color:var(--red)">Failed to load: ${escapeHtml(e.message)}</div>`;
    }
}

function _smUpdateStats(statsEl, entries, total) {
    if (!statsEl) return;
    const clients  = new Set(entries.map(e => e.client_email || '(unassigned)')).size;
    const vendors  = new Set(entries.map(e => e.vendor_id)).size;
    const shown    = entries.length;
    statsEl.innerHTML = [
        _smStatCard('Total Saved Regions', total,   'var(--text)'),
        _smStatCard('Clients',             clients,  'var(--blue)'),
        _smStatCard('Vendors',             vendors,  'var(--green)'),
        _smStatCard('Shown',               shown,    'var(--text-dim)'),
    ].join('');
}

function _smStatCard(label, value, color) {
    return `
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:14px 18px">
        <div style="font-size:18px;font-weight:700;color:${color};font-family:var(--mono)">${value}</div>
        <div style="font-size:9px;color:var(--text-dim);margin-top:3px;letter-spacing:0.08em">${label}</div>
    </div>`;
}

function _smApplyFilter(val) {
    _smFilter = val.toLowerCase();
    _smRenderTable();
}

function _smRenderTable() {
    const tableEl = document.getElementById('smTable');
    if (!tableEl) return;

    const q = _smFilter;
    const filtered = q
        ? _smEntries.filter(e =>
            (e.client_email || '').toLowerCase().includes(q) ||
            (e.vendor_name  || '').toLowerCase().includes(q) ||
            (e.vendor_id    || '').toLowerCase().includes(q) ||
            (e.field_key    || '').toLowerCase().includes(q))
        : _smEntries;

    if (!filtered.length) {
        tableEl.innerHTML = '<div style="padding:20px;font-size:11px;color:var(--text-dim);text-align:center">No saved regions found.</div>';
        return;
    }

    // Each row is its own full-width grid — NO outer grid wrapper.
    // Columns: CLIENT | VENDOR | FIELD | LAYOUT KEY | PAGE | SOURCE | VERIFIED | ACTION
    const cols = '1fr 1fr 140px 140px 48px 90px 105px 80px';
    const rowStyle = `display:grid;grid-template-columns:${cols};align-items:center`;

    const header = `
    <div style="${rowStyle};border-bottom:2px solid var(--border);padding:6px 0;
                font-size:9px;letter-spacing:0.1em;color:var(--text-dim);">
        <span style="padding:0 8px">CLIENT</span>
        <span style="padding:0 8px">VENDOR</span>
        <span style="padding:0 8px">FIELD</span>
        <span style="padding:0 8px">LAYOUT KEY</span>
        <span style="padding:0 8px">PAGE</span>
        <span style="padding:0 8px">SOURCE</span>
        <span style="padding:0 8px">LAST VERIFIED</span>
        <span style="padding:0 8px">ACTION</span>
    </div>`;

    let lastClient = null;
    const rows = filtered.map(e => {
        const ts     = e.last_verified_at ? new Date(e.last_verified_at).toLocaleDateString() : '—';
        const src    = (e.source_engine || '').replace('paddleocr', 'PaddleOCR').replace('pypdfium', 'PDF');
        const vname  = e.vendor_name || e.vendor_id;
        const client = e.client_email || '(unassigned)';

        // Plain full-width divider — NOT inside any grid, so it always spans 100%
        let groupHeader = '';
        if (client !== lastClient) {
            lastClient = client;
            groupHeader = `
            <div style="background:var(--bg2);border-top:2px solid var(--border);
                        border-bottom:1px solid var(--border);padding:6px 12px;
                        font-size:9px;letter-spacing:0.08em;color:var(--blue);font-weight:600;">
                👤 ${escapeHtml(client)}
            </div>`;
        }

        return groupHeader + `
        <div id="sm-admin-row-${e.id}"
             style="${rowStyle};padding:7px 0;border-bottom:1px solid var(--border);
                    transition:background .12s;"
             onmouseover="this.style.background='var(--bg2)'"
             onmouseout="this.style.background='transparent'">
            <span style="padding:0 8px;font-size:9px;color:var(--text-dim);
                         overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                  title="${escapeHtml(client)}">${escapeHtml(client)}</span>
            <span style="padding:0 8px;font-size:10px;color:var(--text);
                         overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                  title="${escapeHtml(e.vendor_id)}">${escapeHtml(vname)}</span>
            <span style="padding:0 8px;font-size:10px;font-weight:600;color:var(--text)">${escapeHtml(e.field_key)}</span>
            <span style="padding:0 8px;font-size:9px;color:var(--text-dim);
                         overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                  title="${escapeHtml(e.layout_key)}">${escapeHtml(e.layout_key)}</span>
            <span style="padding:0 8px;font-size:10px;color:var(--text)">${e.page_number}</span>
            <span style="padding:0 8px;font-size:9px;color:var(--text-dim)">${src}</span>
            <span style="padding:0 8px;font-size:9px;color:var(--text-dim)">${ts}</span>
            <span style="padding:0 8px">
                <button onclick="_smAdminDelete(${e.id},'${escapeJsString(e.field_key)}','${escapeJsString(vname)}','${escapeJsString(client)}')"
                    style="background:none;border:1px solid var(--red,#e06c75);color:var(--red,#e06c75);
                           cursor:pointer;padding:3px 8px;font-size:9px;font-family:var(--mono);
                           border-radius:2px;letter-spacing:0.06em;transition:opacity .15s"
                    onmouseover="this.style.opacity='.7'" onmouseout="this.style.opacity='1'">
                    DEL
                </button>
            </span>
        </div>`;
    }).join('');

    // No outer grid — header and rows are independent full-width blocks
    tableEl.innerHTML = header + rows;
}

function _smRenderPager() {
    const el = document.getElementById('smPager');
    if (!el) return;
    const pages = Math.ceil(_smTotal / _smPageSize);
    const current = Math.floor(_smOffset / _smPageSize) + 1;
    if (pages <= 1) { el.innerHTML = ''; return; }
    el.innerHTML = `
        <button onclick="_smGoPage(${_smOffset - _smPageSize})" ${_smOffset === 0 ? 'disabled' : ''}
            style="background:var(--bg2);border:1px solid var(--border);color:var(--text);
                   padding:4px 10px;font-family:var(--mono);font-size:9px;cursor:pointer;border-radius:2px">
            ← PREV
        </button>
        <span>PAGE ${current} / ${pages} &nbsp;·&nbsp; ${_smTotal} TOTAL</span>
        <button onclick="_smGoPage(${_smOffset + _smPageSize})" ${current >= pages ? 'disabled' : ''}
            style="background:var(--bg2);border:1px solid var(--border);color:var(--text);
                   padding:4px 10px;font-family:var(--mono);font-size:9px;cursor:pointer;border-radius:2px">
            NEXT →
        </button>`;
}

async function _smGoPage(newOffset) {
    _smOffset = Math.max(0, newOffset);
    await _smReload();
}

/* ── Admin: Quota Grace Events ──────────────────────────────────────────── */

async function renderAdminQuotaEventsPage(app) {
    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px">
            <div style="display:flex;align-items:center;gap:20px">
                <div>
                    <div class="page-title" style="margin:0">Quota Alerts</div>
                    <div style="font-size:10px;color:var(--text-dim);margin-top:4px;letter-spacing:0.06em">
                        Grace overages and hard blocks across all clients
                    </div>
                </div>
                ${_adminSubTabsHTML('quota-events')}
            </div>
        </div>
        <div id="quotaEventsContent">
            <div style="padding:16px;font-size:10px;color:var(--text-dim)">Loading…</div>
        </div>
    </div>`;
    updateNavActive();

    try {
        const events = await apiJSON('/admin/quota-events?limit=200');
        _renderQuotaEvents(events);
    } catch (e) {
        const el = document.getElementById('quotaEventsContent');
        if (el) el.innerHTML = `<div style="padding:16px;font-size:11px;color:var(--red)">Failed to load: ${escapeHtml(e.message)}</div>`;
    }
}

function _renderQuotaEvents(events) {
    const el = document.getElementById('quotaEventsContent');
    if (!el) return;

    if (!events.length) {
        el.innerHTML = '<div style="padding:20px;font-size:11px;color:var(--text-dim);text-align:center">No quota events recorded yet.</div>';
        return;
    }

    const graceCount    = events.filter(e => e.event_type === 'grace_used').length;
    const exceededCount = events.filter(e => e.event_type === 'exceeded').length;
    const clientSet     = new Set(events.map(e => e.email || ''));
    const totalGracePages = events.reduce((s, e) => s + (parseInt(e.grace_pages_used, 10) || 0), 0);

    const statCard = (label, val, color) => `
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:14px 18px">
        <div style="font-size:18px;font-weight:700;color:${color};font-family:var(--mono)">${val}</div>
        <div style="font-size:9px;color:var(--text-dim);margin-top:3px;letter-spacing:0.08em">${label}</div>
    </div>`;

    const cols = '1.6fr 120px 100px 100px 100px 1.2fr';
    const rowStyle = `display:grid;grid-template-columns:${cols};align-items:center`;

    const header = `
    <div style="${rowStyle};border-bottom:2px solid var(--border);padding:6px 0;
                font-size:9px;letter-spacing:0.1em;color:var(--text-dim);">
        <span style="padding:0 8px">CLIENT</span>
        <span style="padding:0 8px">TYPE</span>
        <span style="padding:0 8px">GRACE USED</span>
        <span style="padding:0 8px">UPLOADED</span>
        <span style="padding:0 8px">USED / LIMIT</span>
        <span style="padding:0 8px">FILE</span>
    </div>`;

    const rows = events.map(e => {
        // Coerce server numbers to safe integers before interpolating into HTML.
        const graceUsed    = parseInt(e.grace_pages_used, 10) || 0;
        const incomingPgs  = parseInt(e.incoming_pages,   10) || 0;
        const usedBefore   = parseInt(e.used_before,      10) || 0;
        const limitAtTime  = parseInt(e.limit_at_time,    10) || 0;

        const ts      = e.event_ts ? new Date(e.event_ts).toLocaleString() : '—';
        const isGrace = e.event_type === 'grace_used';
        const typeBadge = isGrace
            ? `<span style="background:rgba(229,192,123,0.15);color:var(--amber,#e5c07b);border:1px solid rgba(229,192,123,0.3);border-radius:2px;padding:2px 7px;font-size:9px;font-weight:600;letter-spacing:0.08em">GRACE</span>`
            : `<span style="background:rgba(224,108,117,0.12);color:var(--red,#e06c75);border:1px solid rgba(224,108,117,0.25);border-radius:2px;padding:2px 7px;font-size:9px;font-weight:600;letter-spacing:0.08em">BLOCKED</span>`;
        const graceCell = isGrace
            ? `<span style="color:var(--amber,#e5c07b);font-weight:600">${graceUsed} pg</span>`
            : `<span style="color:var(--text-dim)">—</span>`;
        const fname = e.filename
            ? `<span title="${escapeHtml(e.filename)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:9px;color:var(--text-dim)">${escapeHtml(e.filename)}</span>`
            : `<span style="color:var(--text-dim)">—</span>`;

        return `
        <div style="${rowStyle};padding:8px 0;border-bottom:1px solid var(--border);transition:background .12s"
             onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background='transparent'">
            <div style="padding:0 8px">
                <div style="font-size:10px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                     title="${escapeHtml(e.email || '')}">${escapeHtml(e.email || '—')}</div>
                <div style="font-size:9px;color:var(--text-dim);margin-top:1px">${escapeHtml(ts)}</div>
            </div>
            <span style="padding:0 8px">${typeBadge}</span>
            <span style="padding:0 8px;font-size:10px">${graceCell}</span>
            <span style="padding:0 8px;font-size:10px;color:var(--text)">${incomingPgs} pg</span>
            <span style="padding:0 8px;font-size:10px;color:var(--text-dim)">${usedBefore} / ${limitAtTime}</span>
            <div style="padding:0 8px;overflow:hidden">${fname}</div>
        </div>`;
    }).join('');

    el.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px">
        ${statCard('Grace Events',       graceCount,      'var(--amber,#e5c07b)')}
        ${statCard('Hard Blocks',        exceededCount,   'var(--red,#e06c75)')}
        ${statCard('Clients Affected',   clientSet.size,  'var(--blue)')}
        ${statCard('Total Grace Pages',  totalGracePages, 'var(--text)')}
    </div>
    <div style="border:1px solid var(--border);border-radius:4px;overflow:hidden">
        ${header}
        ${rows}
    </div>`;
}

async function _smAdminDelete(smId, fieldKey, vendorName, clientEmail) {
    if (!confirm(`Delete saved correction?\n\nClient:  ${clientEmail}\nVendor:  ${vendorName}\nField:   ${fieldKey}\n\nThis removes the saved region and the prompt correction example for this field.`)) return;
    try {
        const resp = await apiJSON(`/spatial-memory/${smId}`, { method: 'DELETE' });
        _smEntries = _smEntries.filter(e => e.id !== smId);
        _smTotal = Math.max(0, _smTotal - 1);
        _smRenderTable();
        _smRenderPager();
        _smUpdateStats(document.getElementById('smStatsStrip'), _smEntries, _smTotal);
        const promptCount = Number(resp.gold_correction_fields_deleted || 0);
        showToast(`Deleted saved correction for "${fieldKey}" (${promptCount} prompt example${promptCount === 1 ? '' : 's'})`);
    } catch (e) {
        showToast(`Failed to delete: ${e.message}`);
    }
}
