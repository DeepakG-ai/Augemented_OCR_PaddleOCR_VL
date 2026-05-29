# Gold Corrections

This document covers the design and operations of Gold Corrections, which allows the system to learn from manual edits by inject value-redacted correction examples into the LLM system prompt.

---

## What it is

Gold Corrections are human-reviewed correction history examples injected into the vision-LLM system prompt as few-shot hints. 

To prevent the LLM from hardcoding stale values (e.g. copying an old invoice number or old date onto a new document), the examples are strictly **value-redacted**. The prompt structure shows the original wrong value and the corrected output value, and explicitly instructs the model: *"Human review has corrected these field values. Do not copy either value. Instead look into the document and return the value which is exactly inside."* This teaches the model the spatial field boundaries and parsing rules without hardcoding old results.

---

## How it works

Gold corrections are created automatically during the manual review phase and are managed via vendor API routes.

```
          [Manual Review Saved]
                    │
                    ▼
     [Compare original vs corrected]
                    │
                    ▼
     [Filter out table line items]
 (Keep header-only diffs to reduce noise)
                    │
                    ▼
       [Insert to gold_examples]
                    │
                    ▼
      [Rebuild System Prompt (GET)]
  (Inject diffs as <correction_examples>)
```

### 1. Creation Path
When corrections are saved on the manual Review page via `PUT /extractions/{extraction_id}/corrections`:
1. The server compares the LLM's raw `result` with the user's `corrected_result` using `_compute_correction_diff()`.
2. **Line Item Filtering**: Any diffs on table line items (`line_items` or keys ending with `_line_items`) are stripped out. Table rows and line cells are highly dynamic and add prompt noise rather than signal. Only header fields are kept.
3. If header differences remain, the system saves the audit diff to the `gold_examples` table using `db_mod.save_gold_example()`.

### 2. Prompt Compilation Path
When a new document starts extraction, or when an admin previews the prompts via `GET /vendors/{id}/template`:
1. The system loads active gold corrections for the vendor from the database.
2. It calls `_gold_correction_examples` to map the raw database diffs into a simplified JSON representation containing `original_value` and `correct_diff` for each field:
   ```json
   {
     "supplier": {
       "original_value": "Wrong Co",
       "correct_diff": "ACME Corp"
     }
   }
   ```
3. The mapped JSON is wrapped inside `<correction_examples>` tags alongside instructions telling the model not to copy either string but to use them as examples.
4. The system prompt is compiled, and its SHA-256 hash is computed to check against cached prompts.

---

## Rules & Hard Constraints

- **Do Not Copy Rule**: The system prompt must explicitly instruct the LLM not to copy the `original_value` or the `correct_diff` but to look at the new document image.
- **Header Fields Only**: Diffs on line-item tables are strictly excluded. 
- **Purge Controls**: Gold corrections can be deleted by admins per field key via `DELETE /vendors/{vendor_id}/gold-corrections/{field_key}`. This deletes matching keys in historical `correction_diff` JSON objects and deletes the entire `gold_examples` row if the diff object becomes empty.
- **Cascade Deletion**: If a vendor is deleted, all corresponding `gold_examples` rows must be deleted in the same transaction to maintain database integrity.
- **po_per_page Key Prefix Limitation**: For `po_per_page` split extractions, manual review corrections are stored with prefixed keys (e.g., `doc_0_po_number`). Because these do not match the base template keys (e.g. `po_number`), they are currently ignored by the Vision-LLM prompt compiler and do not function as active few-shot prompt corrections. This is a known current limitation of the prompt-hints system.

---

## All Scenarios in Plain English

### Scenario 1 — Gold correction created
- Qwen extracts `"Wrong Co"` as the supplier for vendor `V1`.
- A user reviews the document and corrects the supplier to `"ACME Corp"`.
- The system compares the results and creates a diff: `{"supplier": {"original": "Wrong Co", "corrected": "ACME Corp"}}`.
- It inserts a new row into `gold_examples`.
- The next time a document for vendor `V1` is uploaded, this diff is injected as a few-shot example in the system prompt.

### Scenario 2 — Model learns from correction without copying
- A new invoice from vendor `V1` is uploaded. The actual supplier on the image is `"ACME Corp"`.
- The system prompt contains the gold example showing `supplier` original `"Wrong Co"` vs corrected `"ACME Corp"`.
- The model reads the new document. It sees `"ACME Corp"` on the image. 
- Guided by the example and prompt instructions, it correctly extracts `"ACME Corp"` without hallucinating or falling back to `"Wrong Co"`.

### Scenario 3 — Deleting a gold correction field
- A user changed the invoice layout, and an old gold correction for `supplier` is causing extraction issues.
- An admin calls `DELETE /vendors/V1/gold-corrections/supplier`.
- The system updates all `gold_examples` rows for `V1`, removing the `supplier` key from `correction_diff` JSON.
- For rows where `correction_diff` becomes empty, the system hard-deletes the row.

---

## Error Responses

| Situation | HTTP Code | Error Message |
|---|---|---|
| Delete correction on unowned vendor | 403 | `"Access denied"` |

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_extractor_no_boxes.py`](../../tests/test_extractor_no_boxes.py) | `test_prompt_with_gold_examples_no_boxes_and_includes_correction_diff` | Verifies that gold corrections are correctly mapped and injected into the compiled system prompt. |
| [`test_spatial_memory_management.py`](../../tests/test_spatial_memory_management.py) | `test_delete_gold_correction_field_removes_all_versions` | Verifies that deleting a field correction prunes keys from diffs and deletes empty rows. |
| [`test_db_vendor_filtering.py`](../../tests/test_db_vendor_filtering.py) | `test_delete_vendor_removes_gold_examples_before_vendor_row` | Verifies database integrity: deleting a vendor cascadingly deletes gold examples first. |

---

## Quick Reference

| Operation / Path | Target Table | Fields / Outputs | Notes |
|---|---|---|---|
| Save gold correction | `gold_examples` | `original_result`, `corrected_result`, `correction_diff` | Created inside corrections API |
| Load gold corrections | `gold_examples` | `db_mod.get_gold_examples` | Injected into system prompt |
| Delete field correction | `gold_examples` | `db_mod.delete_gold_correction_field` | Prunes keys and empty rows |
