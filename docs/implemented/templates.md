# Templates

This document details how document extraction templates are configured, managed, compiled into vision-LLM prompts, and restricted for tenant-level security.

---

## What it is

A template is a schema definition configured per-vendor. It defines:
- The **format type** (e.g. `single_po_multipage`, `po_per_page`, `single_page`).
- The **header fields** to extract (e.g. `invoice_date`, `po_no`).
- The **line item table columns** to extract (e.g. `qty`, `unit_price`).
- **Prompt instructions** and **extraction rules** tailored to the vendor's invoice or PO layout.

The vision-LLM system prompt is compiled fresh from the database whenever a document is processed or saved, integrating the template rules with human-review examples.

---

## How it works

Templates are managed via `/vendors/{vendor_id}/template` API endpoints.

```
                  [POST /vendors/{vendor_id}/template]
                                    │
                                    ▼
                      [Upsert Vendor (Normalized)]
                                    │
                                    ▼
                       [Build System Prompt & Hash]
                      (Using gold_examples context)
                                    │
                                    ▼
                       [Upsert to templates Table]
                                    │
                                    ▼
                     [Delete Stale Fields Geometry]
                     (Clean qwen_layout_boxes &
                      spatial_memory for removed keys)
                                    │
                                    ▼
                     [Rename ERP Mapped Fields Keys]
                     (Propagate position-based renames)
```

### Save Template Flow
1. **Vendor Upsert**: When saving a template for `vendor_id`, if the vendor row does not exist in `vendors`:
   - It is auto-created. If a client user is calling, they are assigned ownership (`user_id = user.id`). If an admin calls, they must create the vendor explicitly via `/vendors` first (otherwise returns `400`).
2. **Prompt Compilation**:
   - The system retrieves any human review corrections (`gold_examples`) saved for this vendor.
   - It runs `extractor.build_system_prompt()` to assemble the instructions, extraction rules, and value-redacted gold examples into a raw text string.
   - A `prompt_hash` is computed from the fields and rules.
3. **Database Upsert**: The schema fields, format type, instructions, rules, generated prompt, and hash are inserted/updated in the `templates` table.
4. **Stale Geometry Cleanup**:
   - If fields were deleted from the template compared to the previous version, any saved bounding boxes in `qwen_layout_boxes` and user-drawn coordinates in `spatial_memory` referencing those deleted field keys are hard-deleted. This prevents stale layout coordinates from popping up in the UI.
5. **ERP Rename Propagation**:
   - The system matches position-based renames. For example, if the user renamed header field 2 from `po_no` to `po_number` without changing the total count, the system automatically translates the downstream ERP mapping (`field_mappings` table) keys to `po_number` so the client does not lose their configured ERP output structure.

### Retrieve Template & Security Visibility Rule
When retrieving a template via `GET /vendors/{vendor_id}/template`:
- **For Admins (`role = "admin"`)**: The response includes a fully rendered preview of the compiled Page 1 and Page 2 system/user prompts (`system_prompt_page1`, `user_prompt_page1`, etc.) containing the gold examples. This allows admins to inspect what the vision model receives.
- **For Clients (`role = "client"`)**: The system **redacts** the prompt fields (`system_prompt` and `user_prompt` keys are set to `null`). The client receives only the raw config (fields, rules, instructions). This prevents leaking raw system prompts or other training text to the browser.

---

## Rules & Hard Constraints

- **One Template Per Vendor**: Enforced by the `UNIQUE(vendor_id)` index in the database.
- **Validation Constraint**: A template must contain at least one header field or line item column. Empty schemas are rejected with HTTP 400.
- **Dynamic Prompt Hash**: The `prompt_hash` is a SHA-256 of the template config. If it matches during extraction, the pipeline loads the cached prompt instead of rebuilding it (speeding up processing).
- **Prompt Redaction**: System prompt fields must never be exposed to non-admin roles in `GET` or `POST` API responses.
- **Pruning Boundary**: Stale cleanup prunes *both* `qwen_layout_boxes` and `spatial_memory` tables immediately on template updates.
- **Silent Rename Exceptions (M2)**: If an exception occurs during the `detect_renames` or `upsert_field_mapping` steps, the error is swallowed and logged as a warning (`logger.warning`), while the endpoint still returns HTTP 200. This prevents template save failures but can result in silent mapping configuration drift.

---

## All Scenarios in Plain English

### Scenario 1 — Client creates a new template and vendor
- A logged-in client user POSTs a template to `/vendors/V100/template`.
- Vendor `V100` does not exist in the database.
- The server creates vendor `V100` and assigns its `user_id` to the calling client.
- The server compiles the system prompt and inserts the template.
- The response returns status success. The generated prompt fields in the response are empty (`null`).

### Scenario 2 — Admin saves a template
- An admin POSTs a template to `/vendors/V200/template`.
- If vendor `V200` does not exist, the request fails with HTTP 400 (admin must create it via `/vendors` first).
- If it exists, the template is saved.
- The response includes the compiled `system_prompt_preview` so the admin can review the prompt structure immediately.

### Scenario 3 — User updates a template, removing a field
- A vendor template contains header fields `["supplier", "invoice_date", "terms"]`.
- The user updates it to `["supplier", "invoice_date"]` (removing `terms`).
- The database upserts the template.
- The cleanup routine runs and deletes any rows in `qwen_layout_boxes` and `spatial_memory` where `vendor_id = '...'` and `field_key = 'terms'`.

### Scenario 4 — Positional rename of a field
- A template has header fields `["supplier", "po_no", "invoice_total"]`.
- The user saves an update with fields `["supplier", "po_number", "invoice_total"]`.
- Because the list length is unchanged (3), the system detects a rename at index 1: `po_no` → `po_number`.
- The vendor's ERP field mapping configuration is updated: the key `po_no` is renamed to `po_number` in `header_map` and a warning notice is queued for review.

---

## Error Responses

| Situation | HTTP | Message |
|---|---|---|
| No template found | 404 | `"No template configured for this vendor"` |
| Save template for non-existent vendor as Admin | 400 | `"Admin must create the vendor via POST /vendors before saving a template"` |
| Save template with no fields | 400 | `"Template must contain at least one header field or line item column."` |

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_template_prompt_visibility.py`](../../tests/test_template_prompt_visibility.py) | `test_client_get_template_hides_generated_prompts_but_keeps_config` | Verifies client `GET` responses redact system/user prompts. |
| | `test_admin_get_template_receives_generated_prompt_previews` | Verifies admin `GET` responses include compiled prompt previews. |
| | `test_client_template_save_does_not_return_system_prompt_preview` | Verifies client `POST` template returns empty `system_prompt_preview`. |
| | `test_admin_template_save_returns_system_prompt_preview` | Verifies admin `POST` template returns full prompt preview. |
| [`test_single_agent_bbox.py`](../../tests/test_single_agent_bbox.py) | `test_page1_message_has_boxes_template` | Verifies user message formatting requests label grounding on page 1 only. |

---

## Quick Reference

| Operation / Path | Target Table | Fields / Outputs | Notes |
|---|---|---|---|
| Load Template config | `templates` | `header_fields`, `line_item_fields` | Enforces client vendor isolation |
| Compile prompt | n/a | `extractor.build_system_prompt` | Injects instructions + gold corrections |
| Delete stale boxes | `qwen_layout_boxes` | Row deletion | Done in `save_template` |
| Delete stale memory | `spatial_memory` | Row deletion | Done in `save_template` |
| Propagate ERP mapping | `field_mappings` | `header_map`, `line_map` keys renamed | Done in `save_template` |
