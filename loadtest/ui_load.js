// k6 load test — simulates concurrent UI/API users browsing vendors/templates.
// This validates the read-cache + connection-pool work, NOT the GPU pipeline
// (extractions are queued and are a separate, single-GPU concern).
//
// Run from your Windows laptop OR from inside the Linux pod — see loadtest/README.md.
//
//   k6 run -e BASE_URL=https://YOUR_POD_URL -e EMAIL=you@x.com -e PASSWORD=secret ui_load.js
//
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = (__ENV.BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const EMAIL = __ENV.EMAIL;
const PASSWORD = __ENV.PASSWORD;
const PEAK = parseInt(__ENV.PEAK || '50', 10); // target concurrent users

export const options = {
  scenarios: {
    ui_users: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '30s', target: PEAK },  // ramp up
        { duration: '3m',  target: PEAK },  // hold at peak
        { duration: '30s', target: 0 },     // ramp down
      ],
      gracefulRampDown: '10s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],     // <1% errors (429s here = rate limit, see README)
    http_req_duration: ['p(95)<800'],   // tune to your network; warm cache should be well under
  },
};

// Log in once; share the token with every virtual user.
export function setup() {
  const res = http.post(
    `${BASE}/auth/login`,
    JSON.stringify({ email: EMAIL, password: PASSWORD }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(res, { 'login succeeded': (r) => r.status === 200 });
  // Login returns the JWT in `access_token` (TokenOut). Fall back to `token` just in case.
  const body = res.json();
  const token = body.access_token || body.token;
  if (!token) throw new Error(`No token in login response: ${res.body}`);
  return { token };
}

export default function (data) {
  const params = {
    headers: { Authorization: `Bearer ${data.token}` },
    tags: { name: 'browse' },
  };

  // A user opens Vendors, reads, opens Templates, reads — the exact path the
  // cache + Promise.all work targets.
  const vendors = http.get(`${BASE}/vendors`, { ...params, tags: { name: 'GET /vendors' } });
  check(vendors, { 'vendors 200': (r) => r.status === 200 });
  sleep(Math.random() * 3 + 2); // think time 2-5s

  const templates = http.get(`${BASE}/templates`, { ...params, tags: { name: 'GET /templates' } });
  check(templates, { 'templates 200': (r) => r.status === 200 });
  sleep(Math.random() * 3 + 2);
}
