# Qwen Prompt Plan for Anchor BBoxes

## Goal
Use Qwen to return:

- extracted field values
- bounding boxes for requested header-field anchors and requested line-item column headers only

Do not use Qwen for value-level bounding boxes.
PaddleOCR remains the source of all word-level boxes and is used for drag-drop and manual value correction.

## Core Model

- Qwen box = anchor box
- PaddleOCR box = exact word box / manual value box

This means:

- header fields:
  - Qwen returns the extracted field value
  - Qwen returns one anchor bbox for the requested field key/label region
- line items:
  - Qwen returns all row values in `line_items`
  - Qwen returns one bbox per requested line-item column header only
  - every row in that column will visualize against the same header/column anchor

## Output Contract

Qwen should return exactly two top-level keys:

- `fields`
- `boxes`

### Required Shape

```json
{
  "fields": {
    "vendor": "FRESH PRODUCTS, INC.\nPO BOX 933189\nCLEVELAND, OH 44193\n419-531-8472",
    "bill_to": "RJ SCHINNER CO., INC\nN89 W14700 PATRITA DRIVE\nMENOMONEE FALLS WI 53051",
    "ship_to": "RJ SCHINNER - VANCOUVER WA\n12225 NE 60th Way\nVANCOUVER WA 98682\n360-940-1200 Fax 360-253-2343",
    "line_items": [
      {
        "item": "3WDS-F-02-BX",
        "pack": "10/bx",
        "ship_qty": 78,
        "order_qty": 78,
        "unit_price": 15.69
      },
      {
        "item": "3WDS-F-01-BX",
        "pack": "10/bx",
        "ship_qty": 6,
        "order_qty": 6,
        "unit_price": 15.69
      }
    ]
  },
  "boxes": {
    "vendor": [100, 220, 180, 245],
    "bill_to": [420, 220, 500, 245],
    "ship_to": [760, 220, 835, 245],
    "item": [120, 520, 180, 545],
    "pack": [480, 520, 540, 545],
    "ship_qty": [620, 520, 700, 545],
    "order_qty": [760, 520, 845, 545],
    "unit_price": [930, 520, 1015, 545]
  }
}
```

## Meaning of Each Section

### `fields`

Contains extracted values only.

- header fields live directly under `fields`
- line-item rows live under `fields.line_items`

### `boxes`

Contains anchor/header boxes only.

- header field keys map to the anchor bbox for that requested field
- line-item column names map to the table-header bbox for that requested column

Examples:

- `boxes.vendor` = bbox for the `VENDOR` anchor region
- `boxes.ship_to` = bbox for the `SHIP TO` anchor region
- `boxes.item` = bbox for the `ITEM` table header
- `boxes.unit_price` = bbox for the `UNIT PRICE` table header

## Important Rules

### What Qwen Must Return

- field values in `fields`
- one anchor bbox for each requested header field in `boxes`
- one header bbox for each requested line-item column in `boxes`

### What Qwen Must Not Return

- no bounding boxes for header field values
- no bounding boxes for line-item row values
- no row-wise line-item cell boxes
- no duplicate page metadata when processing one page at a time
- no `matched_label`
- no explanation text
- no markdown fences

## Missing Data Rules

- if a requested field value is not visible, return `null` in `fields`
- if a requested anchor/header is not visible, return `null` in `boxes`
- do not omit keys

Example:

```json
{
  "fields": {
    "vendor": null
  },
  "boxes": {
    "vendor": null
  }
}
```

## Coordinate Rules

- every bbox must be `[x0, y0, x1, y1]`
- coordinates must be in page image coordinates
- the bbox should tightly cover the anchor/header text region
- for line-item columns, the bbox should cover the header text only, not the full column

## Multipage Rule

Each page is processed independently by Qwen.

That means:

- Qwen returns one page-local JSON result
- merge happens later in backend code
- `boxes` are page-local anchors

This is important for line items:

- page 1 may have one set of table header coordinates
- page 2 may repeat the table header at different coordinates
- merge logic must keep the page association of each page's `boxes`

## Why This Design Is Correct

This format matches the actual architecture:

- Qwen = semantic extraction + anchor/header location
- PaddleOCR = full text geometry + drag-drop correction

It avoids the earlier problem where Qwen was expected to act like a full OCR engine.
It also keeps token usage lower than returning inline `_bbox` fields for every value.

## System Prompt Requirements

The system prompt should instruct Qwen to:

- extract requested values into `fields`
- return anchor/header boxes only in `boxes`
- never produce value-level boxes
- count visible line-item rows first
- return only valid JSON

### Recommended System Prompt Core

```text
You are a highly accurate document extraction assistant.

This request is processed one page at a time.

Return exactly two top-level keys:
- `fields`: extracted values
- `boxes`: bounding boxes for requested header-field anchors and requested line-item column headers only

Rules:
- Extract only what is explicitly visible in the page image.
- Never guess or fabricate data.
- If a requested value is not visible, return null for that field.
- If a requested anchor/header box is not visible, return null for that box.
- Return bounding boxes only for requested field anchors and requested table column headers.
- Do not return bounding boxes for field values.
- Do not return row-level line item bounding boxes.
- For line items, return all visible rows on the page.
- Count visible line-item rows first, then return that exact number of rows.
- Numbers should be numeric when possible.
- Dates should stay in the format visible in the page.
- Every bounding box must be [x0, y0, x1, y1].
- Return only valid JSON, with no markdown fences and no extra explanation.
```

## User Prompt Requirements

The user prompt should declare:

- requested `header_fields`
- requested `line_item_columns`
- exact JSON shape to return

### Recommended User Prompt Template

```text
Extract the requested fields from this page.

<header_fields>
  - vendor
  - bill_to
  - ship_to
</header_fields>

<line_item_columns>
  - item
  - pack
  - ship_qty
  - order_qty
  - unit_price
</line_item_columns>

Return JSON in exactly this shape:
{
  "fields": {
    "vendor": null,
    "bill_to": null,
    "ship_to": null,
    "line_items": [
      {
        "item": null,
        "pack": null,
        "ship_qty": null,
        "order_qty": null,
        "unit_price": null
      }
    ]
  },
  "boxes": {
    "vendor": null,
    "bill_to": null,
    "ship_to": null,
    "item": null,
    "pack": null,
    "ship_qty": null,
    "order_qty": null,
    "unit_price": null
  }
}
```

## Backend Parsing Plan

The backend should parse:

- `result["fields"]`
- `result["boxes"]`

Recommended handling:

- use `fields` as the extracted data payload
- use `boxes` as Qwen anchor metadata
- for line items, store the page-local column header box map
- keep PaddleOCR as the only source for exact word-level geometry and manual drag-drop corrections

## Visualization Plan

### Header Fields

- use the extracted value from `fields`
- use the Qwen anchor from `boxes`
- if the user manually corrects by drag-drop, store the Paddle value box separately

### Line Items

- use `fields.line_items` for row data
- use `boxes.<column_name>` as the single visualization anchor for that column
- all rows in one column map to the same header anchor
- optional `value_box` can be added later only from Paddle/manual correction, not from Qwen

## Edge Cases

### Header Field Value Appears in Multiple Places

Example:

- `ship_to` company name appears once in a logo block and once in the actual ship-to block

Expected behavior:

- `fields.ship_to` contains the extracted value
- `boxes.ship_to` points only to the `SHIP TO` anchor/header region
- manual correction from Paddle can still refine the actual value region later

### Table Header Repeats on Page 2

Expected behavior:

- page 2 returns a new page-local `boxes` map
- merge logic must preserve which page each column box belongs to

### Missing Table Header on a Continuation Page

Expected behavior:

- if a requested column header is not visible on that page, return `null` for that box
- do not invent a new header box

## Acceptance Criteria

This plan is correct if:

- Qwen returns `fields` and `boxes` only
- Qwen never returns row-wise line-item boxes
- Qwen never returns value-level boxes
- header fields have one anchor box each
- line-item columns have one header box each
- missing values are `null`
- missing boxes are `null`
- output stays compact and page-local

## Locked Decision

Use this output format:

```json
{
  "fields": { ... },
  "boxes": { ... }
}
```

Do not use:

- inline `vendor_bbox` / `ship_to_bbox` fields
- `matched_label`
- page number inside every box object
- row-level line-item bbox output from Qwen
