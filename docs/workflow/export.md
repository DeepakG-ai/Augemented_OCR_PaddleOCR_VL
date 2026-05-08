# Export — Outbound Stage (CSV + Excel)

> Source files:
> - Contract builder: [backend/contracts.py](../../backend/contracts.py)
> - Excel/CSV builders: [backend/exporter.py](../../backend/exporter.py)
> - Outbound worker: [backend/worker.py](../../backend/worker.py) `_process_outbound` (lines 989–1076)
> - Download endpoints: `GET /extractions/{id}/export.xlsx` and `.csv` in [backend/main.py](../../backend/main.py)
> - Storage: [backend/object_store.py](../../backend/object_store.py) (MinIO + local fallback)

---

## Where outbound fits

The outbound stage runs:
1. **Automatically after postprocess** — every successful extraction triggers it via `ensure_job(... "outbound" ...)`.
2. **Again after review save** — the `PUT /extractions/{id}/corrections` endpoint enqueues outbound with `trigger: "review"` so corrected exports replace machine-generated ones.

```
postprocess complete
       │
       ▼
  ensure_job(outbound)
       │
       ▼
outbound worker:
  1. build contract from extraction row (uses corrected_result if present)
  2. build Excel bytes from contract
  3. build CSV bytes from contract
  4. put both to MinIO (EXPORTS_BUCKET)
  5. upsert delivery rows for both
       │
       ▼
User clicks "Export Excel" or "Export CSV"
       │
       ▼
GET /extractions/{id}/export.xlsx
  - assert_extraction_access (isolation check)
  - read MinIO object_key from extractions row
  - stream the bytes back with attachment filename
```

---

## The contract (`contracts.py:build_purchase_order_contract`)

Before any export is built, the extraction row is normalized into a stable shape — the **purchase_order.v1 contract**. This decouples export logic from the underlying schema; if you change DB fields, you just update the contract builder.

```python
def build_purchase_order_contract(extraction: dict) -> dict[str, Any]:
    effective = extraction.get("corrected_result") or extraction.get("result") or {}
    review_meta = extraction.get("correction_meta") or {}
```

**`corrected_result || result`**: always prefer the human-corrected version. If a user reviewed the extraction and saved corrections, the export uses those values. Otherwise the machine output is used as-is.

### Two shapes

**Single PO** (`format_type` is `single_po_multipage` or `single_page`):
```python
primary = effective                      # dict
documents = [_document_payload(primary, 1)]
header = _header_fields(primary)         # everything except line_items
line_items = primary.get("line_items")
```

**Multi-PO** (`format_type` is `po_per_page`):
```python
documents = [_document_payload(result, idx+1) for idx, result in enumerate(effective)]
header = {"document_count": len(documents)}
line_items = _multi_document_export_rows(documents)   # flatten all docs
```

For multi-PO, the line_items list is flattened across all documents — each row carries its `document_index` and its document's header fields. This makes a single-table CSV/Excel export trivial.

### The output structure

```python
{
    "contract_version": "purchase_order.v1",
    "document_type": "purchase_order",
    "extraction_id": 123,
    "vendor_id": "ACME",
    "vendor_name": "ACME Industries",
    "filename": "po_invoice.pdf",
    "source": {
        "format_type": "single_po_multipage",
        "total_pages": 3,
        "status": "done",
    },
    "document_count": 1,
    "documents": [
        {"document_index": 1, "header": {...}, "line_items": [...]},
    ],
    "header": {...},                    # flat header dict for single PO; doc_count for multi
    "line_items": [...],                # flat rows for export
    "review": {
        "canonical_source": "human",    # 'human' if corrected, else 'machine'
        "fields_changed": ["po_number"],
        "reviewed_at": "2026-05-07T10:23Z",
        "reason_code": "manual_review",
    },
}
```

The `review` block is what tells downstream systems "this came from a human review" vs "this is raw model output" — useful for analytics dashboards.

---

## The outbound worker (`worker.py:_process_outbound` lines 989–1076)

```python
async def _process_outbound(pool, job):
    extraction_id = job["extraction_id"]
    extraction_row = await db_mod.get_extraction(pool, extraction_id)
    if await _stop_if_cancelled(pool, extraction_id, "outbound", "..."):
        return
    
    # 1. Build the contract
    contract = build_purchase_order_contract(extraction_row)
    
    store = get_store()
    
    # 2. Excel
    excel_bytes = build_excel_bytes(contract)
    xlsx_key = f"exports/extractions/{extraction_id}/purchase_order.xlsx"
    store.put_bytes(EXPORTS_BUCKET, xlsx_key, excel_bytes,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    
    # 3. CSV
    csv_bytes = build_csv_bytes(contract)
    csv_key = f"exports/extractions/{extraction_id}/purchase_order.csv"
    store.put_bytes(EXPORTS_BUCKET, csv_key, csv_bytes, "text/csv")
    
    # 4. Record on the extraction + delivery table
    await db_mod.save_export_artifact(pool, extraction_id, xlsx_key)   # extractions.export_object_key
    await db_mod.upsert_delivery(pool, ext_id, contract_type="purchase_order.v1",
                                 target_type="excel", status="delivered",
                                 payload=contract, object_key=xlsx_key)
    await db_mod.upsert_delivery(pool, ext_id, contract_type="purchase_order.v1",
                                 target_type="csv", status="delivered",
                                 payload=contract, object_key=csv_key)
```

### Object keys

```
exports/extractions/{extraction_id}/purchase_order.xlsx
exports/extractions/{extraction_id}/purchase_order.csv
```

Stable per-extraction. Re-running outbound (e.g. after corrections) overwrites the previous file at the same key. No versioning — the most recent export is the only one available. If versioning is needed, change the key to include a timestamp.

### Delivery rows

`integration_deliveries` has a `UNIQUE (extraction_id, contract_type, target_type)` index. `upsert_delivery` uses `ON CONFLICT DO UPDATE` to keep one row per (extraction, contract, target). Re-export updates `status`, `payload`, `object_key`, `error`, leaving the row history clean.

### Failure handling

```python
except Exception as exc:
    if job.get("extraction_id") and stage == "outbound":
        await db_mod.upsert_delivery(
            pool, ext_id, contract_type="purchase_order.v1",
            target_type="excel", status="failed", error=str(exc),
        )
    await db_mod.fail_job(pool, job["id"], str(exc), retryable=False)
```

**Outbound failure does NOT mark the extraction as failed.** The user-facing result is still good — only the export delivery is broken. The delivery row records the error; the user can retry by saving review again (which re-enqueues outbound).

---

## Excel build (`exporter.build_excel_bytes`)

Uses `openpyxl`. High-level shape:

- **Sheet 1: Header** — single block of `{key: value}` pairs.
- **Sheet 2: Line Items** — table with all line item columns, one row per item.
- For multi-PO format: line items table has a `document_index` column to distinguish which PO each row belongs to.
- Header values that span multiple lines (e.g. multi-line addresses) are kept as `\n`-separated strings; openpyxl wraps them automatically with `wrapText=True`.

The export is intentionally **flat** — no nested cells, no merged cells beyond the simplest cases. Downstream tools (ERP imports, BI tools) prefer flat tables.

---

## CSV build (`exporter.build_csv_bytes`)

Uses Python's standard `csv` module. Output shape:

- One header row with all columns: `[doc_index] + header_keys + line_item_columns`.
- One data row per line item.
- For documents with no line items, one row with empty line-item cells.
- All values quoted; double-quote escaping (`""`).
- UTF-8 with BOM (Excel-friendly).

This shape (header keys repeat on every line item row) is the result of `_multi_document_export_rows` — a deliberate denormalization for spreadsheet consumption. Tools that need normalized data should consume the JSON contract directly.

---

## Object store abstraction (`object_store.py`)

```python
def get_store() -> ObjectStore:
    if _minio_configured():
        return MinIOStore(...)
    return LocalFsStore(".local_object_store")
```

Two implementations:

- **MinIOStore**: production. Uses `minio` client, talks to whatever S3-compatible endpoint `MINIO_ENDPOINT` points at.
- **LocalFsStore**: dev/test fallback. Writes files under `./.local_object_store/{bucket}/{key}`. Identical interface, no MinIO required.

Three buckets:
- `documents` — raw uploaded PDFs/images.
- `artifacts` — rendered page images (per-extraction).
- `exports` — generated CSV/Excel.

Workers receive `store = get_store()` once per job; the same instance handles `put_bytes` and `get_bytes` calls.

---

## Download endpoints

```python
@app.get("/extractions/{extraction_id}/export.xlsx")
async def download_excel(extraction_id: int, request: Request,
                         user: dict = Depends(get_current_user)):
    pool = request.app.state.pool
    await assert_extraction_access(pool, extraction_id, user)
    
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction or not extraction.get("export_object_key"):
        raise HTTPException(404, "Export not ready")
    
    store = get_store()
    data = store.get_bytes(EXPORTS_BUCKET, extraction["export_object_key"])
    
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-...",
        headers={"Content-Disposition": f'attachment; filename="...xlsx"'},
    )
```

(Simplified — actual code derives a friendly filename from the document filename, e.g. `po_invoice_extraction_123.xlsx`.)

The CSV endpoint is identical except for the media type and filename pattern. Both:
1. **Authorize** via `assert_extraction_access`.
2. **404 if not ready** — outbound hasn't run yet, or it failed.
3. **Stream the MinIO bytes** as a download attachment.

The frontend's `_downloadAuthed` helper (in `extract.js` lines 850–865) handles the JS side: it uses `apiFetch` (so the Bearer token is attached), reads the Content-Disposition header for the filename, creates a Blob, and triggers a click on a hidden `<a>` element.

---

## End-to-end timing (for a typical 3-page PO)

| Stage | Time |
|---|---|
| normalize (PDF render + classify) | 1–2 s |
| OCR (if needed) | 2–4 s |
| LLM (Qwen3-VL, 3 pages, sequential) | 8–15 s |
| postprocess (qwen_layout_apply + spatial_memory) | <0.5 s |
| outbound (Excel + CSV + MinIO put) | 0.5–1 s |
| **Total** | **12–22 s** |

The user sees results when postprocess marks the extraction `done`. Outbound runs in parallel with the user reviewing — by the time they click "Export Excel", it's almost certainly already in MinIO.

---

## Common pitfalls

1. **`404 Export not ready`**: outbound hasn't completed (or failed). Check the `jobs` table for the outbound row's status, and `integration_deliveries.error` for the failure message.
2. **Outdated export after correction**: the user saved review but the worker hasn't picked up the new outbound job yet. Wait a few seconds or refresh.
3. **MinIO down**: outbound fails; `LocalFsStore` fallback only triggers at startup, not at runtime. If MinIO becomes unavailable mid-flight, restart the API container to pick up the local fallback.
4. **Special characters in filenames**: the Content-Disposition header doesn't currently encode non-ASCII filenames. If a vendor name has accented characters, the download filename strips them. Fix would be to use RFC 5987 `filename*=` syntax.
5. **Unicode in CSV**: BOM-prefixed UTF-8 should open correctly in Excel. Some older Excel versions still misread it — open via "Data → From Text" if needed.

---

## What this design does NOT do

- **No multi-format generation.** Just CSV and Excel. Adding XML/JSON/EDI would mean adding a new builder + payload upsert call.
- **No async download / signed URLs.** Every download is a server-streamed blob. For very large exports, a pre-signed MinIO URL would be more efficient.
- **No diff between machine and human export.** A "show me what would the export look like before vs after my corrections" feature is not built. The contract block has `review.canonical_source` indicating which is in use.
- **No automatic email or webhook delivery.** Exports live in MinIO; users download manually. To push exports to an external system, a new outbound `target_type` and a corresponding worker step would be needed.
- **No retention policy.** Exports stay in MinIO forever (unless the bucket has a lifecycle rule). For compliance retention, configure MinIO bucket policies directly.
