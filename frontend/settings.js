/* ── Augmented OCR — Settings page ───────────────────────────────────── */

// ══════════════════════════════════════════════════════════════════════
// PAGE: SETTINGS  (config + scheduler combined)
// Client → GET/PUT /api/config  (own config only)
// Admin  → GET /admin/config/users (all configs) + PUT per user
// ══════════════════════════════════════════════════════════════════════

let _settingsSSE = null;        // current EventSource
let _settingsAllConfigs = [];   // admin: list of all user configs
let _clientOnline = false;      // desktop agent heartbeat status (REST only)

// ── Scheduler state ───────────────────────────────────────────────────
let _schedulerState  = { schedules: [], max_schedules: 3 };
let _pendingNewRow   = false;

// ── Helpers ───────────────────────────────────────────────────────────

function _getAuthUser() {
    try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch { return null; }
}

function _statusDot(active) {
    const color = active ? 'var(--green)' : 'var(--text-dim)';
    return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:6px"></span>`;
}

function _uploadModeLabel(mode) {
    if (mode === 'folder') return '<span style="color:var(--green)">FOLDER WATCH</span>';
    return '<span style="color:var(--blue)">UI UPLOAD</span>';
}

// ── SSE connection ─────────────────────────────────────────────────────

function _startConfigSSE(onEvent) {
    if (_settingsSSE) {
        _settingsSSE.close();
        _settingsSSE = null;
    }
    const token = localStorage.getItem('auth_token');
    const url = `/api/config/stream?token=${encodeURIComponent(token || '')}`;
    const es = new EventSource(url);
    es.onmessage = (e) => {
        try { onEvent(JSON.parse(e.data)); } catch (_) {}
    };
    es.onerror = () => {
        // Reconnects automatically — no action needed.
    };
    _settingsSSE = es;
}

function _stopConfigSSE() {
    if (_settingsSSE) {
        _settingsSSE.close();
        _settingsSSE = null;
    }
}

// ── Admin: all-users config table ────────────────────────────────────

function _renderAllConfigsTable(configs) {
    if (!configs || configs.length === 0) {
        return `<div style="color:var(--text-dim);padding:16px;font-size:11px">No users found.</div>`;
    }
    return `
    <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead>
            <tr style="border-bottom:1px solid var(--border);color:var(--text-dim);letter-spacing:0.08em">
                <th style="text-align:left;padding:8px 12px;font-weight:500">USER</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">ROLE</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">UPLOAD MODE</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">INPUT FOLDER</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">OUTPUT FOLDER</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">DESKTOP AGENT</th>
                <th style="text-align:left;padding:8px 12px;font-weight:500">SERVER WATCHER</th>
                <th style="text-align:center;padding:8px 12px;font-weight:500">EDIT</th>
            </tr>
        </thead>
        <tbody>
            ${configs.map(u => `
            <tr style="border-bottom:1px solid var(--border)"
                onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background=''">
                <td style="padding:8px 12px;font-weight:500">${escapeHtml(u.email)}</td>
                <td style="padding:8px 12px;color:var(--text-dim)">${escapeHtml(u.role || '').toUpperCase()}</td>
                <td style="padding:8px 12px">${_uploadModeLabel(u.config?.upload_mode)}</td>
                <td style="padding:8px 12px;color:var(--text-mid);font-family:var(--mono);font-size:10px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                    title="${escapeHtml(u.config?.input_folder || '')}">
                    ${escapeHtml(u.config?.input_folder || '—')}
                </td>
                <td style="padding:8px 12px;color:var(--text-mid);font-family:var(--mono);font-size:10px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                    title="${escapeHtml(u.config?.output_folder || '')}">
                    ${escapeHtml(u.config?.output_folder || '—')}
                </td>
                <td style="padding:8px 12px">
                    ${_statusDot(u.client_online)}${u.client_online ? 'ACTIVE' : 'OFF'}
                </td>
                <td style="padding:8px 12px">
                    ${_statusDot(u.watcher_active)}${u.watcher_active ? 'ACTIVE' : 'OFF'}
                </td>
                <td style="padding:8px 12px;text-align:center">
                    <button class="small-btn"
                        onclick="openAdminEditConfig('${escapeInlineJsString(u.user_id)}','${escapeInlineJsString(u.email)}')">
                        Edit
                    </button>
                </td>
            </tr>`).join('')}
        </tbody>
    </table>`;
}

// ── Admin: inline edit modal for a single user ────────────────────────

function openAdminEditConfig(userId, email) {
    const existing = _settingsAllConfigs.find(u => u.user_id === userId) || {};
    const cfg = existing.config || {};
    const modal = document.getElementById('settingsModal');
    if (!modal) return;
    modal.innerHTML = `
    <div style="background:var(--bg0);border:1px solid var(--border);border-radius:4px;padding:24px;max-width:520px;width:100%">
        <div style="font-size:10px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:4px">EDIT CONFIG</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:20px">${escapeHtml(email)}</div>
        ${_configFormFields(cfg)}
        <div style="display:flex;gap:8px;margin-top:20px">
            <button class="small-btn" onclick="submitAdminConfigEdit('${escapeInlineJsString(userId)}')"
                    style="background:var(--blue);color:#fff">Save</button>
            <button class="small-btn" onclick="closeSettingsModal()">Cancel</button>
        </div>
        <div id="modalStatus" style="margin-top:10px;font-size:10px;color:var(--text-dim)"></div>
    </div>`;
    modal.style.display = 'flex';
}

async function submitAdminConfigEdit(userId) {
    const body = _readFormFields();
    const statusEl = document.getElementById('modalStatus');
    try {
        await apiFetch(`/admin/config/users/${encodeURIComponent(userId)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (statusEl) statusEl.textContent = 'Saved.';
        setTimeout(() => closeSettingsModal(), 800);
        // Refresh table
        _settingsAllConfigs = await apiJSON('/admin/config/users');
        const tbl = document.getElementById('allConfigsTable');
        if (tbl) tbl.innerHTML = _renderAllConfigsTable(_settingsAllConfigs);
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    }
}

function closeSettingsModal() {
    const modal = document.getElementById('settingsModal');
    if (modal) { modal.style.display = 'none'; modal.innerHTML = ''; }
}

// ── Shared form helpers ────────────────────────────────────────────────

function _configFormFields(cfg) {
    return `
    <div style="margin-bottom:14px">
        <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em">
            INPUT FOLDER PATH
            <input id="inputFolder" type="text"
                   value="${escapeHtml(cfg.input_folder || '')}"
                   placeholder="e.g. C:\\PDFDropbox\\incoming"
                   style="display:block;width:100%;margin-top:6px;background:var(--bg1);border:1px solid var(--border);color:var(--text);padding:8px 10px;font-family:var(--mono);font-size:11px;box-sizing:border-box">
        </label>
    </div>
    <div style="margin-bottom:14px">
        <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em">
            OUTPUT FOLDER PATH <span style="font-weight:normal;color:var(--text-dim)">(JSON results)</span>
            <input id="outputFolder" type="text"
                   value="${escapeHtml(cfg.output_folder || '')}"
                   placeholder="e.g. C:\\PDFDropbox\\results"
                   style="display:block;width:100%;margin-top:6px;background:var(--bg1);border:1px solid var(--border);color:var(--text);padding:8px 10px;font-family:var(--mono);font-size:11px;box-sizing:border-box">
        </label>
    </div>
    <div style="margin-bottom:14px">
        <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em">
            SUCCESS FOLDER PATH <span style="color:var(--green)">(PDF moved here after successful extraction)</span>
            <input id="successFolder" type="text"
                   value="${escapeHtml(cfg.success_folder || '')}"
                   placeholder="e.g. C:\\PDFDropbox\\success"
                   style="display:block;width:100%;margin-top:6px;background:var(--bg1);border:1px solid var(--border);color:var(--text);padding:8px 10px;font-family:var(--mono);font-size:11px;box-sizing:border-box">
        </label>
    </div>
    <div style="margin-bottom:14px">
        <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em">
            FAILED FOLDER PATH <span style="color:var(--red-dim,#e06c75)">(PDF moved here if ingestion fails)</span>
            <input id="failedFolder" type="text"
                   value="${escapeHtml(cfg.failed_folder || '')}"
                   placeholder="e.g. C:\\PDFDropbox\\failed"
                   style="display:block;width:100%;margin-top:6px;background:var(--bg1);border:1px solid var(--border);color:var(--text);padding:8px 10px;font-family:var(--mono);font-size:11px;box-sizing:border-box">
        </label>
    </div>`;
}

function onUploadModeChange() {} // no-op: folder fields always visible now

function _readFormFields() {
    return {
        upload_mode:    'folder',
        input_folder:   (document.getElementById('inputFolder')?.value   || '').trim(),
        output_folder:  (document.getElementById('outputFolder')?.value  || '').trim(),
        success_folder: (document.getElementById('successFolder')?.value || '').trim(),
        failed_folder:  (document.getElementById('failedFolder')?.value  || '').trim(),
    };
}

// ── Activity log (SSE events) ──────────────────────────────────────────

let _activityLog = [];

function _appendActivity(msg, color) {
    color = color || 'var(--text-dim)';
    _activityLog.unshift({ msg, color, ts: new Date().toLocaleTimeString() });
    if (_activityLog.length > 50) _activityLog.pop();
    const el = document.getElementById('configActivityLog');
    if (el) el.innerHTML = _renderActivityLog();
}

function _renderActivityLog() {
    if (_activityLog.length === 0) {
        return `<div style="color:var(--text-dim);font-size:10px;padding:12px">No events yet.</div>`;
    }
    return _activityLog.map(e => `
    <div style="display:flex;gap:10px;padding:5px 12px;border-bottom:1px solid var(--border);font-size:10px">
        <span style="color:var(--text-dim);white-space:nowrap">${escapeHtml(e.ts)}</span>
        <span style="color:${e.color}">${escapeHtml(e.msg)}</span>
    </div>`).join('');
}

function _handleSSEEvent(ev) {
    switch (ev.type) {
        case 'connected':
            if (ev.client_online !== undefined) _clientOnline = !!ev.client_online;
            _refreshOwnConfigDisplay(ev.config, ev.watcher_active);
            _appendActivity('Connected to config stream', 'var(--green)');
            break;
        case 'config_updated':
            if (ev.client_online !== undefined) _clientOnline = !!ev.client_online;
            _refreshOwnConfigDisplay(ev.config, ev.watcher_active);
            _appendActivity('Config updated', 'var(--blue)');
            break;
        case 'folder_ingest_started':
            _appendActivity(
                `Folder ingest queued: ${ev.filename || ev.path} → vendor: ${ev.vendor_name || ev.vendor_id} (job #${ev.job_id})`,
                'var(--green)',
            );
            break;
        case 'folder_ingest_error':
            _appendActivity(
                `Folder ingest error: ${ev.reason} — ${ev.path}`,
                'var(--red,#e06c75)',
            );
            break;
    }
}

function _refreshOwnConfigDisplay(config, watcherActive) {
    const el = document.getElementById('ownConfigStatus');
    if (!el) return;
    const inputFolder = config?.input_folder || '—';
    el.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px">
        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:14px 16px">
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:0.12em;margin-bottom:6px">DESKTOP AGENT</div>
            <div style="font-size:14px;font-weight:600">${_statusDot(_clientOnline)}${_clientOnline ? 'ACTIVE' : 'INACTIVE'}</div>
        </div>
        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:14px 16px">
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:0.12em;margin-bottom:6px">FOLDER WATCHER</div>
            <div style="font-size:14px;font-weight:600">${_statusDot(watcherActive || _clientOnline)}${(watcherActive || _clientOnline) ? 'ACTIVE' : 'INACTIVE'}</div>
        </div>
        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:14px 16px">
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:0.12em;margin-bottom:6px">INPUT FOLDER</div>
            <div style="font-size:11px;font-family:var(--mono);word-break:break-all;color:var(--text-mid)">${escapeHtml(inputFolder)}</div>
        </div>
    </div>`;
    // Sync form fields with latest config values
    const inEl      = document.getElementById('inputFolder');
    const outEl     = document.getElementById('outputFolder');
    const successEl = document.getElementById('successFolder');
    const failedEl  = document.getElementById('failedFolder');
    if (inEl      && config?.input_folder)   inEl.value      = config.input_folder;
    if (outEl     && config?.output_folder)  outEl.value     = config.output_folder;
    if (successEl && config?.success_folder) successEl.value = config.success_folder;
    if (failedEl  && config?.failed_folder)  failedEl.value  = config.failed_folder;
}

// ── Scheduler functions ───────────────────────────────────────────────

// Convert browser local hour:minute → UTC hour:minute (for sending to server)
function _localToUtcHM(lh, lm) {
    const d = new Date();
    d.setHours(lh, lm, 0, 0);
    return { hour: d.getUTCHours(), minute: d.getUTCMinutes() };
}

// Convert UTC hour:minute (from server) → local time string for display
function _utcToLocalStr(utcH, utcM) {
    const d = new Date();
    d.setUTCHours(utcH, utcM, 0, 0);
    return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}

async function _loadSchedulerState() {
    try {
        const data = await apiJSON('/api/scheduler');
        _schedulerState = data;
        _pendingNewRow = false;
        _renderSchedulerPanel();
    } catch (e) {
        const el = document.getElementById('schedulerPanel');
        if (el) el.innerHTML = `<div style="color:var(--red-dim);font-size:11px;padding:8px 0">Error loading scheduler</div>`;
    }
}

function _renderSchedulerPanel() {
    const el = document.getElementById('schedulerPanel');
    if (!el) return;
    const { schedules, max_schedules } = _schedulerState;
    const canAdd = schedules.length < max_schedules && !_pendingNewRow;

    const rowsHTML = schedules.map(s => {
        const localStr = (s.utc_hour != null && s.utc_minute != null)
            ? _utcToLocalStr(s.utc_hour, s.utc_minute) : '';
        const [lh, lm] = localStr ? localStr.split(':') : ['', ''];
        const nextStr  = s.next_run ? new Date(s.next_run).toLocaleString() : '—';
        let badge;
        if (s.is_executing) {
            badge = `<span style="color:var(--blue);font-size:10px">⚡ RUNNING</span>`;
        } else if (s.enabled) {
            badge = `<span style="color:var(--green);font-size:10px">● ACTIVE</span>`;
        } else {
            badge = `<span style="color:var(--text-dim);font-size:10px">○ INACTIVE</span>`;
        }
        return `
        <div style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border);flex-wrap:wrap">
            <div style="display:flex;gap:6px;align-items:flex-end">
                <div>
                    <div style="font-size:8px;color:var(--text-dim);margin-bottom:3px">HOUR (0–23)</div>
                    <input type="number" min="0" max="23" value="${escapeHtml(lh)}"
                           id="schedH_${s.id}"
                           oninput="this.value=Math.min(23,Math.max(0,parseInt(this.value)||0))"
                           style="width:58px;background:var(--bg0);border:1px solid var(--border);color:var(--text);padding:6px 8px;font-family:var(--mono);font-size:12px">
                </div>
                <div>
                    <div style="font-size:8px;color:var(--text-dim);margin-bottom:3px">MIN (0–59)</div>
                    <input type="number" min="0" max="59" value="${escapeHtml(lm)}"
                           id="schedM_${s.id}"
                           oninput="this.value=Math.min(59,Math.max(0,parseInt(this.value)||0))"
                           style="width:58px;background:var(--bg0);border:1px solid var(--border);color:var(--text);padding:6px 8px;font-family:var(--mono);font-size:12px">
                </div>
            </div>
            <div style="min-width:110px">
                ${badge}<br>
                <span style="font-size:9px;color:var(--text-dim);font-family:var(--mono)">${escapeHtml(nextStr)}</span>
            </div>
            <div style="display:flex;gap:6px;margin-left:auto">
                ${s.enabled
                    ? `<button class="small-btn" onclick="stopScheduler(${s.id})"
                               style="background:var(--red-dim,#e06c75);color:#fff;padding:6px 12px;font-weight:600">■ STOP</button>`
                    : `<button class="small-btn" onclick="startScheduler(${s.id})"
                               style="background:var(--green,#98c379);color:#1e1e1e;padding:6px 12px;font-weight:600">▶ START</button>`}
                <button class="small-btn" onclick="deleteSchedule(${s.id})"
                        style="color:var(--text-dim);padding:6px 10px" title="Remove this schedule">×</button>
            </div>
        </div>`;
    }).join('');

    const newRowHTML = _pendingNewRow ? `
    <div style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border);flex-wrap:wrap;background:var(--bg0);border-radius:4px;padding:12px">
        <div style="display:flex;gap:6px;align-items:flex-end">
            <div>
                <div style="font-size:8px;color:var(--text-dim);margin-bottom:3px">HOUR (0–23)</div>
                <input type="number" min="0" max="23" placeholder="8" id="schedH_new"
                       oninput="this.value=Math.min(23,Math.max(0,parseInt(this.value)||0))"
                       style="width:58px;background:var(--bg1);border:1px solid var(--blue);color:var(--text);padding:6px 8px;font-family:var(--mono);font-size:12px">
            </div>
            <div>
                <div style="font-size:8px;color:var(--text-dim);margin-bottom:3px">MIN (0–59)</div>
                <input type="number" min="0" max="59" placeholder="0" id="schedM_new"
                       oninput="this.value=Math.min(59,Math.max(0,parseInt(this.value)||0))"
                       style="width:58px;background:var(--bg1);border:1px solid var(--blue);color:var(--text);padding:6px 8px;font-family:var(--mono);font-size:12px">
            </div>
        </div>
        <span style="font-size:10px;color:var(--blue);font-weight:600">NEW SCHEDULE</span>
        <div style="display:flex;gap:6px;margin-left:auto">
            <button class="small-btn" onclick="startScheduler(null)"
                    style="background:var(--blue);color:#fff;padding:6px 14px;font-weight:600">SAVE</button>
            <button class="small-btn" onclick="_cancelNewRow()"
                    style="color:var(--text-dim);padding:6px 10px" title="Cancel">×</button>
        </div>
    </div>` : '';

    const emptyMsg = schedules.length === 0 && !_pendingNewRow
        ? '<div style="color:var(--text-dim);font-size:11px;padding:10px 0">No schedules configured.</div>'
        : '';

    el.innerHTML = `
    ${rowsHTML}
    ${newRowHTML}
    ${emptyMsg}
    <div style="margin-top:10px;display:flex;align-items:center;gap:12px">
        <button class="small-btn" onclick="addScheduleRow()"
                ${canAdd ? '' : 'disabled'}
                style="opacity:${canAdd ? 1 : 0.4};font-size:10px;padding:6px 12px">
            + ADD  (${schedules.length}/${max_schedules})
        </button>
        <span id="schedActionStatus" style="font-size:10px;color:var(--text-dim)"></span>
    </div>`;
}

function addScheduleRow() {
    _pendingNewRow = true;
    _renderSchedulerPanel();
}

function _cancelNewRow() {
    _pendingNewRow = false;
    _renderSchedulerPanel();
}

async function startScheduler(scheduleId) {
    const suffix = scheduleId != null ? String(scheduleId) : 'new';
    const rawH = document.getElementById(`schedH_${suffix}`)?.value ?? '';
    const rawM = document.getElementById(`schedM_${suffix}`)?.value ?? '';
    const localH = parseInt(rawH, 10);
    const localM = parseInt(rawM, 10);
    const statusEl = document.getElementById('schedActionStatus');

    if (isNaN(localH) || localH < 0 || localH > 23) {
        if (statusEl) statusEl.textContent = 'Hour must be 0–23';
        return;
    }
    if (isNaN(localM) || localM < 0 || localM > 59) {
        if (statusEl) statusEl.textContent = 'Minute must be 0–59';
        return;
    }

    const { hour: utcH, minute: utcM } = _localToUtcHM(localH, localM);
    if (statusEl) statusEl.textContent = 'Saving…';
    try {
        const body = { hour: utcH, minute: utcM };
        if (scheduleId != null) body.schedule_id = scheduleId;
        await apiJSON('/api/scheduler/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        _pendingNewRow = false;
        await _loadSchedulerState();
        if (statusEl) statusEl.textContent = 'Started.';
        setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    }
}

async function stopScheduler(scheduleId) {
    const statusEl = document.getElementById('schedActionStatus');
    if (statusEl) statusEl.textContent = 'Stopping…';
    try {
        await apiJSON('/api/scheduler/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ schedule_id: scheduleId }),
        });
        await _loadSchedulerState();
        if (statusEl) statusEl.textContent = 'Stopped.';
        setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    }
}

async function deleteSchedule(scheduleId) {
    const statusEl = document.getElementById('schedActionStatus');
    if (statusEl) statusEl.textContent = 'Removing…';
    try {
        await apiFetch(`/api/scheduler/${scheduleId}`, { method: 'DELETE' });
        await _loadSchedulerState();
        if (statusEl) statusEl.textContent = 'Removed.';
        setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    }
}

function _parseCronToLocal(cronExpr) {
    if (!cronExpr) return '—';
    const parts = cronExpr.trim().split(/\s+/);
    if (parts.length < 2) return cronExpr;
    const utcM = parseInt(parts[0], 10);
    const utcH = parseInt(parts[1], 10);
    if (isNaN(utcM) || isNaN(utcH)) return cronExpr;
    return _utcToLocalStr(utcH, utcM);
}

async function _loadAdminSchedules() {
    const el = document.getElementById('adminScheduleList');
    if (!el) return;
    try {
        const schedules = await apiJSON('/admin/schedules');

        if (!schedules.length) {
            el.innerHTML = '<div style="color:var(--text-dim);font-size:11px;padding:16px">No schedules configured across any client.</div>';
            return;
        }

        // Group by user email
        const byUser = {};
        schedules.forEach(s => {
            const key = s.email || s.user_id || 'Unknown';
            if (!byUser[key]) byUser[key] = [];
            byUser[key].push(s);
        });

        const groups = Object.entries(byUser).map(([email, rows]) => {
            const rowsHTML = rows.map(s => {
                const localTime = _parseCronToLocal(s.cron_expr);
                const lastRan = s.last_ran_at
                    ? new Date(s.last_ran_at).toLocaleString()
                    : '<span style="color:var(--text-dim)">Never</span>';
                const nextRun = s.next_run
                    ? new Date(s.next_run).toLocaleString()
                    : '<span style="color:var(--text-dim)">—</span>';
                const statusBadge = s.is_executing
                    ? `<span style="color:var(--blue);font-weight:600">⚡ RUNNING</span>`
                    : s.enabled
                        ? `<span style="color:var(--green);font-weight:600">● ACTIVE</span>`
                        : `<span style="color:var(--text-dim)">○ PAUSED</span>`;
                return `
                <tr style="border-bottom:1px solid var(--border)"
                    onmouseover="this.style.background='var(--bg0)'" onmouseout="this.style.background=''">
                    <td style="padding:8px 14px;font-family:var(--mono);font-size:12px;font-weight:600;color:var(--text)">${escapeHtml(localTime)}</td>
                    <td style="padding:8px 14px;font-family:var(--mono);font-size:10px;color:var(--text-dim)">${escapeHtml(s.cron_expr || '')}</td>
                    <td style="padding:8px 14px">${statusBadge}</td>
                    <td style="padding:8px 14px;font-size:10px;color:var(--text-dim)">${nextRun}</td>
                    <td style="padding:8px 14px;font-size:10px;color:var(--text-dim)">${lastRan}</td>
                </tr>`;
            }).join('');

            return `
            <div style="margin-bottom:4px">
                <div style="padding:8px 14px;background:var(--bg0);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
                    <span style="font-size:11px;font-weight:600;color:var(--text)">${escapeHtml(email)}</span>
                    <span style="font-size:9px;color:var(--text-dim);letter-spacing:0.08em">${rows.length} SCHEDULE${rows.length !== 1 ? 'S' : ''}</span>
                </div>
                <table style="width:100%;border-collapse:collapse;font-size:11px">
                    <thead>
                        <tr style="border-bottom:1px solid var(--border)">
                            <th style="padding:6px 14px;text-align:left;font-size:9px;font-weight:500;letter-spacing:0.1em;color:var(--text-dim)">LOCAL TIME</th>
                            <th style="padding:6px 14px;text-align:left;font-size:9px;font-weight:500;letter-spacing:0.1em;color:var(--text-dim)">CRON</th>
                            <th style="padding:6px 14px;text-align:left;font-size:9px;font-weight:500;letter-spacing:0.1em;color:var(--text-dim)">STATUS</th>
                            <th style="padding:6px 14px;text-align:left;font-size:9px;font-weight:500;letter-spacing:0.1em;color:var(--text-dim)">NEXT RUN</th>
                            <th style="padding:6px 14px;text-align:left;font-size:9px;font-weight:500;letter-spacing:0.1em;color:var(--text-dim)">LAST RAN</th>
                        </tr>
                    </thead>
                    <tbody>${rowsHTML}</tbody>
                </table>
            </div>`;
        }).join('');

        el.innerHTML = `<div style="padding:4px 0">${groups}</div>`;

    } catch (e) {
        el.innerHTML = `<div style="color:var(--red,#e06c75);font-size:11px;padding:16px">Error: ${escapeHtml(e.message)}</div>`;
    }
}

// ── Save own config ────────────────────────────────────────────────────

async function saveOwnConfig() {
    const body = _readFormFields();
    const statusEl = document.getElementById('ownSaveStatus');
    if (statusEl) statusEl.textContent = 'Saving…';
    try {
        await apiFetch('/api/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (statusEl) statusEl.textContent = 'Saved.';
        setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2000);
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    }
}

// ── Main render ───────────────────────────────────────────────────────

async function renderSettingsPage(app) {
    _stopConfigSSE();
    _activityLog = [];
    const authUser = _getAuthUser();
    const isAdmin = authUser && authUser.role === 'admin';

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">Settings</div>

        <!-- Status strip -->
        <div style="margin-bottom:20px">
            <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:10px">
                MY CONFIGURATION${isAdmin ? ' · ADMIN' : ''}
            </div>
            <div id="ownConfigStatus" style="color:var(--text-dim);font-size:11px">Loading…</div>
        </div>

        <!-- Two-column: config (left) + scheduler (right) -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;align-items:start">

            <!-- Left: Config form -->
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:20px">
                <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:16px">EDIT MY CONFIG</div>
                <div id="ownConfigForm">Loading…</div>
                <div style="display:flex;align-items:center;gap:12px;margin-top:16px">
                    <button class="small-btn" onclick="saveOwnConfig()"
                            style="background:var(--blue);color:#fff;padding:8px 18px">Save</button>
                    <span id="ownSaveStatus" style="font-size:10px;color:var(--text-dim)"></span>
                </div>
            </div>

            <!-- Right: Scheduler -->
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:20px">
                <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:14px">
                    SCHEDULER <span style="color:var(--text-dim);font-weight:normal;font-size:9px">— up to 3 daily times</span>
                </div>
                <div id="schedulerPanel">
                    <div style="color:var(--text-dim);font-size:11px">Loading…</div>
                </div>
            </div>
        </div>

        <!-- Live activity log (SSE) -->
        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden;margin-bottom:20px">
            <div style="padding:10px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
                <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">LIVE ACTIVITY LOG</span>
                <span id="sseStatus" style="font-size:9px;color:var(--text-dim)">Connecting…</span>
            </div>
            <div id="configActivityLog" style="max-height:180px;overflow-y:auto">
                <div style="color:var(--text-dim);font-size:10px;padding:12px">No events yet.</div>
            </div>
        </div>

        ${isAdmin ? `
        <!-- Admin: all users config table -->
        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden;margin-bottom:20px">
            <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
                <span style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim)">ALL CLIENT CONFIGURATIONS</span>
                <button class="small-btn" onclick="refreshAllConfigs()">Refresh</button>
            </div>
            <div id="allConfigsTable" style="overflow-x:auto">
                <div style="color:var(--text-dim);padding:16px;font-size:11px">Loading…</div>
            </div>
        </div>
        <!-- Admin: all client schedules -->
        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;overflow:hidden">
            <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
                <span style="font-size:9px;letter-spacing:0.14em;color:var(--blue)">ALL CLIENT SCHEDULES</span>
                <button class="small-btn" onclick="_loadAdminSchedules()">Refresh</button>
            </div>
            <div id="adminScheduleList">
                <div style="color:var(--text-dim);font-size:11px;padding:12px">Loading…</div>
            </div>
        </div>` : ''}
    </div>

    <!-- Modal overlay for admin edit -->
    <div id="settingsModal"
         style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:900;
                align-items:center;justify-content:center;padding:20px">
    </div>

    <div class="bottom-bar">
        <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.1em">SETTINGS</span>
    </div>`;
    updateNavActive();

    // Load own config and render form
    let ownConfig = {};
    let watcherActive = false;
    try {
        const data = await apiJSON('/api/config');
        ownConfig = data.config || {};
        watcherActive = !!data.watcher_active;
        _clientOnline = !!data.client_online;
    } catch (e) {
        console.warn('Settings load error:', e);
    }
    const formEl = document.getElementById('ownConfigForm');
    if (formEl) formEl.innerHTML = _configFormFields(ownConfig);
    _refreshOwnConfigDisplay(ownConfig, watcherActive);

    // Load scheduler state
    await _loadSchedulerState();

    // Start SSE
    _startConfigSSE((ev) => {
        _handleSSEEvent(ev);
        const statusEl = document.getElementById('sseStatus');
        if (statusEl) statusEl.textContent = 'Live';
    });

    // Admin: load all configs + schedules
    if (isAdmin) {
        await Promise.all([refreshAllConfigs(), _loadAdminSchedules()]);
    }
}

async function refreshAllConfigs() {
    try {
        _settingsAllConfigs = await apiJSON('/admin/config/users');
        const tbl = document.getElementById('allConfigsTable');
        if (tbl) tbl.innerHTML = _renderAllConfigsTable(_settingsAllConfigs);
    } catch (e) {
        const tbl = document.getElementById('allConfigsTable');
        if (tbl) tbl.innerHTML = `<div style="color:var(--red,#e06c75);padding:16px;font-size:11px">${escapeHtml(e.message)}</div>`;
    }
}
