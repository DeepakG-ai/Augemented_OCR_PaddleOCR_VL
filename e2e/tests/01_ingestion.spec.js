// @ts-check
/**
 * E2E: Ingestion Flow
 *
 * Verifies the full upload → vendor-detect → pipeline → result path.
 * All API calls are intercepted so no live backend is required.
 */
const { test, expect } = require('@playwright/test');
const path = require('path');
const { CLIENT_USER, injectAuth } = require('../helpers/auth');

// ── Reusable mock data ────────────────────────────────────────────────

const MOCK_VENDORS = [
  { id: 'RS001', name: 'ROBERT SCOTT', created_at: '2025-01-01T00:00:00Z' },
  { id: 'RJS01', name: 'RJ SCHINNER',  created_at: '2025-01-01T00:00:00Z' },
];

const MOCK_INGEST_RESPONSE = {
  extraction_id: 999,
  job_id: 42,
  detected_vendor: { vendor_id: 'RS001', vendor_name: 'ROBERT SCOTT' },
};

const MOCK_VENDOR_TEMPLATE = {
  id: 1,
  vendor_id: 'RS001',
  format_type: 'single_po_multipage',
  header_fields: ['po_number', 'bill_to'],
  line_item_fields: ['item', 'qty'],
  prompt_instructions: '',
  extraction_rules: [],
  system_prompt: null,
  user_prompt: null,
  system_prompt_page1: null,
  user_prompt_page1: null,
  system_prompt_page2: null,
  user_prompt_page2: null,
  prompt_hash: 'mockhash',
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-01T00:00:00Z',
};

// SSE stream that simulates the durable worker pipeline
function buildSSE() {
  const events = [
    { event: 'progress', extraction: { id: 999, vendor_name: 'ROBERT SCOTT', progress: { stage: 'normalize', message: 'Rendered 3 page(s)', total_pages: 3, digital_pages: 3, scanned_pages: 0 } }, job: {} },
    { event: 'progress', extraction: { id: 999, vendor_name: 'ROBERT SCOTT', progress: { stage: 'ocr', message: 'All pages digital - OCR skipped' } }, job: {} },
    { event: 'progress', extraction: { id: 999, vendor_name: 'ROBERT SCOTT', progress: { stage: 'llm', message: 'Starting vision extraction on 3 page(s)', total_pages: 3, page: 1 } }, job: {} },
    { event: 'progress', extraction: { id: 999, vendor_name: 'ROBERT SCOTT', progress: { stage: 'llm', total_pages: 3, page: 2 } }, job: {} },
    { event: 'progress', extraction: { id: 999, vendor_name: 'ROBERT SCOTT', progress: { stage: 'llm', total_pages: 3, page: 3 } }, job: {} },
    { event: 'progress', extraction: { id: 999, vendor_name: 'ROBERT SCOTT', progress: { stage: 'postprocess', message: 'Field mapping complete' } }, job: {} },
    {
      event: 'done',
      extraction: {
        id: 999, status: 'done', vendor_name: 'ROBERT SCOTT', total_pages: 3,
        result: { po_number: 'PO-12345', bill_to: 'ABC Corp', line_items: [{ item: 'Widget', qty: 10 }] },
        field_locations: {},
      },
      job: { status: 'done' },
    },
  ];
  return events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('');
}

// ── Test setup ────────────────────────────────────────────────────────

test.describe('Ingestion Pipeline', () => {

  test.beforeEach(async ({ page }) => {
    await injectAuth(page, CLIENT_USER);

    // Mock /vendors
    await page.route('**/vendors', (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_VENDORS) });
      }
      return route.fallback();
    });

    // Mock detected vendor template load. The extract page loads this after
    // /ingest/ui returns detected_vendor so the test must keep the app on-page.
    await page.route('**/vendors/RS001/template', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_VENDOR_TEMPLATE),
      })
    );

    // Mock /upload-preview (file preview)
    await page.route('**/upload-preview', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pages: [{ page_number: 1, image_b64: 'iVBOR', mime_type: 'image/png' }],
          total_pages: 1,
        }),
      })
    );

    // Mock /ingest/ui
    await page.route('**/ingest/ui', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_INGEST_RESPONSE) })
    );

    // Mock SSE job stream
    await page.route('**/jobs/42/stream', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: buildSSE(),
      })
    );

    // Mock extraction pages (for post-extraction load)
    await page.route('**/extractions/999/pages', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ page_number: 1, image_b64: 'iVBOR', mime_type: 'image/png' }]),
      })
    );
  });

  // ── Tests ─────────────────────────────────────────────────────────

  test('renders the extract page with upload zone', async ({ page }) => {
    await page.goto('/#/extract');
    await expect(page.locator('#dropzone')).toBeVisible();
    await expect(page.locator('#extractBtn')).toBeVisible();
    await expect(page.locator('#extractBtn')).toBeDisabled();
  });

  test('enables extract button after file upload', async ({ page }) => {
    await page.goto('/#/extract');
    const fileInput = page.locator('#fileInput');
    await fileInput.setInputFiles(path.resolve(__dirname, '../fixtures/dummy.pdf'));
    await expect(page.locator('#fileBadge')).toBeVisible();
    await expect(page.locator('#extractBtn')).toBeEnabled();
  });

  test('runs full pipeline and shows DONE on all stages', async ({ page }) => {
    await page.goto('/#/extract');

    // Upload file
    const fileInput = page.locator('#fileInput');
    await fileInput.setInputFiles(path.resolve(__dirname, '../fixtures/dummy.pdf'));
    await expect(page.locator('#extractBtn')).toBeEnabled();

    // Click extract
    await page.locator('#extractBtn').click();

    // Wait for the pipeline panel to appear
    await expect(page.locator('#pipelinePanel')).toBeVisible({ timeout: 5000 });

    // Wait for the done event to propagate — the result section should become visible
    await expect(page.locator('#resultSection')).toBeVisible({ timeout: 15000 });

    // Verify all pipeline stages show DONE
    for (const stageId of ['upload', 'detect', 'normalize', 'ocr', 'llm', 'json', 'postprocess']) {
      const badge = page.locator(`#pipeBadge_${stageId}`);
      await expect(badge).toHaveText('DONE');
    }
  });

  test('shows detected vendor name', async ({ page }) => {
    await page.goto('/#/extract');
    const fileInput = page.locator('#fileInput');
    await fileInput.setInputFiles(path.resolve(__dirname, '../fixtures/dummy.pdf'));
    await page.locator('#extractBtn').click();

    // Wait for the detection stage to complete
    await expect(page.locator('#detectedVendorName')).toHaveText('ROBERT SCOTT', { timeout: 10000 });
  });

  test('pipeline normalize stage shows page classification result', async ({ page }) => {
    await page.goto('/#/extract');
    const fileInput = page.locator('#fileInput');
    await fileInput.setInputFiles(path.resolve(__dirname, '../fixtures/dummy.pdf'));
    await page.locator('#extractBtn').click();

    // Wait for completion
    await expect(page.locator('#resultSection')).toBeVisible({ timeout: 15000 });

    // The normalize detail should show the actual result, not generic text
    const detail = page.locator('#pipeDetail_normalize');
    const text = await detail.textContent();
    // Should NOT contain technical terms
    expect(text).not.toContain('pypdfium2');
    expect(text).not.toContain('PaddleOCR');
  });

  test('shows Open Review button after extraction completes', async ({ page }) => {
    await page.goto('/#/extract');
    const fileInput = page.locator('#fileInput');
    await fileInput.setInputFiles(path.resolve(__dirname, '../fixtures/dummy.pdf'));
    await page.locator('#extractBtn').click();

    await expect(page.locator('#resultSection')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('.review-link-btn')).toBeVisible();
    await expect(page.locator('.review-link-btn')).toHaveText('Open Review');
  });
});
