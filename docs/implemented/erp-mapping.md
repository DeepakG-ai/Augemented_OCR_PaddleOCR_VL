# ERP Field Mapping

This document details the configuration and operations of ERP Field Mapping, which translates vendor-specific key names into canonical schemas required by downstream ERP systems.

---

## What it is

The vision model extracts fields using the names defined in the vendor's template (e.g. `PO_no`). However, downstream client ERP systems expect standardized canonical key names (e.g. `po_number`). 

ERP Field Mapping translates vendor-specific keys to canonical schema targets. 
- The original extraction result `result` is left **untouched** so that human reviewers see the fields as they appear on the original invoice.
- A translated copy is written to the `mapped_result` column on the `extractions` table and returned to programmatic API integration clients.

---

## How it works

Field mapping is configured per-vendor, and executed automatically during the postprocessing stage of the extraction pipeline.

```
       [Postprocess Worker Starts]
                    │
                    ▼
          [Load raw result JSON]
                    │
                    ▼
     [Read Vendor Schema Mapping]
 (Target lists: headers & line items)
                    │
                    ▼
         [Run apply_mapping()]
                    │
                    ├─── (Schema provided?) ─── Yes ───► [Use custom schema keys]
                    │                                             │
                    └─── No ───────────────────────────► [Use default AP targets]
                                                                  │
                                                                  ▼
                                                      [Initialize Output Skeleton]
                                                     (Keys set to null initially)
                                                                  │
                                                                  ▼
                                                       [Copy Mapped Fields]
                                                     (Drop unmapped source keys)
                                                                  │
                                                                  ▼
                                                       [Save to mapped_result]
```

### 1. Mapping Configuration
Admins configure mappings via `/vendors/{id}/mapping` routes. Mappings are stored in the `field_mappings` table:
- `header_map`: A JSONB dictionary matching `{source_field: target_canonical_field}`.
- `line_map`: A JSONB dictionary matching `{source_column: target_canonical_column}`.

### 2. Execution Loop
During postprocessing, the worker calls `field_mapper.apply_mapping(result, mapping, schema)`:
1. **Target Identification**:
   - If a target `schema` object is provided (e.g. a specific ERP output schema like "SyteLine PO Automation"), the mapper loads the schema's custom `header_fields` and `line_fields` lists.
   - If no schema is provided, the mapper falls back to default module constants `HEADER_TARGETS` (e.g. `vendor_name`, `invoice_number`, `invoice_total`, etc.) and `LINE_TARGETS` (e.g. `item`, `unit_price`, etc.).
2. **Output Construction**:
   - An empty document dictionary is initialized with every target field key set to `None`.
   - The mapper loops through the `header_map`. If the target key exists in the skeleton, the value from the source key in `result` is copied.
   - Any source fields that have no mapping configured are **dropped**, ensuring only clean schema fields reach the target.
   - The same matching loop is run for every entry in the `line_items` array.
3. **Persist Mapped Copy**:
   - The translated JSON is written to `mapped_result` in `extractions`.

### 3. Positional Rename Propagation
If a user edits a template via `/vendors/{id}/template` and renames a field, the system propagates the map to prevent breaking the ERP schema:
- The system runs `detect_renames(old_fields, new_fields)`. If the number of fields is the same, any slot that changed names is treated as a positional rename (e.g., field at index 1 changed from `po_no` to `po_number`).
- The system calls `apply_renames(field_map, renames)` to copy the mapped target from the old key to the new key, ensuring the mapping configuration survives field renaming.

---

## Rules & Hard Constraints

- **Skeleton Completeness**: The mapped JSON must contain every canonical field defined in the target schema. Unmapped targets must be set to `null` rather than omitted, ensuring predictable JSON shapes for downstream parsers.
- **Unmapped Pruning**: Any extracted field in the source document that is not mapped to a target must be omitted from the output.
- **Result Isolation**: The primary `result` or `corrected_result` column is never modified by the field mapper.
- **Rename Boundary**: Positional rename detection is skipped if the length of the field list changes. A length change means fields were added or removed, so matching slots safely by index is impossible.
- **Silent Rename Exceptions (M2)**: Any errors during rename matching or mapping upserts in the template saving process are caught and logged as warnings rather than raising HTTP errors, which can cause the active mapping config to drift silently from the template.

---

## All Scenarios in Plain English

### Scenario 1 — Standard AP Invoice mapping
- A document has raw extraction: `{"supplier": "ACME", "po_no": "9901", "other_field": "val"}`.
- Mapping config: `header_map = {"supplier": "vendor_name", "po_no": "po_number"}`.
- Default AP targets are loaded. `other_field` has no mapping.
- Output skeleton initialized with all default targets (`vendor_name`, `invoice_number`, `invoice_total`, etc.) set to `null`.
- Supplier is copied to `vendor_name`, PO is copied to `po_number`. `other_field` is omitted.
- The resulting JSON is stored in `mapped_result`.

### Scenario 2 — Custom schema mapping
- A vendor is assigned a custom ERP schema expecting `["CustNum", "Client", "OrderDate"]`.
- The document has raw extraction: `{"buyer_code": 105, "buyer_name": "ACME"}`.
- Mapping config: `header_map = {"buyer_code": "CustNum", "buyer_name": "Client"}`.
- Output is generated containing only custom schema fields: `{"CustNum": 105, "Client": "ACME", "OrderDate": null}`. None of the default AP fields are included.

### Scenario 3 — Positional rename propagation
- A vendor's template header fields are `["vendor", "po_no", "invoice_date"]`.
- The user has mapped `po_no` to target `po_number`.
- The user edits the template fields to `["vendor", "po_number", "invoice_date"]`.
- The system detects the rename of `po_no` to `po_number`.
- The vendor's mapping is automatically updated, replacing `po_no` with `po_number` in `header_map`. The mapping is preserved.

---

## Error Responses

Because ERP mapping runs inside the postprocess pipeline worker, any database exceptions or schema validation failures are logged. The worker catches exceptions, logs them, and falls back to saving the unmapped raw `result` to prevent the job from hanging.

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_field_mapper.py`](../../tests/test_field_mapper.py) | `test_dict_result_renames_header_and_line_fields` | Verifies key renaming, skeleton initialization (unmapped targets -> null), and pruning (source-only fields dropped). |
| | `test_list_result_maps_each_document` | Verifies mapping support for lists (`po_per_page` splits). |
| | `test_empty_mapping_yields_canonical_skeleton` | Verifies that an empty mapping still returns a full target skeleton filled with null values. |
| | `test_custom_schema_replaces_default_header_targets` | Verifies custom schemas replace default AP targets in the output. |
| | `test_single_position_rename_detected` | Verifies rename detection aligns matching positions. |
| | `test_apply_renames_carries_mapping_forward` | Verifies renamed keys are mapped to their targets in mapping configuration. |

---

## Quick Reference

| Source Field / Config | Table / Target | Output Column | Notes |
|---|---|---|---|
| Map configuration | `field_mappings` | `header_map`, `line_map` | Configured per vendor |
| Custom target schema | `output_schemas` | `header_fields`, `line_fields` | Overrides default constants |
| Default headers list | `HEADER_TARGETS` | n/a | Fallback target list |
| Mapped result storage | `extractions` | `mapped_result` | JSONB formatted copy |
