/* ── Schedules page ─────────────────────────────────────────────────── */

let _schedulerState = { enabled: false, hour: 8, minute: 0, schedule_id: null, next_run: null };

async function renderSchedulesPage(app) {
    const user = (() => { try { return JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch { return null; } })();
    const isAdmin = user && user.role === 'admin';

    app.innerHTML = headerHTML() + `
    <div class="page-content">
        <div class="page-title">SCHEDULER</div>
        <p style="color:var(--text-dim);font-size:11px;margin-bottom:24px">
            Automatically scan your input folder and ingest every PDF at the configured time.
            PDFs are moved to the success or failed folder after processing (configure paths in Settings).
        </p>

        <!-- Status card -->
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px">
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:14px 16px">
                <div style="font-size:9px;color:var(--text-dim);letter-spacing:0.12em;margin-bottom:6px">STATUS</div>
                <div id="schedStatusBadge" style="font-size:14px;font-weight:600">—</div>
            </div>
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:14px 16px">
                <div style="font-size:9px;color:var(--text-dim);letter-spacing:0.12em;margin-bottom:6px">RUNS AT</div>
                <div id="schedRunsAt" style="font-size:14px;font-weight:600;font-family:var(--mono)">—</div>
            </div>
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:14px 16px">
                <div style="font-size:9px;color:var(--text-dim);letter-spacing:0.12em;margin-bottom:6px">NEXT RUN</div>
                <div id="schedNextRun" style="font-size:11px;color:var(--text-mid);font-family:var(--mono)">—</div>
            </div>
        </div>

        <!-- Config row -->
        <div style="background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:20px;margin-bottom:20px">
            <div style="font-size:9px;letter-spacing:0.14em;color:var(--text-dim);margin-bottom:16px">SCHEDULE TIME (daily)</div>
            <div style="display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap">
                <div>
                    <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em;display:block;margin-bottom:6px">HOUR (0–23)</label>
                    <input id="schedHour" type="number" min="0" max="23" value="8"
                           style="width:80px;background:var(--bg0);border:1px solid var(--border);color:var(--text);padding:8px 10px;font-family:var(--mono);font-size:13px">
                </div>
                <div>
                    <label style="font-size:9px;color:var(--text-dim);letter-spacing:0.1em;display:block;margin-bottom:6px">MINUTE (0–59)</label>
                    <input id="schedMinute" type="number" min="0" max="59" value="0"
                           style="width:80px;background:var(--bg0);border:1px solid var(--border);color:var(--text);padding:8px 10px;font-family:var(--mono);font-size:13px">
                </div>
                <div style="display:flex;gap:8px;margin-bottom:2px">
                    <button class="small-btn" id="btnStartSched"
                            onclick="startScheduler()"
                            style="background:var(--green,#98c379);color:#1e1e1e;padding:9px 20px;font-weight:600">
                        ▶ START
                    </button>
                    <button class="small-btn" id="btnStopSched"
                            onclick="stopScheduler()"
                            style="background:var(--red-dim,#e06c75);color:#fff;padding:9px 20px;font-weight:600">
                        ■ STOP
                    </button>
                </div>
                <span id="schedActionStatus" style="font-size:10px;color:var(--text-dim);align-self:center"></span>
            </div>
        </div>

        ${isAdmin ? `
        <div class="section" style="margin-top:24px">
            <div class="section-title" style="color:var(--blue)">ALL CLIENT SCHEDULES (ADMIN)</div>
            <div id="adminScheduleList">
                <div style="color:var(--text-dim);font-size:11px">Loading...</div>
            </div>
        </div>` : ''}
    </div>`;

    updateNavActive();
    await _loadSchedulerState();
    if (isAdmin) await _loadAdminSchedules();
}


async function _loadSchedulerState() {
    try {
        const data = await apiJSON('/api/scheduler');
        _schedulerState = data;
        _renderSchedulerStatus(data);
    } catch (e) {
        document.getElementById('schedStatusBadge').textContent = 'Error loading';
    }
}


function _renderSchedulerStatus(data) {
    const statusEl  = document.getElementById('schedStatusBadge');
    const runsAtEl  = document.getElementById('schedRunsAt');
    const nextRunEl = document.getElementById('schedNextRun');
    const hourEl    = document.getElementById('schedHour');
    const minEl     = document.getElementById('schedMinute');

    if (statusEl) {
        statusEl.innerHTML = data.enabled
            ? `<span style="color:var(--green)">● ACTIVE</span>`
            : `<span style="color:var(--text-dim)">○ INACTIVE</span>`;
    }
    if (runsAtEl) {
        runsAtEl.textContent = (data.hour != null && data.minute != null)
            ? `${String(data.hour).padStart(2,'0')}:${String(data.minute).padStart(2,'0')} daily`
            : '—';
    }
    if (nextRunEl) {
        nextRunEl.textContent = data.next_run
            ? new Date(data.next_run).toLocaleString()
            : '—';
    }
    if (hourEl   && data.hour   != null) hourEl.value   = data.hour;
    if (minEl    && data.minute != null) minEl.value    = data.minute;
}


async function startScheduler() {
    const hour   = parseInt(document.getElementById('schedHour')?.value   || '8',  10);
    const minute = parseInt(document.getElementById('schedMinute')?.value || '0', 10);
    const statusEl = document.getElementById('schedActionStatus');
    if (statusEl) statusEl.textContent = 'Starting…';
    try {
        const data = await apiJSON('/api/scheduler/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hour, minute }),
        });
        _schedulerState = { ...data, hour, minute };
        _renderSchedulerStatus(_schedulerState);
        if (statusEl) statusEl.textContent = 'Scheduler started.';
        setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    }
}


async function stopScheduler() {
    const statusEl = document.getElementById('schedActionStatus');
    if (statusEl) statusEl.textContent = 'Stopping…';
    try {
        await apiJSON('/api/scheduler/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        _schedulerState.enabled  = false;
        _schedulerState.next_run = null;
        _renderSchedulerStatus(_schedulerState);
        if (statusEl) statusEl.textContent = 'Scheduler stopped.';
        setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2500);
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    }
}


async function _loadAdminSchedules() {
    const el = document.getElementById('adminScheduleList');
    if (!el) return;
    try {
        const schedules = await apiJSON('/admin/schedules');
        if (!schedules.length) {
            el.innerHTML = '<div style="color:var(--text-dim);font-size:11px">No schedules across any client.</div>';
            return;
        }
        el.innerHTML = `
        <table class="data-table" style="width:100%">
            <thead><tr>
                <th>USER</th><th>CRON</th><th>ENABLED</th><th>LAST RAN</th>
            </tr></thead>
            <tbody>${schedules.map(s => `<tr>
                <td style="font-size:10px">${escapeHtml(s.email || '')}</td>
                <td style="font-family:monospace;font-size:10px">${escapeHtml(s.cron_expr)}</td>
                <td style="font-size:10px;color:${s.enabled ? 'var(--green)' : 'var(--text-dim)'}">${s.enabled ? 'ACTIVE' : 'PAUSED'}</td>
                <td style="font-size:10px;color:var(--text-dim)">${s.last_ran_at ? new Date(s.last_ran_at).toLocaleString() : '—'}</td>
            </tr>`).join('')}</tbody>
        </table>`;
    } catch (e) {
        el.innerHTML = `<div style="color:var(--red-dim);font-size:11px">Error: ${escapeHtml(e.message)}</div>`;
    }
}
