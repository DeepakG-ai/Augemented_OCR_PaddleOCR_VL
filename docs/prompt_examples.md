# Prompt Examples

This document shows the exact prompts the LLM model receives for all extraction
modes. Fields are fully dynamic — users define header and line item column
names in the UI.

---

## Mode 1: Auto Extract (no fields selected)

The system prompt is built from the user's template configuration (instructions,
rules, format_type). No specific fields are injected.

### System Prompt

```
You are a highly accurate document data extraction assistant. Extract ONLY what is explicitly visible in the document image. Never guess or fabricate data. If a field is not visible, set it to null.

DOCUMENT CONTEXT:
Supplier is in top-left block. PO number is labelled Purchase Order No.

EXTRACTION RULES:
  1. PO number: PO followed by 5 digits
  2. Amount = Qty x Unit Cost

DOCUMENT FORMAT:
This document is a single purchase order that spans multiple pages. Header fields appear on page 1; line items may continue across subsequent pages.

CRITICAL: Count the number of rows in the line items table FIRST, then extract that exact number of items.

OUTPUT RULES:
  - Return ONLY valid JSON. No markdown fences, no explanation, no extra text.
  - Use null for missing fields, never omit them.
  - For line_items, return an array even if only one item exists.
  - Numbers should be numeric (not strings) when possible.
  - Dates should be in the format they appear in the document.
```

### User Message (same for every page)

```
Extract ALL data from this invoice/purchase order document (page 1 of 4).

CRITICAL: Count the number of rows in the line items table FIRST, then extract that exact number of items.

Return in this JSON format:
- Header fields: extract all visible header fields (po_number, order_date, vendor, bill_to, ship_to, etc.)
- Line items: extract all visible line item rows with all columns

Strictly return in JSON format. Do NOT include markdown fences, explanations, or any extra text. Return ONLY valid JSON.

ACCURACY REQUIREMENTS:
- Before extraction: Count total rows in the table visually.
- After extraction: Verify your line_items array has that many items.
- Double-check you didn't skip rows at page breaks or table headers.
- Extract ONLY what is explicitly visible in the document image.
- Never guess or fabricate values.
```

---

## Mode 2: Extract Fields (specific fields selected)

The system prompt is the same as above. The user message dynamically injects
the selected header_fields and line_item_fields.

### Example Configuration

- **Header fields**: `supplier`, `bill_to`, `date`, `po_number`, `phone_number`
- **Line item columns**: `no`, `variant`, `description`, `qty`, `uom`, `unit_cost`, `amount`

### User Message (same for EVERY page — rj_schinner approach)

```
Extract the header fields AND all visible line item rows from this purchase order page (page 1 of 4).
If any field is empty or not visible, return null in the JSON object.

Header fields to extract:
  - supplier
  - bill_to
  - date
  - po_number
  - phone_number

Line item columns to extract (each row):
  - no
  - variant
  - description
  - qty
  - uom
  - unit_cost
  - amount

Return ONLY valid JSON matching EXACTLY this structure:
{
  "supplier": null,
  "bill_to": null,
  "date": null,
  "po_number": null,
  "phone_number": null,
  "line_items": [
    {
      "no": "",
      "variant": "",
      "description": "",
      "qty": "",
      "uom": "",
      "unit_cost": "",
      "amount": ""
    }
  ]
}

Rules:
- Empty or missing cells → null.
- Numbers (qty, unit_cost, amount, unit_price, etc.) must be numbers, not strings.
- Extract every visible line item row.
```

---

## Merging Logic (single_po_multipage)

Every page gets the **same prompt** (rj_schinner approach). The merger handles
combining results:

1. **Header fields**: First non-null value from page 1 wins
2. **Line items**: Concatenated from all pages with deduplication
3. **Dedup key**: First 3 line item columns (e.g., `no`, `variant`, `description`)

```
Page 1 → header fields + 5 line items
Page 2 → header fields (ignored by merger) + 3 line items
Page 3 → header fields (ignored by merger) + 2 line items
─────────────────────────────────────────────
Merged → header from page 1 + 10 line items (deduped)
```

---

## Format Types

| Format | Behavior |
|--------|----------|
| `single_po_multipage` | Same prompt every page. Merger: header from pg 1, concat line_items. |
| `po_per_page` | Same prompt every page. Each page returned as independent record. |
| `single_page` | Single page. No merging needed. |
