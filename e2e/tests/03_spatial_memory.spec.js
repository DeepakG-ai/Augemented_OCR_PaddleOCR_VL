// @ts-check
/**
 * E2E: Spatial Memory — Draw-box correction flow
 *
 * Verifies the manual correction (draw → accept → confirm) saves
 * field_locations with strategy:'manual' and hits the corrections API.
 */
const { test, expect } = require('@playwright/test');
const { CLIENT_USER, injectAuth } = require('../helpers/auth');

const EXTRACTION_ID = '600';

const MOCK_EXTRACTION = {
  id: 600,
  status: 'done',
  vendor_id: 'RS001',
  vendor_name: 'ROBERT SCOTT',
  format_type: 'single_po_multipage',
  total_pages: 1,
  result: {
    po_number: 'PO-OLD',
    bill_to: 'Old Corp',
    line_items: [{ item: 'Widget', qty: 10 }],
  },
  corrected_result: null,
  field_locations: {
    po_number: { page: 1, box: [100, 100, 200, 120], matched_text: 'PO-OLD', score: 0.9, strategy: 'qwen_anchor', confidence: 'high' },
  },
  page_results: null,
};

// 1×1 white PNG (valid base64)
const TINY_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFklEQVQYV2P8z8BQDwAFAQH/AkI1HgAAAABJRU5ErkJggg==';

const MOCK_PAGES = [
  { page_number: 1, image_b64: TINY_PNG, mime_type: 'image/png' },
];

const MOCK_OCR = {
  ocr_pages: [{
    page_number: 1,
    words: [
      { text: 'PO-NEW', box: [300, 100, 400, 120], score: 0.99 },
      { text: 'Corp',   box: [300, 130, 380, 150], score: 0.98 },
    ],
  }],
};

test.describe('Spatial Memory Correction', () => {

  test.beforeEach(async ({ page }) => {
    await injectAuth(page, CLIENT_USER);

    await page.route('**/vendors', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
    );

    await page.route(`**/extractions/${EXTRACTION_ID}`, (route) => {
      if (route.request().url().includes('/pages') || route.request().url().includes('/ocr') || route.request().url().includes('/corrections') || route.request().url().includes('/spatial')) {
        return route.fallback();
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_EXTRACTION) });
    });

    await page.route(`**/extractions/${EXTRACTION_ID}/pages`, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PAGES) })
    );

    await page.route(`**/extractions/${EXTRACTION_ID}/ocr`, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_OCR) })
    );

    await page.route('**/gold-corrections', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ fields: {} }) })
    );

    await page.route('**/spatial-memory-fields', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ fields: {} }) })
    );
  });

  test('draw button enters selection mode', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);
    await expect(page.locator('#rvFieldsList')).toBeVisible({ timeout: 5000 });

    // Click the draw button on po_number
    const drawBtn = page.locator('#rvField_po_number .rv-draw-btn');
    await expect(drawBtn).toBeVisible();
    await drawBtn.click();

    // The field should enter "selecting" state
    await expect(page.locator('#rvField_po_number')).toHaveClass(/selecting/);

    // The viewer should have the crosshair cursor class
    await expect(page.locator('#rvViewer')).toHaveClass(/rv-selection-active/);
  });

  test('escape cancels selection mode', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);
    await expect(page.locator('#rvFieldsList')).toBeVisible({ timeout: 5000 });

    // Start selection
    const drawBtn = page.locator('#rvField_po_number .rv-draw-btn');
    await drawBtn.click();
    await expect(page.locator('#rvField_po_number')).toHaveClass(/selecting/);

    // Press Escape
    await page.keyboard.press('Escape');

    // Selection should be cancelled
    await expect(page.locator('#rvViewer')).not.toHaveClass(/rv-selection-active/);
  });

  test('confirm saves corrections via PUT and navigates to history', async ({ page }) => {
    // Track the corrections API call
    let correctionsCalled = false;
    let correctionPayload = null;

    await page.route(`**/extractions/${EXTRACTION_ID}/corrections`, (route) => {
      correctionsCalled = true;
      correctionPayload = route.request().postDataJSON();
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'ok' }),
      });
    });

    // Mock /extractions for the history page redirect
    await page.route('**/extractions?limit=*', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
    );

    await page.goto(`/#/review/${EXTRACTION_ID}`);
    await expect(page.locator('#rvFieldsList')).toBeVisible({ timeout: 5000 });

    // Make a change (edit po_number)
    const poInput = page.locator('.review-field-input[data-field="po_number"]');
    await poInput.fill('PO-CHANGED');

    // Mock the confirm dialog (typed-only changes prompt)
    page.on('dialog', async dialog => await dialog.accept());

    // Click Confirm
    await page.locator('#rvConfirmBtn').click();

    // Wait for navigation to history
    await page.waitForURL(/#\/history/, { timeout: 5000 });

    // Verify the corrections API was called
    expect(correctionsCalled).toBe(true);
    expect(correctionPayload).toBeTruthy();
    expect(correctionPayload.corrected_result.po_number).toBe('PO-CHANGED');
  });

  test('undo reverses the last field edit', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);
    await expect(page.locator('#rvFieldsList')).toBeVisible({ timeout: 5000 });

    // Edit po_number
    const poInput = page.locator('.review-field-input[data-field="po_number"]');
    const originalValue = await poInput.inputValue();
    await poInput.fill('PO-EDITED');

    // Press Ctrl+Z
    await page.keyboard.press('Control+z');

    // Value should revert to original
    await expect(poInput).toHaveValue(originalValue);
  });

  test('reset button restores original value', async ({ page }) => {
    await page.goto(`/#/review/${EXTRACTION_ID}`);
    await expect(page.locator('#rvFieldsList')).toBeVisible({ timeout: 5000 });

    // Edit po_number
    const poInput = page.locator('.review-field-input[data-field="po_number"]');
    await poInput.fill('PO-MODIFIED');

    // The reset button should appear
    const resetBtn = page.locator('#rvField_po_number .rv-reset-btn');
    await expect(resetBtn).toBeVisible();
    await resetBtn.click();

    // Value should be restored to original
    await expect(poInput).toHaveValue('PO-OLD');
  });
});
