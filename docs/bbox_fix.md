# Bounding Box Fix Spec

## Current Problem

### System Roles
- **Qwen JSON** is the source of truth for field names and field values.
- **PaddleOCR** is the source of truth for spatial words and bounding boxes.
- **`qwen_backend/text_matcher.py`** is responsible for joining those two worlds.

### Why the Current Matcher Is Failing
The live matcher still produces incorrect boxes because it mixes text matching and geometry in the wrong order.

There are three primary failure classes:

#### 1. Header fields are still wrong because Stage 1 is value-first, not key+value proximity-first
- Example: `ship_to` can match the top company/logo block instead of the real `SHIP TO` block.
- Root cause: the matcher finds a key once, then still resolves value lines independently and can fall back globally.
- Result: a multiline field can stitch together lines from two different page regions and produce one oversized union box.

#### 2. Line-item columns are still wrong because table header detection is not group-aware
- Example: `no` can incorrectly match `Purchase Order No.` or `Account No.`.
- Root cause: `_find_page_column_headers()` still scores one OCR line at a time from independent per-column matches.
- Result: isolated short-token hits are accepted even when they do not belong to the actual table header row.

#### 3. Line-item columns can disappear even when values are confirmed
- Example: `description` or another column is value-confirmed, but no `line_item_*` output is emitted.
- Root cause: Stage 2 currently requires both `confirmed` and `column_anchor_box` before emitting.
- Result: a column can be confirmed from values and still be silently dropped because header anchoring was weak or missing.

## What Correct Behavior Looks Like

### Top-Level Header Fields
For fields like `ship_to`, `supplier`, `account_no`, `deliver_to`, and similar non-line-item fields:
- Search both the **normalized field key** and the **extracted value**.
- Prefer value candidates that are spatially closest to the correct key label.
- Prefer value blocks that are geometrically plausible relative to the key:
  - directly below the key
  - or to the right of the key
- Final emitted header `box` must be the **value block only**.
- Do **not** emit a key+value union box.

### Manual Review Reuse
Manual drag-drop corrections in Review should become reusable **spatial memory** for future documents of the same vendor/layout.

The important rule is:
- store **where the field is**
- do **not** store the old field value as the future answer

Example:
- Robert Scott PDF 1:
  - `supplier` region is corrected manually
  - OCR text inside that region = `Fresh Products`
- Robert Scott PDF 2:
  - same layout, same `supplier` region
  - OCR text inside that region = `Bluechip`

Correct behavior:
- reuse the saved `supplier` region as the first-pass search area
- read the **new OCR text** inside that area
- return `Bluechip`, not `Fresh Products`

### Line Items
For line-item columns:
- Find the **table header row/window first**.
- Treat the table header as a **grouped structure**, not isolated label hits.
- Use the selected table header to guide column positions.
- Keep line-item `box` anchored to the **column/header anchor**.
- Keep the actual matched cell location in `value_box`.

## Required Normalization Rules

### Field Key Normalization
All Qwen field keys must be normalized before header search:
- lowercase the full key
- replace `_` with spaces
- collapse repeated whitespace

Examples:
- `ACCOUNT_NO` -> `account no`
- `SHIP_TO` -> `ship to`
- `PURCHASE_ORDER` -> `purchase order`

### Explicit Alias Expansion
Alias expansion is intentionally narrow in this pass.

Only these aliases are allowed:
- `uom` -> `uom`, `u m`, `u/m`, `um`
- `qty` -> `qty`, `quantity`, `qty.`

No other alias families should be introduced in this change.

## Codex Implementation Plan

### Stage 0: Spatial Memory From Manual Review
Codex should add a reusable spatial-memory layer for manually corrected header fields.

#### Scope
This memory is for **stable-layout top-level header fields** only in this pass, such as:
- `supplier`
- `ship_to`
- `bill_to`
- `account_no`
- `order_number`
- `order_date`

Do **not** use this first pass for line items.

#### What to store
When a user manually corrects a field in Review, store a reusable field anchor keyed by:
- vendor
- template / format
- field name
- page number or page rule

Store the box as **normalized coordinates**, not raw page pixels:
- `x0 / page_width`
- `y0 / page_height`
- `x1 / page_width`
- `y1 / page_height`

Optional supporting metadata may also be stored:
- page width and height at save time
- nearby key label text if known
- extraction id that created the anchor
- confidence / source = manual

#### What not to store as future truth
Do **not** reuse the old corrected value itself as the future answer.

The saved memory is only:
- the likely field region
- not the field text

#### Runtime behavior on future documents
On the next extraction for the same vendor/layout:
- load any saved manual field anchor for that field
- map the normalized box back to the current page dimensions
- use that box as a **first-pass search region**
- search PaddleOCR text inside or near that region
- emit the **new OCR value** found there

If region-based matching is weak or fails:
- fall back to the normal Stage 1 key+value proximity matcher

#### Hard rule
The saved box is a **search prior**, not a blind final answer.

That means:
- the final emitted value must come from current OCR / current extraction
- the system must not copy the old value from the database into the new result

This is required for cases like:
- PDF 1 `supplier = Fresh Products`
- PDF 2 `supplier = Bluechip`
- same location, different value

The future extraction must return `Bluechip`.

### Stage 1: Header Matching With Key + Value Proximity
Codex should replace the current Stage 1 behavior with key+value proximity matching.

#### Required behavior
- Build normalized key variants from the field name.
- Search **all pages** for key candidates.
- Search **all pages** for value candidates.
- For multiline values, build **candidate value blocks** from vertically contiguous OCR lines rather than resolving each line independently in isolation.

#### Candidate scoring
Every key/value pairing should be scored using:
- key match quality
- value match quality
- same-page preference
- spatial closeness
- expected geometry:
  - value below key is preferred
  - value to the right of key is acceptable
- multiline continuity

#### Hard rule
When a key-anchored search is active on a page, do **not** fall back to unrestricted global line search for that same line candidate.

This is required to stop the current bug where:
- the first line of a header block comes from one region
- later lines come from a different region
- the final box becomes a cross-region union

#### Final emission
- Emit the selected value block as the final header `box`.
- Keep any key box only as an internal anchor or optional debug metadata.
- If a reusable manual field anchor exists for this field, use that anchor region as the first candidate search area before wider page search.

### Stage 2: Line-Item Table Header Discovery
Codex should change line-item header discovery from isolated token matching to grouped table-header selection.

#### Required behavior
For each page:
- build OCR lines
- evaluate candidate **1-line** and **2-line** header windows
- search normalized Qwen line-item keys within each window

#### Candidate window scoring
Each candidate table-header window should be scored by:
- number of distinct matched columns
- left-to-right ordering consistency
- horizontal spread across the table
- compactness / row coherence
- position above the data rows

#### Selection rule
Select one best table-header window per page.

#### Hard rules
- Short labels like `no` are invalid unless they belong to the selected table-header window.
- `Purchase Order No.` and `Account No.` must **not** be accepted as the line-item `no` header unless that entire administrative row somehow wins the grouped table-header score, which it should not.

### Stage 3: Row Anchoring and Column Confirmation
Codex should keep the row-wise idea, but constrain it under the selected table region.

#### Required behavior
- Find strong row anchors **below** the selected table-header window.
- Build row y-bands from those anchors.
- Interpolate missing rows when some anchors are absent.
- For each column, search values inside both:
  - the row band
  - the column x-band guided by the selected header

#### Confirmation behavior
- If the header text is weak but value clustering is strong, allow column confirmation from values.
- If the column is confirmed but no explicit header text box is found, synthesize an anchor from the confirmed column band and the selected header window rather than dropping the whole column.

#### Hard rule
Do not drop a confirmed column only because `column_anchor_box` could not be created from a direct header text hit.

This is required to fix the current bug where:
- values confirm the column
- but nothing is emitted because header anchoring was missing

### Stage 4: OCR Box Reservation
Codex should preserve OCR reservation behavior with one important constraint.

#### Required behavior
- Repeated values across rows must not reuse the same OCR token.
- Sibling columns must still be able to use different subspans from the same OCR source word.

Example:
- OCR word: `45 CS`
- valid matches:
  - `quantity = 45`
  - `uom = CS`

The reservation system must block duplicate reuse across rows without blocking valid sibling subspans in the same row.

### Stage 5: Box Emission Contract
The output contract must remain stable and explicit.

#### Header fields
- `box` = final matched **value block**

#### Line-item fields
- `box` = **column/header anchor box**
- `value_box` = actual matched row value box, when found
- `match_mode` must be one of:
  - `column_exact`
  - `column_fuzzy`
  - `column_inferred`

#### Hard rule
Do **not** switch line-item `box` back to the raw row value box.

The column/header anchor remains the UI-facing line-item box.

## Edge Cases

The implementation must explicitly handle the following:

### Header-field edge cases
- `ship_to` value appears in both a logo/company block and a real address block
- `supplier` value appears in multiple page regions
- multiline addresses have partial OCR line splits
- one value line is missing but nearby lines are still correct
- key label is weak or fuzzy, but the value block is clearly near it
- a manually corrected field region exists from a previous extraction and should be reused as a first-pass region
- the same field region is stable across documents but the field value changes between documents
  - example: PDF 1 `supplier = Fresh Products`, PDF 2 `supplier = Bluechip`

### Table-header edge cases
- table headers split across two OCR lines
- merged header text like `QUANTITYUOM`
- short labels like `no`, `qty`, `date`
- administrative rows containing overlapping text such as:
  - `Purchase Order No.`
  - `Account No.`

### Row and column edge cases
- merged cell text like `45 CS`
- duplicate row anchors across multiple rows
- repeated values inside the same column
- pages where table headers are weak or partially missing
- pages after page 1 where a repeated table header may be weaker or absent
- columns confirmed from values but missing explicit header text

### Omission behavior
- If no reliable table-header window exists for a page, omit line-item mappings for that page rather than guessing globally.

## Acceptance Tests

The implementation is only acceptable if these scenarios pass:

- `ship_to` chooses the value block nearest `SHIP TO`
- `supplier` chooses the value block nearest `SUPPLIER`
- multiline header values do not stitch together lines from different regions
- a manually corrected `supplier` region saved from one extraction is reused on the next same-format document
- when the saved `supplier` region is reused and the text changes between documents, the matcher returns the **new OCR value**, not the old saved value
- line-item `no` does not bind to `Purchase Order No.` or `Account No.`
- a grouped table-header row with `No.`, `Variant`, `Description`, `Supplier Code` wins over administrative rows
- `qty` matches `Qty`, `Quantity`, and `qty.`
- `uom` matches `uom`, `u m`, `u/m`, and `um`
- merged header `QUANTITYUOM` still supports both `qty` and `uom`
- merged value `45 CS` supports both quantity and uom
- confirmed columns still emit line-item locations even when explicit header text is weak or absent
- line-item output keeps `box` at the column/header anchor and `value_box` at the row value

## Locked Assumptions

- The live implementation target is `qwen_backend/text_matcher.py`.
- `text_matcher_new.py` is **not** the source of truth for this spec.
- Top-level header final boxes should stay on value blocks.
- Line-item final boxes should stay on column/header anchors.
- Manual drag-drop corrections should be reusable as **field-region memory** for future same-layout documents.
- Reused field memory should bias search to the saved region but must still read the **current OCR value** from the new document.
- Only `qty` and `uom` receive explicit alias expansion in this change.
- If the matcher cannot confidently identify a table-header window for a page, it should prefer omission over a guessed mapping.
