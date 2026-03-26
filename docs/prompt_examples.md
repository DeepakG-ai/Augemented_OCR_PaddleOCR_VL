# Prompt Examples

This document shows the exact prompts the LLM model receives for all 3 format
types. Fields are fully dynamic -- users define header and line item column
names in the UI.

## Example Configuration

- **Header fields**: `supplier`, `bill_to`, `date`, `po_number`, `phone_number`
- **Line item columns**: `no`, `variant`, `description`, `qty`, `uom`, `unit_cost`, `amount`
- **Instructions**: "Supplier is in top-left block. PO number is labelled Purchase Order No."
- **Rules**: ["PO number: PO followed by 5 digits", "Amount = Qty x Unit Cost"]

---

## System Prompt (shared across all formats, cached in DB + Redis)

```
You are a highly accurate document data extraction assistant. Extract ONLY what is explicitly visible in the document image. Never guess or fabricate data. If a field is not visible, set it to null.

DOCUMENT CONTEXT:
Supplier is in top-left block. PO number is labelled Purchase Order No.

EXTRACTION RULES:
  1. PO number: PO followed by 5 digits
  2. Amount = Qty x Unit Cost

DOCUMENT FORMAT:
This document is a single purchase order that spans multiple pages. Header fields appear on page 1; line items may continue across subsequent pages.

OUTPUT RULES:
  - Return ONLY valid JSON. No markdown fences, no explanation, no extra text.
  - Use null for missing fields, never omit them.
  - For line_items, return an array even if only one item exists.
  - Numbers should be numeric (not strings) when possible.
  - Dates should be in the format they appear in the document.
```

---

## Format 1: `single_po_multipage` (4-page PO)

### Page 1 -- User Message

```
You are processing page 1 of 4.
Extract all header fields AND line items from this page.

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

Return JSON matching this structure:
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
  ],
  "_page": 1,
  "_total_pages": 4
}
```

### Pages 2-4 -- User Message (continuation)

```
You are processing page 2 of 4 (continuation page -- header already extracted from page 1).
Extract ONLY the line_items table rows from this page. Do NOT repeat header fields.

Line item columns to extract (each row):
  - no
  - variant
  - description
  - qty
  - uom
  - unit_cost
  - amount

Return JSON matching this structure:
{
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
  ],
  "_page": 2,
  "_total_pages": 4
}
```

---

## Format 2: `po_per_page` (5 independent POs)

### Any page (e.g. page 3) -- User Message

```
You are processing page 3 of 5.
Extract all requested fields from this page.

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

Return JSON matching this structure:
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
  ],
  "_page": 3,
  "_total_pages": 5
}
```

---

## Format 3: `single_page`

### User Message

```
This is a single-page document.
Extract all requested fields from this page.

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

Return JSON matching this structure:
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
  ],
  "_page": 1,
  "_total_pages": 1
}
```
