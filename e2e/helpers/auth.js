/**
 * Auth helpers shared across all E2E tests.
 *
 * Because the SPA stores credentials in localStorage, the fastest way to
 * "log in" in a test is to inject the token before the page boots — no
 * form interaction needed.  Use mockLoginEndpoint() only when you actually
 * want to test the login form itself.
 */

const CLIENT_USER = {
  id: 'aaaaaaaa-0000-0000-0000-000000000001',
  email: 'client1@example.com',
  role: 'client',
};

const CLIENT_USER_2 = {
  id: 'aaaaaaaa-0000-0000-0000-000000000002',
  email: 'client2@example.com',
  role: 'client',
};

const ADMIN_USER = {
  id: 'aaaaaaaa-0000-0000-0000-000000000000',
  email: 'admin@example.com',
  role: 'admin',
};

/**
 * Injects a mock auth token + user into localStorage before the page
 * scripts run.  The router sees the token and skips the /login redirect.
 *
 * @param {import('@playwright/test').Page} page
 * @param {object} user  One of the USER constants above
 */
async function injectAuth(page, user = CLIENT_USER) {
  await page.addInitScript((u) => {
    localStorage.setItem('auth_token', 'mock-token');
    localStorage.setItem('auth_user', JSON.stringify(u));
  }, user);
}

/**
 * Intercepts POST /auth/login so the real login form works without a
 * live backend.  Call this before page.goto().
 *
 * @param {import('@playwright/test').Page} page
 * @param {object} user  The user object to return in the response
 */
async function mockLoginEndpoint(page, user = CLIENT_USER) {
  await page.route('**/auth/login', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ access_token: 'mock-token', user }),
    })
  );
}

module.exports = { CLIENT_USER, CLIENT_USER_2, ADMIN_USER, injectAuth, mockLoginEndpoint };
