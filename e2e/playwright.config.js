// @ts-check
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  timeout: 30_000,
  retries: 0,
  reporter: [
    ['html', { outputFolder: 'report', open: 'never' }],
    ['list'],
  ],
  use: {
    baseURL: 'http://localhost:8000',
    headless: true,
    viewport: { width: 1280, height: 800 },
    // Save screenshot + video only when a test fails
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
});
