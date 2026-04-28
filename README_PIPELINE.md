# Augmented OCR: Hybrid PDF Pipeline

This repository implements a state-of-the-art, vendor-aware document extraction pipeline. It utilizes a hybrid approach, combining deterministic PDF text extraction (`pypdfium2`) with Vision-Language Models (Qwen3-VL) and traditional Optical Character Recognition (PaddleOCR) to achieve high-accuracy data extraction from both digital and scanned documents.

---

## Architecture Workflow

The extraction process is divided into a fast synchronous ingest phase and a robust asynchronous processing pipeline.

### 1. UPLOAD Phase (Synchronous Pre-Processing)
*Before a job enters the queue, the system must fast-fail invalid documents and identify the vendor to determine the correct extraction schema.*

1. **Ingest Raw Bytes**: The API receives the raw PDF upload.
2. **First-Page Fast Render**: To prevent bottlenecks on 100+ page documents, the system extracts and renders *only* Page 1 to a temporary image in memory.
3. **Digital vs. Scanned Check (Page 1)**: 
   - `pypdfium2` attempts to extract the embedded text layer from Page 1.
   - If usable text is found, the page is deemed **digital**.
   - If no text (or very little text) is found, the page is deemed **scanned**, and the system runs a fast `PaddleOCR` pass on the Page 1 image.
4. **Vendor Detection**: The text extracted from Page 1 (whether digital or OCR'd) is normalized (lowercase, punctuation stripped). The `vendor_detector` scans this text against a database of known **Vendor Aliases**.
   - *Success*: A matching vendor is found. The document is saved, and a `normalize` job is enqueued.
   - *Failure*: If no match is found, the upload is rejected with an "Unknown Vendor" error, prompting the user to add an alias in the UI.

### 2. NORMALIZE Worker (Asynchronous)
*Prepares the document for machine learning models by standardizing the format and extracting all available digital text.*

1. **Rasterization**: The worker renders *all* pages of the PDF into high-quality PNG images and stores them securely in MinIO object storage. (Qwen-VL requires image inputs).
2. **Digital Text Extraction (Full Document)**: `pypdfium2` scans every page. It extracts the exact spatial coordinates (bounding boxes) for every digital word.
3. **Page Tagging**: Each page is individually evaluated and tagged in the database as either `digital` or `scanned` based on text density.

### 3. OCR Worker (Asynchronous)
*Fills in the gaps for pages that lack a digital text layer.*

1. **Selective Processing**: The worker queries the database for pages tagged strictly as `scanned`.
2. **Optical Character Recognition**: It runs `PaddleOCR` on the PNGs of the scanned pages to extract word-level bounding boxes.
3. **Efficiency**: Digital pages bypass this step entirely, saving massive amounts of GPU/CPU compute time. By the end of this step, the system has a unified spatial word map for every page, regardless of its original format.

### 4. LLM Worker (Qwen-VL)
*The core semantic extraction engine. It "reads" the document visually and semantically to extract the requested fields.*

1. **Prompt Assembly**: The worker loads the Vendor's Template (which defines `header_fields`, `line_item_fields`, and custom `extraction_rules`).
2. **Inference**: The page PNG and the system prompt are sent to Qwen3-VL.
3. **Spatial Output**: Qwen-VL is prompted to return not just the JSON data, but also the spatial bounding boxes (`<box>...</box>`) indicating *where* on the page it found the data (headers and line item columns).
4. **Coordinate Mapping**: The `qwen_bbox_parser` translates Qwen's normalized `[0-1000]` coordinate grid into the exact pixel-space coordinates required by the frontend UI.

### 5. POSTPROCESS & OUTBOUND
*Cleans and delivers the final payload.*

1. **Data Cleaning**: The raw JSON from Qwen-VL is sanitized (e.g., standardizing date formats, removing currency symbols from numeric columns) based on the template's extraction rules.
2. **Outbound**: The final payload is prepared for the Review UI and enqueued for delivery to external ERP/accounting systems via webhooks.

---

## The Human-in-the-Loop Review System

The Review UI (`app.js`) provides a visual overlay of the extracted data directly on top of the document images, allowing users to verify and correct the LLM's output.

### 1. Bounding Box Rendering
The backend passes a `field_locations` dictionary to the frontend. The UI uses these coordinates to draw interactive colored boxes over the exact words the LLM used to generate a field's value. 

### 2. Manual Correction Workflow
If the LLM makes a mistake, the user can click a field input, drag a rectangle over the correct text on the document image, and the system instantly reads the words inside that drawn rectangle to update the field value.

### 3. Spatial Memory (Self-Healing)
When a user manually corrects a field (e.g., dragging a box over the "Invoice Date"), that correction is not discarded. It is saved to the database as **Spatial Memory**.
- The next time an invoice with the exact same layout arrives from that vendor, the system bypasses the LLM for that specific field.
- It looks at the saved spatial coordinates, reads the *new* text currently at that location, and uses it as the first-pass answer. This ensures the system never repeats a mistake on a consistent layout.

### 4. Vendor Alias Management
When a vendor operates under a subsidiary or trade name (e.g., "Restaurant Depot" sending invoices labeled "JETRO CASH & CARRY"), the system will initially fail with an "Unknown Vendor" error.
- Users can navigate to the Vendor UI and add "JETRO CASH & CARRY" to the vendor's **Aliases** list.
- The system defaults to a weight of `w:1` for new aliases. 
- Future uploads matching this string will automatically route to the correct vendor schema without any code changes or manual database scripts.

---

## Qwen-VL System Prompt Architecture

The system prompt forces Qwen-VL into a strict JSON-output mode and demands spatial bounding boxes. Below is the exact structure of the prompt generated by the backend:

```text
You are a highly accurate document data extraction assistant.
This request is processed one page at a time.

Return exactly two top-level keys:
- `fields`: extracted values
- `boxes`: bounding boxes for requested header-field anchors and requested line-item column headers only

<document_format>
This document is a single purchase order that spans multiple pages. Header fields appear on page 1; line items may continue across subsequent pages.
</document_format>

<critical>
Count the number of rows in the line items table FIRST, then extract that exact number of items.
</critical>

<output_rules>
- Extract ONLY what is explicitly visible in the document image.
- Never guess or fabricate data.
- Return ONLY valid JSON. No markdown fences, no explanation, no extra text.
- Use null for missing fields, never omit them.
- For line_items, return an array even if only one item exists.
- Numbers should be numeric (not strings) when possible.
- Dates should be in the format they appear in the document.
- Return bounding boxes only for requested field anchors and requested table column headers.
- Do not return bounding boxes for field values.
- Do not return row-level line item bounding boxes.
- Every bounding box must be [x0, y0, x1, y1] in a 0-1000 normalized grid relative to the page image.
- The bbox should tightly cover the anchor/header text region only.
</output_rules>

user message 
Extract the header fields AND all visible line item rows from this purchase order page (page 1 of 1).

For each requested header field, also return the bounding box of the field LABEL (not the value) in the `boxes` section.
For each requested line-item column, return the bounding box of the TABLE HEADER text (not the cell values) in the `boxes` section.

If any field is empty or not visible, return null.
If an anchor/header label is not visible on this page, return null for that box.

<header_fields>
  - bill_to
  - ship_to
  - invoice_date
  - invoice_number
  - vendor_name
  - po_number
  - order_date
  - order_number
  - fob
  - terms
  - freight
  - invoice_total
</header_fields>

<line_item_columns>
  - item
  - required_qty
  - ship_qty
  - uom
  - unit_price
  - line_total
</line_item_columns>

Return JSON in exactly this shape:
{
  "fields": {
    "bill_to": null,
    "ship_to": null,
    "invoice_date": null,
    "invoice_number": null,
    "vendor_name": null,
    "po_number": null,
    "order_date": null,
    "order_number": null,
    "fob": null,
    "terms": null,
    "freight": null,
    "invoice_total": null,
    "line_items": [
      {
        "item": null,
        "required_qty": null,
        "ship_qty": null,
        "uom": null,
        "unit_price": null,
        "line_total": null
      }
    ]
  },
  "boxes": {
    "bill_to": null,
    "ship_to": null,
    "invoice_date": null,
    "invoice_number": null,
    "vendor_name": null,
    "po_number": null,
    "order_date": null,
    "order_number": null,
    "fob": null,
    "terms": null,
    "freight": null,
    "invoice_total": null,
    "item": null,
    "required_qty": null,
    "ship_qty": null,
    "uom": null,
    "unit_price": null,
    "line_total": null
  }
}

<rules>
- Empty or missing cells → null.
- Numbers (qty, unit_cost, amount, unit_price, etc.) must be numbers, not strings.
- Extract every visible line item row.
- boxes contain ONLY anchor/header label bounding boxes as [x0, y0, x1, y1].
- Do NOT return bounding boxes for field values or individual line-item cells.
</rules>

Return ONLY valid JSON matching EXACTLY the structure above.
```
