/* ── Augmented OCR — Login Page ──────────────────────────────────────── */

async function renderLoginPage(app) {
    app.className = 'app';
    app.innerHTML = `
        <div style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px">
            <div style="width:100%;max-width:380px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:32px">
                <div style="font-family:var(--display),sans-serif;font-size:22px;font-weight:700;letter-spacing:0.12em;color:var(--blue);text-transform:uppercase;text-align:center;margin-bottom:6px">
                    Augmented <span style="color:var(--text)">OCR</span>
                </div>
                <div style="font-size:10px;letter-spacing:0.18em;color:var(--text-dim);text-align:center;text-transform:uppercase;margin-bottom:28px">
                    Extraction Terminal
                </div>

                <form id="loginForm" autocomplete="on">
                    <label style="display:block;font-size:10px;letter-spacing:0.12em;color:var(--text-dim);text-transform:uppercase;margin-bottom:6px">Email</label>
                    <input id="loginEmail" type="email" required autocomplete="username"
                        style="width:100%;background:var(--bg);border:1px solid var(--border2);color:var(--text);padding:9px 10px;font-family:var(--mono);font-size:12px;border-radius:3px;margin-bottom:16px;box-sizing:border-box">

                    <label style="display:block;font-size:10px;letter-spacing:0.12em;color:var(--text-dim);text-transform:uppercase;margin-bottom:6px">Password</label>
                    <input id="loginPassword" type="password" required autocomplete="current-password"
                        style="width:100%;background:var(--bg);border:1px solid var(--border2);color:var(--text);padding:9px 10px;font-family:var(--mono);font-size:12px;border-radius:3px;margin-bottom:20px;box-sizing:border-box">

                    <button type="submit" id="loginSubmit"
                        style="width:100%;background:var(--blue);border:1px solid var(--blue-dim);color:#fff;padding:10px;font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;border-radius:3px;cursor:pointer">
                        Sign In
                    </button>

                    <div id="loginError" style="margin-top:14px;font-size:11px;color:#ef4444;text-align:center;display:none"></div>
                </form>
            </div>
        </div>
    `;

    document.getElementById('loginForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const email = document.getElementById('loginEmail').value.trim();
        const password = document.getElementById('loginPassword').value;
        const errBox = document.getElementById('loginError');
        const btn = document.getElementById('loginSubmit');
        errBox.style.display = 'none';
        btn.disabled = true;
        btn.textContent = 'Signing in…';

        try {
            const res = await fetch(`${API}/auth/login`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password }),
            });
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                const detail = body.error || body.detail || {};
                const message = typeof detail === 'string' ? detail : detail.message;
                throw new Error(message || `Login failed (${res.status})`);
            }
            const data = await res.json();
            // Validate response shape before trusting it
            if (typeof data.access_token !== 'string' || !data.access_token) {
                throw new Error('Unexpected server response — no access token received');
            }
            if (!data.user || typeof data.user !== 'object') {
                throw new Error('Unexpected server response — no user info received');
            }
            localStorage.setItem('auth_token', data.access_token);
            localStorage.setItem('auth_user', JSON.stringify(data.user));
            window.location.hash = '#/vendors';
        } catch (err) {
            errBox.textContent = err.message || 'Login failed';
            errBox.style.display = 'block';
        } finally {
            btn.disabled = false;
            btn.textContent = 'Sign In';
        }
    });
}

function getAuthToken() {
    return localStorage.getItem('auth_token');
}

function getAuthUser() {
    try {
        return JSON.parse(localStorage.getItem('auth_user') || 'null');
    } catch (e) {
        return null;
    }
}

function logout() {
    localStorage.removeItem('auth_token');
    localStorage.removeItem('auth_user');
    // Full reload clears all in-memory state (loadedFile, activeExtractionId, etc.)
    window.location.replace(window.location.pathname + '#/login');
    window.location.reload();
}
