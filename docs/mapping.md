# ERP Field Mapper — How It Works

Plain-English explanation of the complete mapping flow: from raw Qwen output
to the canonical JSON that gets sent to client systems.

---

## The Problem It Solves

Every vendor's invoice or purchase order uses different column and field
names. Qwen3-VL reads the PDF and returns whatever names actually appear in
that document:

- Canada Metal calls it `"po_number"` — Robert Scott calls it `"order_no"`
- One vendor writes `"list_cost"` — another writes `"unit_price"` or `"ext_price"`
- Header labels like `"vendor"`, `"supplier"`, `"sold_by"` all mean the same thing

Client ERP or AP-automation systems downstream don't care about those
differences. They expect a fixed, predictable set of field names every time.

The ERP Field Mapper bridges that gap: **map once per vendor, apply forever**.

---

## The Two Sides

```
┌─────────────────────┐          ┌──────────────────────────┐
│   AI / SOURCE side  │          │   AP Automation Schema   │
│  (what Qwen sees)   │  ──────► │  (what ERP systems need) │
│                     │          │                          │
│  po_number          │          │  po_number               │
│  order_no           │          │  vendor_name             │
│  supplier           │          │  vendor_address          │
│  list_cost          │          │  invoice_number          │
│  ext_price          │          │  invoice_date            │
│  qty_ordered        │          │  invoice_total           │
│  ...                │          │  invoice_subtotal        │
└─────────────────────┘          │  tax_amount              │
                                 │  freight_amount          │
                                 │  terms                   │
                                 │                          │
                                 │  items[]:                │
                                 │    item                  │
                                 │    line_description      │
                                 │    quantity_ordered      │
                                 │    quantity_received     │
                                 │    unit_price            │
                                 │    line_total            │
                                 │    uom                   │
                                 └──────────────────────────┘
```

The **17 canonical fields** on the right never change. The fields on the
left are whatever the vendor's document happens to use.

---

## Step-by-Step Flow

### Step 1 — Qwen3-VL reads the PDF

Qwen3-VL processes each page and returns a JSON object with the field names
it found in the document. These names come from the template the admin
configured for this vendor.

**Example: Canada Metal invoice**

```json
{
  "po_number":       "DP0001005",
  "vendor_name":     "SLACAN / DIV OF TRIDE...",
  "vendor_address":  "145 ROY BLVD, BRANTFO...",
  "invoice_date":    "May 14, 2026",
  "invoice_total":   53123.92,
  "invoice_subtotal": 32779.36,
  "tax_amount":      2294.56,
  "frieght_amount":  18050.00,
  "invoice_number":  "INV-CM1005",
  "line_items": [
    {
      "item":        "GA...",
      "uom":         "EA",
      "unit_price":  354.16,
      "quantity_ordered": 3500,
      "quantity_received": 4000,
      "line_total":  "EXTENDED...",
      "_page":       1
    }
  ]
}
```

**Example: Robert Scott invoice (same information, different field names)**

```json
{
  "order_no":        "H1583-6900",
  "supplier":        "ROBERT SCOTT & SONS",
  "bill_to":         "123 MAIN ST, TORONTO",
  "doc_date":        "2026-05-10",
  "total_due":       12450.00,
  "subtotal":        11000.00,
  "gst":             550.00,
  "freight":         900.00,
  "inv_ref":         "RS-90012",
  "line_items": [
    {
      "part_number": "GHO-5035",
      "description": "PREMIUM MOP HEAD",
      "qty":         6,
      "list_cost":   70.00,
      "ext_price":   420.00,
      "unit":        "CS"
    }
  ]
}
```

Both documents contain the same business data. The field names are
completely different.

---

### Step 2 — Admin configures the mapping (once)

The admin opens the **Mapper** page in the UI, selects the vendor, and
drags source field names onto the canonical targets.

For Robert Scott, the admin would connect:

| Source field (Qwen output) | → | Canonical target (AP Automation Schema) |
|----------------------------|---|-----------------------------------------|
| `order_no`                 | → | `po_number`                             |
| `supplier`                 | → | `vendor_name`                           |
| `bill_to`                  | → | `vendor_address`                        |
| `doc_date`                 | → | `invoice_date`                          |
| `total_due`                | → | `invoice_total`                         |
| `subtotal`                 | → | `invoice_subtotal`                      |
| `gst`                      | → | `tax_amount`                            |
| `freight`                  | → | `freight_amount`                        |
| `inv_ref`                  | → | `invoice_number`                        |
| `part_number`              | → | `item`                                  |
| `description`              | → | `line_description`                      |
| `qty`                      | → | `quantity_ordered`                      |
| `list_cost`                | → | `unit_price`                            |
| `ext_price`                | → | `line_total`                            |
| `unit`                     | → | `uom`                                   |

The admin clicks **Save Mapping**. This is stored in the database as two
plain JSON objects:

```json
{
  "header_map": {
    "order_no":  "po_number",
    "supplier":  "vendor_name",
    "bill_to":   "vendor_address",
    "doc_date":  "invoice_date",
    "total_due": "invoice_total",
    "subtotal":  "invoice_subtotal",
    "gst":       "tax_amount",
    "freight":   "freight_amount",
    "inv_ref":   "invoice_number"
  },
  "line_map": {
    "part_number": "item",
    "description": "line_description",
    "qty":         "quantity_ordered",
    "list_cost":   "unit_price",
    "ext_price":   "line_total",
    "unit":        "uom"
  }
}
```

This mapping is stored once per vendor. It never needs to be done again
unless the template fields are renamed.

---

### Step 3 — Postprocess worker applies the mapping automatically

After every extraction finishes (normalize → OCR → LLM → postprocess), the
postprocess worker checks: does this vendor have a saved mapping?

If **yes** → apply the mapping, save the result in the `mapped_result`
column of the `extractions` table alongside the original raw result.

If **no** → leave `mapped_result` empty. The raw result is used as-is.

**What "apply the mapping" does, in plain English:**

1. Start with a blank output that has every canonical field set to `null`.
2. For each entry in `header_map`: read the source field's value from the
   Qwen result, write it into the matching canonical field in the output.
3. For every line item in `line_items[]`: same process using `line_map`.
   The output key is renamed from `line_items` to `items`.
4. Any source field that has no mapping entry is dropped entirely from
   the output — it never appears in what the ERP system sees.
5. Any canonical target field that has no source mapped to it stays
   `null` — the field is present but empty, so the ERP schema is always
   the same shape.

**Robert Scott example — before mapping:**

```json
{
  "order_no":  "H1583-6900",
  "supplier":  "ROBERT SCOTT & SONS",
  "bill_to":   "123 MAIN ST, TORONTO",
  "doc_date":  "2026-05-10",
  "total_due": 12450.00,
  "subtotal":  11000.00,
  "gst":       550.00,
  "freight":   900.00,
  "inv_ref":   "RS-90012",
  "terms":     null,
  "line_items": [
    {
      "part_number": "GHO-5035",
      "description": "PREMIUM MOP HEAD",
      "qty":         6,
      "list_cost":   70.00,
      "ext_price":   420.00,
      "unit":        "CS"
    }
  ]
}
```

**Robert Scott example — after mapping:**

```json
{
  "vendor_name":      "ROBERT SCOTT & SONS",
  "vendor_address":   "123 MAIN ST, TORONTO",
  "invoice_number":   "RS-90012",
  "invoice_date":     "2026-05-10",
  "po_number":        "H1583-6900",
  "invoice_total":    12450.00,
  "invoice_subtotal": 11000.00,
  "tax_amount":       550.00,
  "freight_amount":   900.00,
  "terms":            null,
  "items": [
    {
      "item":               "GHO-5035",
      "line_description":   "PREMIUM MOP HEAD",
      "quantity_ordered":   6,
      "quantity_received":  null,
      "unit_price":         70.00,
      "line_total":         420.00,
      "uom":                "CS"
    }
  ]
}
```

Notice:
- All 10 header targets are present (some `null` if not mapped)
- All 7 line item targets are present (`quantity_received` is `null`
  because the source doc had no such field)
- Source-only fields like `subtotal`, `gst`, `qty` are gone
- The key `line_items` became `items`

---

### Step 4 — Client receives canonical JSON via the API

When an ERP system or downstream client calls `POST /v1/extract` with their
API key and uploads a Robert Scott invoice, the response is:

```json
{
  "status": "ok",
  "extraction_id": 847,
  "pages": 2,
  "vendor_id": "robert-scott-1",
  "duration_ms": 4321,
  "mapping_applied": true,
  "extraction": {
    "vendor_name":      "ROBERT SCOTT & SONS",
    "vendor_address":   "123 MAIN ST, TORONTO",
    "invoice_number":   "RS-90012",
    "invoice_date":     "2026-05-10",
    "po_number":        "H1583-6900",
    "invoice_total":    12450.00,
    "invoice_subtotal": 11000.00,
    "tax_amount":       550.00,
    "freight_amount":   900.00,
    "terms":            null,
    "items": [ ... ]
  }
}
```

`mapping_applied: true` tells the client the canonical mapping was used.

If no mapping is configured, `mapping_applied` is `false` and `extraction`
contains the raw Qwen output with whatever field names the template uses.

---

## Two Document Shapes

Qwen can return results in two shapes depending on the vendor's template
format. The mapper handles both.

### Shape 1: `single_po_multipage` / `single_page`

One purchase order spanning multiple pages. Qwen merges everything into a
single dict. The mapper receives a dict and returns a dict.

```
Input:  { "order_no": "...", "line_items": [...] }
Output: { "po_number": "...", "items": [...] }
```

### Shape 2: `po_per_page`

One purchase order per page (e.g. a multi-PO batch invoice). Qwen returns
a list of dicts. The mapper receives a list and returns a list — each
element goes through the same mapping independently.

```
Input:  [ { "order_no": "A-1", ... }, { "order_no": "A-2", ... } ]
Output: [ { "po_number": "A-1", ... }, { "po_number": "A-2", ... } ]
```

---

## Rename Detection (Automatic)

If the admin edits a vendor's template and renames a field (example:
changes `po_number` to `order_number` in position 1 of the header fields
list), the system detects this automatically the moment the template is
saved.

**How it works:**

The system stores a snapshot of the field list at the time the mapping was
saved. On every template save it compares the new field list to the snapshot
**by position**:

```
Old snapshot: ["po_number", "vendor", "date", "total"]
New fields:   ["order_number", "vendor", "date", "total"]
                    ^
              position 0 changed — this is a rename
```

If positions match except for the name, each changed slot is treated as a
rename. If the list got longer or shorter, positions can't be aligned safely
— no renames are detected (manual re-mapping is required for add/remove).

**What happens automatically:**

1. The mapping key `po_number → po_number` is updated to
   `order_number → po_number` so the existing mapping keeps working.
2. A **notice** is stored: `"Header field renamed: po_number → order_number
   — mapping moved automatically"`.
3. The notice appears as a yellow banner in the Mapper UI the next time
   anyone opens that vendor.
4. The admin can dismiss it once they've confirmed the change is correct.

**Example:**

Before template edit, the mapping contained:

```json
{ "header_map": { "po_number": "po_number", "supplier": "vendor_name" } }
```

After the template renames `po_number` → `order_number`:

```json
{
  "header_map": { "order_number": "po_number", "supplier": "vendor_name" },
  "pending_notices": [
    {
      "section":  "header",
      "old":      "po_number",
      "new":      "order_number",
      "remapped": true,
      "at":       "2026-05-22T14:30:00Z"
    }
  ]
}
```

The next extraction for that vendor will use `order_number` correctly —
no manual fix needed.

---

## Where Things Are Stored

| What | Where |
|------|-------|
| The mapping rules | `field_mappings` table: `header_map` (JSONB), `line_map` (JSONB) |
| Original Qwen result | `extractions.result` (JSONB) — never overwritten |
| Mapped canonical result | `extractions.mapped_result` (JSONB) — separate column |
| Field name snapshot (for rename detection) | `field_mappings.header_snapshot`, `field_mappings.line_snapshot` |
| Pending rename notices | `field_mappings.pending_notices` (JSONB array) |

The raw Qwen result is always preserved. The mapped result is a
computed copy stored alongside it. This means:

- The Review page and History page always show the original Qwen result.
- The API client always gets the mapped version (if mapping is configured).
- You can delete and re-create a mapping without losing any extraction data.

---

## Admin vs Client Access

**Admin:**
- Sees all clients in the Mapper page CLIENT dropdown
- Selects a client first, then selects one of that client's vendors
- The "ACTING AS: client@email.com" badge is always visible so the context
  is never ambiguous
- Saves/edits mappings on behalf of any client's vendor

**Client user:**
- Sees only their own vendors
- No CLIENT selector shown
- Can configure and save mappings for their own vendors

---

## What Is Not Mapped

The following are intentionally excluded from the mapping:

- **Internal pipeline fields** like `_page`, `source`, `confidence` —
  these are Qwen metadata, not business data
- **Line-item list structure changes** — the mapper renames keys within
  each line item, it does not reorder, filter, or merge line items
- **Spatial memory corrections** — bounding-box positions used for field
  location memory are stored separately and are unaffected by the mapping
- **Gold examples** — the training/correction examples shown to Qwen use
  the original field names; the mapping is applied only to the final output

---

## Quick Reference

```
PDF uploaded
    │
    ▼
normalize-worker   → renders pages
    │
    ▼
ocr-worker         → PaddleOCR (scanned) or pypdfium2 (digital)
    │
    ▼
llm-worker         → Qwen3-VL reads each page, returns raw JSON
                     with vendor-specific field names
    │
    ▼
postprocess-worker → merges pages into one result,
                     checks field_mappings table:
                     ┌─ mapping found? ──► apply_mapping()
                     │                    rename fields to canonical targets
                     │                    save to extractions.mapped_result
                     └─ no mapping? ─────► leave mapped_result = null
    │
    ▼
POST /v1/extract response
    mapping_applied: true  → returns mapped_result (canonical schema)
    mapping_applied: false → returns raw result (original field names)
```
