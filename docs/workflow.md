# Augmented OCR — Complete System Workflow

> **Multi-vendor, hybrid PDF extraction pipeline with spatial memory and human-in-the-loop review.**

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Authentication & Roles](#authentication--roles)
3. [Page 1: Vendors](#page-1-vendors)
4. [Page 2: Template](#page-2-template)
5. [Page 3: Saved Templates](#page-3-saved-templates)
6. [Page 4: Extraction](#page-4-extraction)
7. [Page 5: History](#page-5-history)
8. [Page 6: Review (HITL)](#page-6-review-hitl)
9. [Page 7: Dashboard](#page-7-dashboard)
10. [Page 8: Admin — User Management](#page-8-admin--user-management)
11. [Backend Pipeline — Stage by Stage](#backend-pipeline--stage-by-stage)
12. [Spatial Memory System](#spatial-memory-system)
13. [Billing & Subscription](#billing--subscription)
14. [Feature Implementation Status](#feature-implementation-status)

---

## System Overview

Augmented OCR is a SaaS document extraction platform. Clients upload vendor invoices/POs as PDFs. The system:

1. **Detects** which vendor the document belongs to (from page 1 text)
2. **Classifies** each page as digital or scanned
3. **Extracts** structured JSON fields using Qwen3-VL (vision language model)
4. **Maps** extracted values to word-level bounding boxes for visual review
5. **Lets the user correct** any mistakes via drag-draw selection on the PDF
6. **Remembers** manual corrections as spatial memory for future same-layout documents

### Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│  Frontend    │────▶│  FastAPI      │────▶│  PostgreSQL  │     │  Qwen3-VL    │
│  SPA (JS)   │◀────│  Backend      │◀────│  Database    │     │  (llama.cpp) │
│             │ SSE │  + Workers    │────▶│              │     │              │
└─────────────┘     └──────┬───────┘     └──────────────┘     └──────────────┘
                           │
                    ┌──────┴───────┐
                    │  pypdfium2   │  Digital pages
                    │  PaddleOCR   │  Scanned pages
                    └──────────────┘
```

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Vanilla JS SPA, hash-based routing, CSS dark/light theme |
| Backend | Python FastAPI, async, uvicorn |
| Database | PostgreSQL (asyncpg connection pool) |
| VLM | Qwen3-VL via llama.cpp (OpenAI-compatible API) |
| Digital PDF | pypdfium2 — embedded text + word boxes |
| Scanned PDF | PaddleOCR — OCR text + word boxes |
| Auth | JWT (HS256), bcrypt password hashing |
| Observability | MLflow tracking and human-readable pipeline logs |
| Containerization | Docker + Docker Compose |

---

## Authentication & Roles

### Two roles

| Role | Can do |
|------|--------|
| **Admin** | Create/deactivate users, set subscription limits, view all clients' usage, see prompt previews |
| **Client** | Create own vendors, configure templates, upload PDFs, review extractions, view own dashboard |

### Login flow

1. User enters email + password at `#/login`
2. Backend verifies bcrypt hash → returns JWT token
3. Token stored in `localStorage` → sent as `Authorization: Bearer` header on every API call
4. 401 response → auto-redirect to login, clear all in-memory state

### Tenant isolation

- Every vendor, template, extraction, and spatial memory record is scoped to the `user_id` (owner)
- Admin can see all users' data via `/admin/*` endpoints
- Clients can only see their own data via `/user/*` and standard endpoints

---

## Page 1: Vendors

**Route:** `#/vendors`
**File:** `frontend/vendors.js` → `renderVendorsPage()`

### What the user sees

- List of vendor cards (name, ID, creation date)
- Each card has: **Template** link, **Extract** shortcut, **Delete** button
- **+ Add New Vendor** button opens a modal

### How vendor creation works

1. User clicks **+ Add New Vendor**
2. Modal opens with two fields: **Vendor Name** and **Vendor ID**
3. User enters name (e.g. `Robert Scott`) and ID (e.g. `RS001`)
4. Name is uppercased automatically
5. If ID is left blank, a random 8-char ID is generated (not recommended)
6. `POST /vendors` → creates vendor in DB with `user_id` = current user

### Why vendor ID matters

The vendor ID is used for:
- Template lookup (`GET /vendors/{id}/template`)
- Vendor detection matching (exact match against aliases)
- Spatial memory scoping (`vendor_id` + `layout_key`)
- Extraction association (`extractions.vendor_id`)

### Vendor detection relationship

When a PDF is uploaded **without** pre-selecting a vendor, the system reads page 1 text and tries to match against:
1. **Vendor name** (exact substring match, case-insensitive)
2. **Vendor aliases** (exact substring match, configured in template page)

The vendor ID itself is also checked because some PDFs contain the vendor's own ID code.

### Delete behavior

- Confirms with dialog: `Delete vendor "X" and all its data?`
- `DELETE /vendors/{id}` → removes vendor row
- **Known issue:** Does not cascade to aliases, spatial_memory, templates, or layout_boxes (orphan data remains)

---

## Page 2: Template

**Route:** `#/template/{vendor_id}`
**File:** `frontend/vendors.js` → `renderTemplatePage()`

### What the user configures

The template page has 6 configuration sections:

#### 1. Format Type (dropdown)

| Format | Behavior |
|--------|----------|
| `single_po_multipage` | Page 1 has header + line items. Pages 2+ have line items only, same PO. |
| `po_per_page` | Each page is a self-contained PO with its own header and line items. |
| `single_page` | Entire document is a single page. All fields extracted at once. |

The format type controls:
- How the system prompt is structured (page 1 vs page 2+ prompts differ)
- Whether Qwen is asked for header fields on every page or only page 1
- How the review page groups results (single record vs array of records)

#### 2. Header Fields (mandatory)

These are the top-level fields Qwen must extract. Examples:
- `vendor`, `supplier`, `bill_to`, `ship_to`, `po_number`, `invoice_number`, `date`

The user types a field name, it's normalized (lowercased, spaces → underscores), and added to the list.

**These fields are what Qwen extracts.** If no header fields are configured, Qwen gets no field instructions and extraction quality drops.

Header fields are also used by:
- Spatial memory filtering (only header fields are eligible for reuse)
- Review page rendering (shown as editable input fields in sidebar)

#### 3. Line Item Columns (mandatory)

Column names for the line items table. Examples:
- `no`, `description`, `qty`, `uom`, `unit_price`, `total`

These become the column headers in Qwen's extraction prompt. Qwen returns `line_items: [{no, description, qty, ...}]`.

#### 4. Vendor Aliases

Words/phrases that uniquely identify this vendor in page 1 text. Examples:
- For Robert Scott: `robert scott`, `rs distributors`
- For RJ Schinner: `rj schinner`, `schinner co`

Aliases are stored in `vendor_aliases` table. During vendor detection, page 1 text is searched for these patterns (exact substring match, case-insensitive).

**Important:** Aliases must be unique across vendors. If two vendors share an alias like `scott`, detection becomes ambiguous.

#### 5. Prompt Instructions (free text)

Natural language instructions injected into the system prompt. Examples:
- `Supplier address is always in the top-left block`
- `PO number starts with PO and is 5 digits`
- `Dates should be in MM/DD/YYYY format`

These instructions are read by Qwen on **every page** of the document.

#### 6. Extraction Rules (list)

Structured rules added as bullet points. Examples:
- `Merge line items across pages`
- `Numbers must be numeric, not strings`
- `Skip rows with empty description`

Rules are appended to the system prompt as numbered constraints.

### Admin-only: Prompt Previews

Admin users see 4 prompt preview buttons:
- **Page 1 System** — includes header fields, vendor confirmation instruction, and label/header box schema
- **Page 1 User** — the actual user message with field names and response shape
- **Page 2+ System** — line-items-only prompt (no header fields, no vendor confirmation)
- **Page 2+ User** — continuation page user message

Clients do **not** see these prompt previews. They are hidden by role check.

### Save behavior

1. User clicks **Save Template**
2. `POST /vendors/{vendor_id}/template` with payload: format_type, vendor_name, header_fields, line_item_fields, prompt_instructions, extraction_rules
3. Backend builds the system prompt from these fields
4. Stores in `templates` table with a `prompt_hash` (SHA256 of the prompt content)
5. Returns the hash for display

---

## Page 3: Saved Templates

**Route:** `#/saved-templates`
**File:** `frontend/vendors.js` → `renderSavedTemplatesPage()`

### What it shows

A read-only table of all saved templates across all vendors:

| Column | Content |
|--------|---------|
| Vendor | Vendor name |
| ID | Vendor ID |
| Format | Format type badge |
| Header Fields | Chip list of field names |
| Line Items | Chip list of column names |
| Rules | Count of extraction rules |
| Actions | **Edit** (→ template page) and **Use** (→ extract page with vendor pre-selected) |

### Use shortcut

Clicking **Use** sets `localStorage.extractVendor = vendor_id` and navigates to `#/extract`. The extract page reads this on load and pre-selects the vendor, skipping auto-detection.

---

## Page 4: Extraction

**Route:** `#/extract`
**File:** `frontend/extract.js` → `renderExtractPage()`

### Layout

Three-panel layout:
- **Left sidebar:** Vendor detection card, file upload dropzone
- **Center viewer:** PDF page images with zoom/pan
- **Right panel:** Pipeline visualization, extracted data, JSON actions

### Complete extraction workflow

#### Step 1: File Upload

1. User drops or selects a PDF/image file
2. Frontend sends `POST /upload-preview` with the file (max 5 pages for quick preview)
3. Backend renders page images using pypdfium2, returns base64 page images
4. Frontend displays page 1 in the viewer, enables page navigation
5. Pipeline stage **Upload** → DONE

#### Step 2: Vendor Selection (two paths)

**Path A — Manual selection:**
- User clicks **Extract** from a vendor card on the Vendors page
- `extractVendor` is set in localStorage
- Extract page loads with vendor pre-selected
- Detection stage is **skipped** (marked DONE with "Manual vendor selected")

**Path B — Auto-detection:**
- User uploads PDF without pre-selecting a vendor
- Detection happens during ingestion (see backend pipeline)
- Pipeline stage **Detecting Vendor** → shows "Reading page 1"

#### Step 3: Click EXTRACT

1. Frontend builds `FormData` with file + optional vendor_id
2. `POST /ingest/ui` → backend returns `{job_id, extraction_id, detected_vendor}`
3. If vendor was auto-detected, frontend shows: `"Client Detected: ROBERT SCOTT"`
4. Frontend opens SSE stream: `GET /jobs/{job_id}/stream`

#### Step 4: SSE Pipeline Streaming

The right panel shows a real-time pipeline visualization with 7 stages:

| Stage | Label | What happens |
|-------|-------|-------------|
| `upload` | Uploading | Document stream received (instant) |
| `detect` | Detecting Vendor | Read page 1 text, match against vendors |
| `normalize` | PDF Rendering | Classify pages as digital/scanned, render images |
| `ocr` | OCR Bounding Box | Run PaddleOCR on scanned pages, pypdfium2 on digital pages |
| `llm` | Vision Extraction | Qwen3-VL processes each page (progress: Page X/Y) |
| `json` | JSON Created | Structured output assembled from all pages |
| `postprocess` | Post Processing | Field mapping, spatial memory application, review data |

Each stage shows:
- Status dot (pending/active/done/failed)
- Detail text (updates from SSE events)
- Progress bar (for LLM stage: page X/Y percentage)
- Elapsed timer (total time since extraction started)

#### Step 5: Results

When extraction completes:
- All pipeline stages → DONE
- Extracted JSON shown in the right panel
- Buttons appear: **Copy JSON**, **Download JSON**
- **Open Review** button links to the review page

#### Step 6: Error Handling

If extraction fails:
- Failed stage shows red FAIL badge
- Conflict section shows error message
- **Retry Extraction** button appears
- Timer stops

If vendor is unknown:
- Detection stage fails with: `"No matching vendor found. Create the vendor first."`
- Extraction does not proceed
- User must go to Vendors page → create vendor → configure template → retry

---

## Page 5: History

**Route:** `#/history`
**File:** `frontend/history.js` → `renderHistoryPage()`

### What it shows

A chronological list of all extractions for the current user:

| Info | Content |
|------|---------|
| Filename | Original uploaded filename |
| Vendor | Vendor name (blue) |
| Status | done / partial / failed / cancelled (color-coded badge) |
| Pages | Total page count |
| Date | Creation timestamp |
| Latency | End-to-end extraction time (e.g. 4.2s) |

### Actions per extraction

- **Click row** → opens detail overlay with full JSON result, page results, copy button
- **Review** link → navigates to `#/review/{id}`
- **Delete** button → confirms then `DELETE /extractions/{id}`

### Delete behavior

- Removes the extraction row and its page artifacts
- **Known billing issue:** `llm_usage` FK has `ON DELETE SET NULL`, so deleting extractions reduces the user's billable page count (users can "game" billing by deleting old extractions)

---

## Page 6: Review (HITL)

**Route:** `#/review/{extraction_id}`
**File:** `frontend/review.js` → `renderReviewPage()`

### Layout

Three-panel layout:
- **Left sidebar:** Editable extracted fields + line items table
- **Center viewer:** PDF page image with mapping rectangles overlay
- **Right panel:** Live JSON output

### How it works

#### Data Loading

1. `GET /extractions/{id}` → loads extraction result (corrected_result or result)
2. `GET /extractions/{id}/pages` → loads page images
3. `GET /extractions/{id}/ocr` → loads unified geometry data (word boxes from pypdfium2 or PaddleOCR)
4. `GET /vendors/{vendor_id}/gold-corrections` → loads correction history metadata
5. `GET /extractions/{id}/spatial-memory-fields` → loads which fields have spatial memory

#### Field Display

Each header field shows:
- **Colored dot** indicating match quality:
  - 🟢 Green = high confidence match (found in OCR/geometry)
  - 🟡 Yellow = low confidence or fuzzy match
  - 🔵 Blue = manual correction (user-drawn box)
  - ⚫ Gray = unmatched (no geometry found)
- **Field name** label
- **Editable input** with current value
- **✎ Draw button** to enter selection mode
- **↻ Reset button** (appears when value differs from original)

#### Blue Dotted Rectangles (Mapping Rects)

On the PDF image, blue dotted rectangles show where each field's value was found:
- Rectangles are positioned using the `field_locations` data
- Each rect has a label (field name)
- SVG lines connect sidebar fields to their corresponding rectangles on the PDF
- Hover highlighting: hovering a field highlights its rect and vice versa

#### Draw-to-Correct (Selection Mode)

1. User clicks **✎** next to a field (e.g. `ship_to`)
2. Cursor changes to crosshair, toast says "Draw a box on the PDF to set ship to"
3. User draws a rectangle on the PDF page
4. System finds all OCR/geometry words inside the drawn rectangle
5. Preview bar appears: "Selected: 456 OAK AVE, SUITE 100" with Accept/Reject buttons
6. **Accept:** Updates field value, saves `field_locations` with `strategy: "manual"`, marks field as changed
7. **Reject:** Cancels selection, reverts to previous value

#### Line Items Table

- Shown below header fields in the sidebar
- Columns match the template's `line_item_fields`
- Each cell is clickable for draw-to-correct
- Cells color-coded by match quality (same as header fields)
- For `single_po_multipage`: only shows line items for the current page
- For `po_per_page`: shows all line items for the current record

#### Alt+Hover OCR Overlay

Holding **Alt** key shows all OCR word boxes on the current page as transparent overlays. This lets the user see what text the system detected and verify geometry accuracy.

#### Confirm Flow

1. User clicks **Confirm**
2. If there are typed-only changes (no box drawn), a confirmation dialog warns:
   > "Manual typed edits will override Qwen's final JSON value. These typed edits will NOT create spatial memory because no value box was selected."
3. `POST /extractions/{id}/review` sends: `corrected_result`, `field_locations`
4. Backend:
   - Saves corrected result
   - Saves field locations (per-extraction snapshot)
   - Saves spatial memory for eligible header fields with `strategy: "manual"` (reusable across future documents)
   - Creates gold correction hints (for future LLM prompt injection)

#### Other Actions

- **Undo (Ctrl+Z):** Reverts last field correction
- **Download JSON / Copy JSON:** Export current state
- **Back to Extract:** Returns to extraction page

---

## Page 7: Dashboard

**Route:** `#/dashboard`
**File:** `frontend/dashboard.js` → `renderDashboardPage()`

### Client view

Shows the current user's own usage:

**KPI Cards:**
- Total Pages processed
- Total Extractions completed
- Input Tokens (prompt)
- Output Tokens (completion)
- Grand Total tokens
- LLM Calls count

**Charts:**
- Token Trend bar chart (last 30 days, input vs output)
- Input/Output split donut chart

**Daily Breakdown Table:**
- Date, Input, Output, Total, Docs, Calls, Avg Latency

**PDF Usage Section:**
- Filter by: Today, 7D, 30D, All, or custom date range
- Table of all PDFs: filename, vendor, date, billable pages, status, tokens, latency
- Expandable per-page detail (page number, type, input/output tokens, latency)

### Admin view

Same KPI cards and charts but for **all users combined**.

Additional section: **Client Token Breakdown** table showing per-client aggregates with a **View** button that navigates to the client's individual dashboard.

### Admin → Client Dashboard

**Route:** `#/admin/client/{user_id}`

Shows the same dashboard layout but filtered to one specific client. Includes date range filtering and per-PDF usage breakdown.

---

## Page 8: Admin — User Management

**Route:** `#/admin/users`
**File:** `frontend/admin.js` → `renderAdminUsersPage()`

### User table

| Column | Content |
|--------|---------|
| Email | User email |
| Role | ADMIN (blue) or CLIENT (gray) |
| Status | ACTIVE (green) or INACTIVE (gray) |
| Page Limit | Subscription limit (or NOT SET in red) |
| Created | Date |
| Actions | Deactivate, Reset PW, Edit limit |

### Create User

- Admin clicks **+ New User**
- Modal: email, password (min 8 chars), confirm password, role (client/admin)
- `POST /admin/users` → creates user with bcrypt-hashed password

### Reset Password

- Admin clicks **Reset PW** on any user (except self)
- Modal: new password + confirm
- `PATCH /admin/users/{id}/password`

### Set Page Limit (Subscription)

- Admin clicks **Edit** next to page limit
- Modal: numeric input for subscription page limit
- `PATCH /admin/users/{id}/subscription-limit`
- Set to **0** → blocks all uploads for that client
- Limit enforced at ingestion time: if `used >= limit`, upload is rejected

### Deactivate User

- Admin clicks **Deactivate** (not available for self)
- Confirms dialog → `DELETE /admin/users/{id}` → sets `is_active = false`
- Deactivated users cannot log in

---

## Backend Pipeline — Stage by Stage

When `POST /ingest/ui` is called, the backend creates a durable job that progresses through these stages:

### Stage 1: Normalize

**File:** `backend/worker.py` → `_process_normalize()`

1. Read uploaded PDF bytes
2. For each page, run `pypdfium2`:
   - Count embedded characters and words
   - If char_count > threshold → page is **digital**
   - If char_count ≤ threshold → page is **scanned**
3. Render each page as an image (for Qwen and review UI)
4. Store page images in DB (`extraction_pages` table)
5. Emit SSE: `{stage: "normalize", digital_pages: X, scanned_pages: Y}`

### Stage 2: OCR (Geometry)

**File:** `backend/worker.py` → `_process_ocr()`

1. For **digital** pages: extract word boxes from pypdfium2 (already done in normalize)
2. For **scanned** pages: run PaddleOCR to get word boxes
3. Merge into unified geometry format:
   ```json
   {
     "page_number": 1,
     "source": "pypdfium",
     "words": [{"text": "ROBERT", "box": [10, 20, 80, 40], "score": 1.0}]
   }
   ```
4. Store unified geometry as `ocr_data` in DB
5. Emit SSE: `{stage: "ocr", message: "Geometry complete"}`

### Stage 3: LLM (Vision Extraction)

**File:** `backend/worker.py` → `_process_llm()` + `backend/extractor.py`

1. Load template for the vendor (system prompt, fields, rules)
2. For page 1:
   - Build system prompt with header fields + line item columns + vendor confirmation + label/header box schema
   - Send page 1 image to Qwen3-VL with the prompt
   - Qwen returns: extracted fields JSON + anchor/header bounding boxes
   - Verify vendor: if Qwen says `vendor_confirmed: false`, mark extraction as unverified
3. For pages 2+:
   - Build continuation prompt (line items only, no header fields)
   - Send each page image to Qwen
   - Qwen returns: line items for that page
4. Record LLM usage (tokens, latency) in `llm_usage` table
5. Emit SSE per page: `{stage: "llm", page: X, total_pages: Y}`

### Stage 4: Postprocess

**File:** `backend/worker.py` → `_process_postprocess()`

1. **Merge page results:** Combine all page results into final JSON
2. **Field mapping:** Match extracted values to word-level geometry from OCR data
3. **Spatial memory lookup:** Check if this vendor+layout has saved field regions
4. **Apply spatial memory:** If a saved region exists, read current text from that region (NOT old values)
5. **Build field_locations:** Map each field to its geometry (page, box, strategy, confidence)
6. **Gold correction hints:** Load prior correction hints and inject into results metadata
7. Store final result, page results, field locations, and OCR data
8. Emit SSE: `{event: "done", extraction: {result, field_locations, ...}}`

---

## Spatial Memory System

**File:** `backend/spatial_memory.py`

### Purpose

When a user manually corrects a field in Review (draws a box), that region is saved. On the next document from the same vendor with the same layout, the system:

1. Loads the saved region for each field
2. Converts normalized coordinates to current page coordinates
3. Reads the **current text** inside that region from the **current document**
4. Uses that as the first-pass candidate (before Qwen's extraction)

### Critical rule: geometry memory, NOT answer memory

- **Store:** field key, normalized box, page number, vendor_id, layout_key
- **Do NOT store as future answer:** the old corrected text value
- The reused answer always comes from the current document's text inside the saved region

### Layout key

Spatial memory is scoped by `vendor_id` + `layout_key`. The layout key prevents one vendor's multiple layouts from sharing wrong memory.

Layout key is computed from:
- Vendor ID
- Template ID
- Stable anchor/header positions (hashing prominent anchor text)

### Eligible fields (phase 1)

Only header fields are eligible for spatial memory:
- vendor, supplier, bill_to, ship_to, deliver_to, po_number, invoice_number, date fields

Line item row-level memory is NOT stored (too volatile across documents).

---

## Billing & Subscription

### How billing works

1. Admin sets `subscription_limit` per user (e.g. 1000 pages)
2. Each LLM call records a row in `llm_usage` with: extraction_id, page_num, tokens, latency
3. Billable pages = count of DISTINCT (extraction_id, page_num) where extraction_id IS NOT NULL
4. At ingestion: if `used >= limit` → upload rejected with HTTP 402

### Soft limit model

The system uses a **soft limit**: the user can go slightly over on their last allowed PDF (because pages are counted after extraction, not before). But the **next** upload is blocked.

### Known billing bug

Deleting extractions sets `llm_usage.extraction_id = NULL` (FK ON DELETE SET NULL), which causes those pages to drop out of the billable count. Users can reduce their bill by deleting old extractions.

---

## Feature Implementation Status

### ✅ Implemented and Working

| Feature | Status |
|---------|--------|
| Hybrid digital/scanned page classification | ✅ Per-page pypdfium2 → PaddleOCR fallback |
| Vendor detection from page 1 text | ✅ Exact match + alias lookup |
| Unknown vendor blocking | ✅ Returns structured error, does not auto-create |
| Template configuration (format, fields, rules, aliases) | ✅ Full UI |
| Multi-page extraction with Qwen3-VL | ✅ Page-by-page with SSE progress |
| Unified geometry (pypdfium2 + PaddleOCR) | ✅ Same word-box schema for both engines |
| Review UI with drag-draw correction | ✅ Full HITL flow |
| Spatial memory save from manual corrections | ✅ Saves normalized box per field |
| Spatial memory reuse on future documents | ✅ Reads current text from saved region |
| Gold correction hints (value-redacted) | ✅ Injected into system prompt |
| Durable job system with SSE streaming | ✅ Replaced old inline extraction |
| JWT authentication + role-based access | ✅ Admin/client isolation |
| Subscription page limits | ✅ Soft limit with admin control |
| Dashboard with token analytics | ✅ Charts, daily breakdown, per-PDF detail |
| Admin user management | ✅ Create, deactivate, reset PW, set limits |

### 🔴 Known Bugs (Confirmed by Audit)

| Bug | Impact | Priority |
|-----|--------|----------|
| Billing game (delete extraction → reduce bill) | Users get free pages | CRITICAL |
| LLM crash on malformed response (missing `choices` key) | Unhandled KeyError crashes extraction | CRITICAL |
| Digital zero-word page (>50 chars but 0 words) skips OCR | Page has no geometry for review | HIGH |
| Quota fails open (DB error → continues without check) | Bypass subscription limit | HIGH |
| Vendor delete doesn't cascade (orphan aliases, memory, boxes) | DB bloat | MEDIUM |
| Extraction delete doesn't cascade jobs/pages | DB bloat | MEDIUM |
| Substring alias matching ambiguity | Wrong vendor detection with short aliases | MEDIUM |
| No mandatory field validation on template save | Empty templates produce poor extraction | MEDIUM |
| SSE "unverified" not treated as terminal state | UI spinner hangs forever | MEDIUM |

### 🔮 Planned / Future

| Feature | Description |
|---------|-------------|
| Spatial memory comparison UI | Show Qwen value vs spatial memory value, let user choose |
| Line item column anchor reuse | Reuse column header positions (not row data) |
| Rename `/ocr` endpoint to `/geometry` | Cosmetic cleanup for clarity |
| Unsaved changes warning on Review page | Prevent accidental navigation with pending edits |
| Near-limit warning in frontend | Show quota warning before hitting the wall |
| History list optimization | Don't return full result payloads in list view |
| Concurrent PDF upload support | Process multiple PDFs sequentially from a batch upload |
