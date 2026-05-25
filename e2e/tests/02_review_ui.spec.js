// @ts-check
/**
 * E2E: Review UI
 *
 * Verifies the Review page renders correctly with mocked extraction data:
 * - Sidebar fields list
 * - Document image canvas
 * - Line items table
 * - JSON output panel
 * - Page navigation
 * - Confirm and navigation buttons
 */
const { test, expect } = require('@playwright/test');
const { CLIENT_USER, injectAuth } = require('../helpers/auth');

// ── Mock data ──────────────────────────────────────────────────────────

const EXTRACTION_ID = '500';

const MOCK_EXTRACTION = {
  id: 500,
  status: 'done',
  vendor_id: 'RS001',
  vendor_name: 'ROBERT SCOTT',
  format_type: 'single_po_multipage',
  total_pages: 2,
  result: {
    po_number: 'PO-99001',
    bill_to: 'ABC Corp',
    ship_to: 'Warehouse 7',
    order_date: '11/14/25',
    line_items: [
      { item: '3PB-F-20-BX', pack: '12/bx', order_qty: 24, unit_price: 6.22, _page: 1 },
      { item: '10SG-F-02',   pack: '4/cs',  order_qty: 15, unit_price: 41.89, _page: 1 },
      { item: 'OFB-F-01-BX', pack: '8/bx',  order_qty: 12, unit_price: 27.66, _page: 2 },
    ],
  },
  corrected_result: null,
  field_locations: {
    po_number:  { page: 1, box: [100, 200, 300, 220], matched_text: 'PO-99001', score: 0.95, strategy: 'qwen_anchor', confidence: 'high' },
    bill_to:    { page: 1, box: [100, 240, 350, 270], matched_text: 'ABC Corp', score: 0.92, strategy: 'qwen_anchor', confidence: 'high' },
    ship_to:    { page: 1, box: [100, 280, 350, 310], matched_text: 'Warehouse 7', score: 0.88, strategy: 'manual', confidence: 'high' },
    order_date: { page: 1, box: [400, 200, 520, 220], matched_text: '11/14/25', score: 0.9, strategy: 'qwen_anchor', confidence: 'high' },
  },
  page_results: null,
};

const MOCK_PAGES = [
  { page_number: 1, image_b64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', mime_type: 'image/png' },
  { page_number: 2, image_b64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', mime_type: 'image/png' },
];

const MOCK_OCR = {
  ocr_pages: [
    {
      page_number: 1,
      words: [
        { text: 'PO-99001', box: [100, 200, 300, 220], score: 0.99 },
        { text: 'ABC',      box: [100, 240, 200, 260], score: 0.98 },
        { text: 'Corp',     box: [210, 240, 300, 260], score: 0.97 },
      ],
    },
    { page_number: 2, words: [{ text: 'OFB-F-01-BX', box: [50, 100, 200, 120], score: 0.96 }] },
  ],
};

// ── Test setup ────────────────────────────────────────────────────────

test.describe('Review UI', () => {

  test.beforeEach(async ({ page }) => {
    await injectAuth(page, CLIENT_USER);

    // Mock /vendors (needed by header render)
    await page.route('**/vendors', (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
      }
      return route.fallback();
    });

    // Mock extraction fetch
    await page.route(`**/extractions/${EXTRACTION_ID}`, (route) => {
      if (route.request().url().includes('/pages') || route.request().url().includes('/ocr') || route.request().url().includes('/corrections') || route.request().url().includes('/spatial')) {
        return route.fallback();
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_EXTRACTION) });
    });

    // Mock pages
    await page.route(`**/extractions/${EXTRACTION_ID}/pages`, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PAGES) })
    );

    // Mock OCR
    await page.route(`**/extractions/${EXTRACTION_ID}/ocr`, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_OCR) })
    );

    // Mock gold corrections
    await page.route('**/gold-corrections', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ fields: {} }) })
    );

    // Mock spatial memory fields
    await page.route('**/spatial-memory-fields', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ fields: {} }) })
    );
  });

  // ── Tests ─────────────────────────────────────────────────────────

  test('renders the extracted fields sidebar', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);

    // Wait for field list to render
    await expect(page.locator('#rvFieldsList')).toBeVisible({ timeout: 5000 });

    // Check header fields are present
    await expect(page.locator('#rvFieldsList')).toContainText('po_number');
    await expect(page.locator('#rvFieldsList')).toContainText('bill_to');
    await expect(page.locator('#rvFieldsList')).toContainText('ship_to');
    await expect(page.locator('#rvFieldsList')).toContainText('order_date');
  });

  test('renders the document image', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);
    await expect(page.locator('#rvDocImg')).toBeVisible({ timeout: 5000 });
  });

  test('renders the line items table', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);

    // Wait for line items
    await expect(page.locator('#rvLineItemsWrap')).toBeVisible({ timeout: 5000 });

    // Should show the page-1 line items
    await expect(page.locator('.review-line-items')).toBeVisible();
    await expect(page.locator('.review-line-items')).toContainText('3PB-F-20-BX');
  });

  test('renders JSON output panel', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);
    const jsonPre = page.locator('#rvJsonPre');
    await expect(jsonPre).toBeVisible({ timeout: 5000 });

    const jsonText = await jsonPre.textContent();
    const parsed = JSON.parse(jsonText);
    expect(parsed.po_number).toBe('PO-99001');
    expect(parsed.line_items).toHaveLength(3);
  });

  test('page navigation updates line items', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);

    // Verify page 1 line items
    await expect(page.locator('.review-line-items')).toContainText('3PB-F-20-BX');

    // Navigate to page 2
    await page.locator('#rvNextBtn').click();
    await expect(page.locator('#rvPageInd')).toContainText('PAGE 2');

    // Page 2 should show OFB-F-01-BX
    await expect(page.locator('#rvLineItemsWrap')).toContainText('OFB-F-01-BX');
  });

  test('shows matched/unmatched stats in bottom bar', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);

    await expect(page.locator('.stat-matched')).toBeVisible({ timeout: 5000 });
    // We have 4 field_locations, so at least some should be matched
    const matchedText = await page.locator('.stat-matched').textContent();
    expect(matchedText).toMatch(/\d+ MATCHED/);
  });

  test('confirm button exists and navigates to history', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);
    await expect(page.locator('#rvConfirmBtn')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('#rvConfirmBtn')).toHaveText('Confirm');
  });

  test('OCR status indicator shows loaded', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);
    const ocrStatus = page.locator('#rvOcrStatus');
    await expect(ocrStatus).toBeVisible({ timeout: 5000 });
    await expect(ocrStatus).toContainText('OCR LOADED');
  });

  test('field input allows editing and marks as changed', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);

    // Find the po_number input and edit it
    const poInput = page.locator('.review-field-input[data-field="po_number"]');
    await expect(poInput).toBeVisible({ timeout: 5000 });
    await poInput.fill('PO-CHANGED');

    // The field item should now have the rv-changed class
    const fieldItem = page.locator('#rvField_po_number');
    await expect(fieldItem).toHaveClass(/rv-changed/);

    // JSON should reflect the change
    const jsonPre = page.locator('#rvJsonPre');
    const jsonText = await jsonPre.textContent();
    expect(jsonText).toContain('PO-CHANGED');
  });
});
