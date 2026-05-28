"""
Tests for the output-schema CRUD endpoints and the vendor mapping
endpoints' schema awareness.

Covers:
  - GET    /schemas              — list (any authenticated user)
  - POST   /schemas              — admin creates a custom schema
  - PUT    /schemas/{id}         — admin edits, commits new snapshot
  - DELETE /schemas/{id}         — admin deletes non-system; system blocked
  - POST   /schemas/{id}/reset   — restores from snapshot
  - Client (non-admin) gets 403 on any mutating schema route
  - GET  /vendors/{id}/mapping   — returns schemas[] + target fields from
                                   the vendor's assigned schema
  - POST /vendors/{id}/mapping   — admin-only; filters maps to the schema
                                   fields; persists schema_id
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.main as main
from backend.auth import get_current_user, require_admin


# ── Fixtures ──────────────────────────────────────────────────────────────

def _ap_schema() -> dict:
    fields = ["vendor_name", "invoice_number", "po_number"]
    line_fields = ["item", "unit_price"]
    return {
        "id": 1,
        "name": "AP Automation",
        "slug": "ap_automation",
        "is_system": True,
        "header_fields": fields,
        "line_fields": line_fields,
        "header_fields_snapshot": fields,
        "line_fields_snapshot": line_fields,
    }


def _po_schema() -> dict:
    fields = ["CustNum", "Client", "CustPo", "OrderDate"]
    line_fields = ["Line", "Item", "QtyOrdered", "Price"]
    return {
        "id": 2,
        "name": "PO Automation",
        "slug": "po_automation",
        "is_system": True,
        "header_fields": fields,
        "line_fields": line_fields,
        "header_fields_snapshot": fields,
        "line_fields_snapshot": line_fields,
    }


def _custom_schema(sid: int = 5) -> dict:
    return {
        "id": sid,
        "name": "My Custom",
        "slug": "my_custom",
        "is_system": False,
        "header_fields": ["a", "b"],
        "line_fields": ["x"],
        "header_fields_snapshot": ["a", "b"],
        "line_fields_snapshot": ["x"],
    }


def _client_user(uid: str = "client-uuid") -> dict:
    return {"id": uid, "role": "client", "email": f"{uid}@test"}


class _ScopedOverrides:
    """Temporarily swap FastAPI dependency overrides for one test block."""
    def __init__(self, app, overrides: dict):
        self.app = app
        self.overrides = overrides
        self._saved: dict = {}

    def __enter__(self):
        self._saved = dict(self.app.dependency_overrides)
        self.app.dependency_overrides.update(self.overrides)
        return self

    def __exit__(self, *exc):
        self.app.dependency_overrides.clear()
        self.app.dependency_overrides.update(self._saved)


# ── Schema CRUD ───────────────────────────────────────────────────────────

class ListSchemasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_returns_seeded_schemas(self) -> None:
        rows = [_ap_schema(), _po_schema()]
        with patch.object(main.db_mod, "get_all_schemas",
                          new=AsyncMock(return_value=rows)):
            r = self.client.get("/schemas")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["slug"], "ap_automation")
        self.assertEqual(body[1]["slug"], "po_automation")
        self.assertTrue(body[0]["is_system"])

    def test_client_user_can_list_schemas(self) -> None:
        """Non-admins need the list for the mapper dropdown."""
        rows = [_ap_schema()]
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}), \
             patch.object(main.db_mod, "get_all_schemas",
                          new=AsyncMock(return_value=rows)):
            r = self.client.get("/schemas")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 1)


class CreateSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_admin_creates_custom_schema(self) -> None:
        created = _custom_schema(sid=7)
        with patch.object(main.db_mod, "create_schema",
                          new=AsyncMock(return_value=created)) as mock_create:
            r = self.client.post("/schemas", json={
                "name": "My Custom",
                "header_fields": ["a", "b"],
                "line_fields": ["x"],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["name"], "My Custom")
        self.assertFalse(body["is_system"])
        # DB helper received properly typed lists.
        kwargs = mock_create.await_args.kwargs
        self.assertEqual(kwargs["name"], "My Custom")
        self.assertEqual(kwargs["header_fields"], ["a", "b"])
        self.assertEqual(kwargs["line_fields"], ["x"])

    def test_missing_name_returns_400(self) -> None:
        r = self.client.post("/schemas", json={
            "name": "  ",
            "header_fields": [],
            "line_fields": [],
        })
        self.assertEqual(r.status_code, 400)

    def test_non_list_fields_returns_400(self) -> None:
        r = self.client.post("/schemas", json={
            "name": "Bad",
            "header_fields": "oops",
            "line_fields": [],
        })
        self.assertEqual(r.status_code, 400)

    def test_client_cannot_create_schema(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.post("/schemas", json={
                "name": "X", "header_fields": [], "line_fields": [],
            })
        self.assertEqual(r.status_code, 403)


class UpdateSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_admin_updates_existing_schema(self) -> None:
        # Saved snapshot reflects the new field list (snapshot moves on save).
        updated = {**_custom_schema(sid=5),
                   "name": "Renamed",
                   "header_fields": ["a", "b", "c"],
                   "header_fields_snapshot": ["a", "b", "c"]}
        with patch.object(main.db_mod, "update_schema",
                          new=AsyncMock(return_value=updated)):
            r = self.client.put("/schemas/5", json={
                "name": "Renamed",
                "header_fields": ["a", "b", "c"],
                "line_fields": ["x"],
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["name"], "Renamed")
        # Snapshot was committed in tandem with the field update.
        self.assertEqual(body["header_fields_snapshot"], ["a", "b", "c"])

    def test_unknown_schema_returns_404(self) -> None:
        with patch.object(main.db_mod, "update_schema",
                          new=AsyncMock(side_effect=ValueError("Schema 99 not found"))):
            r = self.client.put("/schemas/99", json={
                "name": "X", "header_fields": [], "line_fields": [],
            })
        self.assertEqual(r.status_code, 404)

    def test_client_cannot_update_schema(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.put("/schemas/1", json={
                "name": "X", "header_fields": [], "line_fields": [],
            })
        self.assertEqual(r.status_code, 403)


class DeleteSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_admin_deletes_custom_schema(self) -> None:
        with patch.object(main.db_mod, "delete_schema",
                          new=AsyncMock(return_value=None)):
            r = self.client.delete("/schemas/5")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_deleting_system_schema_returns_400(self) -> None:
        with patch.object(main.db_mod, "delete_schema",
                          new=AsyncMock(side_effect=ValueError("System schemas cannot be deleted"))):
            r = self.client.delete("/schemas/1")
        self.assertEqual(r.status_code, 400)

    def test_client_cannot_delete_schema(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.delete("/schemas/5")
        self.assertEqual(r.status_code, 403)


class ResetSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_reset_restores_snapshot(self) -> None:
        # Caller had edited header_fields to ['a','b','EXTRA']; reset rolls back.
        restored = _custom_schema(sid=5)  # snapshot = ['a','b'] / ['x']
        with patch.object(main.db_mod, "reset_schema",
                          new=AsyncMock(return_value=restored)):
            r = self.client.post("/schemas/5/reset")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["header_fields"], body["header_fields_snapshot"])
        self.assertEqual(body["header_fields"], ["a", "b"])

    def test_unknown_schema_returns_404(self) -> None:
        with patch.object(main.db_mod, "reset_schema",
                          new=AsyncMock(side_effect=ValueError("Schema 99 not found"))):
            r = self.client.post("/schemas/99/reset")
        self.assertEqual(r.status_code, 404)

    def test_client_cannot_reset_schema(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.post("/schemas/1/reset")
        self.assertEqual(r.status_code, 403)


# ── Vendor mapping endpoints (schema awareness) ───────────────────────────

class GetVendorMappingSchemaTests(unittest.TestCase):
    """GET /vendors/{id}/mapping must return schemas[] and use the vendor's
    assigned schema for target_header_fields / target_line_fields."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_response_includes_schemas_list_and_assigned_targets(self) -> None:
        # Vendor is mapped against PO Automation (id=2).
        tmpl = {"id": 10, "header_fields": ["po_no", "supplier"],
                "line_item_fields": ["sku", "qty"]}
        mapping = {
            "schema_id": 2,
            "header_map": {"po_no": "CustPo"},
            "line_map": {"sku": "Item"},
            "pending_notices": [],
        }
        schemas = [_ap_schema(), _po_schema()]
        with patch.object(main.db_mod, "get_template",
                          new=AsyncMock(return_value=tmpl)), \
             patch.object(main.db_mod, "get_field_mapping",
                          new=AsyncMock(return_value=mapping)), \
             patch.object(main.db_mod, "get_all_schemas",
                          new=AsyncMock(return_value=schemas)):
            r = self.client.get("/vendors/v-1/mapping")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # Schema metadata is surfaced for the dropdown.
        self.assertEqual(body["schema_id"], 2)
        self.assertEqual(body["schema_name"], "PO Automation")
        self.assertEqual(len(body["schemas"]), 2)
        slugs = [s["slug"] for s in body["schemas"]]
        self.assertIn("ap_automation", slugs)
        self.assertIn("po_automation", slugs)
        # Target fields come from the assigned schema.
        self.assertEqual(body["target_header_fields"], _po_schema()["header_fields"])
        self.assertEqual(body["target_line_fields"], _po_schema()["line_fields"])

    def test_schemas_payload_carries_field_lists_for_dropdown_switching(self) -> None:
        """The mapper UI uses schemas[].header_fields/line_fields when the user
        changes the dropdown selection — these must be present in the response."""
        tmpl = {"id": 10, "header_fields": [], "line_item_fields": []}
        schemas = [_ap_schema(), _po_schema()]
        with patch.object(main.db_mod, "get_template",
                          new=AsyncMock(return_value=tmpl)), \
             patch.object(main.db_mod, "get_field_mapping",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "get_all_schemas",
                          new=AsyncMock(return_value=schemas)):
            r = self.client.get("/vendors/v-1/mapping")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        po = next(s for s in body["schemas"] if s["slug"] == "po_automation")
        self.assertEqual(po["header_fields"], _po_schema()["header_fields"])
        self.assertEqual(po["line_fields"], _po_schema()["line_fields"])

    def test_unconfigured_mapping_falls_back_to_ap_automation(self) -> None:
        tmpl = {"id": 10, "header_fields": ["po_no"], "line_item_fields": []}
        schemas = [_ap_schema(), _po_schema()]
        with patch.object(main.db_mod, "get_template",
                          new=AsyncMock(return_value=tmpl)), \
             patch.object(main.db_mod, "get_field_mapping",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "get_all_schemas",
                          new=AsyncMock(return_value=schemas)):
            r = self.client.get("/vendors/v-1/mapping")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["schema_id"], 1)
        self.assertEqual(body["schema_name"], "AP Automation")
        self.assertFalse(body["configured"])

    def test_stale_schema_id_falls_back_to_ap_automation(self) -> None:
        """If the assigned schema was deleted, fall back to AP Automation."""
        tmpl = {"id": 10, "header_fields": [], "line_item_fields": []}
        mapping = {"schema_id": 999, "header_map": {}, "line_map": {},
                   "pending_notices": []}
        schemas = [_ap_schema(), _po_schema()]
        with patch.object(main.db_mod, "get_template",
                          new=AsyncMock(return_value=tmpl)), \
             patch.object(main.db_mod, "get_field_mapping",
                          new=AsyncMock(return_value=mapping)), \
             patch.object(main.db_mod, "get_all_schemas",
                          new=AsyncMock(return_value=schemas)):
            r = self.client.get("/vendors/v-1/mapping")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["schema_id"], 1)
        self.assertEqual(body["schema_name"], "AP Automation")


class SaveVendorMappingSchemaTests(unittest.TestCase):
    """POST /vendors/{id}/mapping must persist schema_id and filter the
    submitted maps to the assigned schema's fields."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_save_persists_schema_id_and_filters_unknown_targets(self) -> None:
        tmpl = {"id": 10, "header_fields": ["po_no", "supplier"],
                "line_item_fields": ["sku"]}
        po = _po_schema()
        saved = {
            "schema_id": po["id"],
            "header_map": {"po_no": "CustPo"},
            "line_map": {"sku": "Item"},
        }
        with patch.object(main.db_mod, "get_template",
                          new=AsyncMock(return_value=tmpl)), \
             patch.object(main.db_mod, "get_schema_by_id",
                          new=AsyncMock(return_value=po)), \
             patch.object(main.db_mod, "get_field_mapping",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "upsert_field_mapping",
                          new=AsyncMock(return_value=saved)) as mock_upsert:
            # 'vendor_name' is not a PO field — must be dropped before persistence.
            r = self.client.post("/vendors/v-1/mapping", json={
                "schema_id": 2,
                "header_map": {"po_no": "CustPo", "supplier": "vendor_name"},
                "line_map": {"sku": "Item"},
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["schema_id"], 2)
        # The upsert call received the filtered maps.
        args, kwargs = mock_upsert.await_args.args, mock_upsert.await_args.kwargs
        self.assertEqual(kwargs.get("schema_id"), 2)
        # Positional args: (pool, vendor_id, template_id, header_map, line_map, ...)
        passed_header_map = args[3]
        self.assertEqual(passed_header_map, {"po_no": "CustPo"})  # supplier dropped
        passed_line_map = args[4]
        self.assertEqual(passed_line_map, {"sku": "Item"})

    def test_missing_template_returns_404(self) -> None:
        with patch.object(main.db_mod, "get_template",
                          new=AsyncMock(return_value=None)):
            r = self.client.post("/vendors/v-1/mapping", json={
                "schema_id": 1,
                "header_map": {},
                "line_map": {},
            })
        self.assertEqual(r.status_code, 404)

    def test_invalid_map_types_return_400(self) -> None:
        tmpl = {"id": 10, "header_fields": [], "line_item_fields": []}
        with patch.object(main.db_mod, "get_template",
                          new=AsyncMock(return_value=tmpl)):
            r = self.client.post("/vendors/v-1/mapping", json={
                "schema_id": 1,
                "header_map": "not-an-object",
                "line_map": {},
            })
        self.assertEqual(r.status_code, 400)

    def test_save_without_schema_id_falls_back_to_ap_automation(self) -> None:
        """Older clients can post without schema_id; backend defaults to AP."""
        tmpl = {"id": 10, "header_fields": ["po_no"], "line_item_fields": []}
        ap = _ap_schema()
        saved = {"schema_id": ap["id"], "header_map": {"po_no": "po_number"},
                 "line_map": {}}
        with patch.object(main.db_mod, "get_template",
                          new=AsyncMock(return_value=tmpl)), \
             patch.object(main.db_mod, "get_schema_by_slug",
                          new=AsyncMock(return_value=ap)), \
             patch.object(main.db_mod, "get_field_mapping",
                          new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "upsert_field_mapping",
                          new=AsyncMock(return_value=saved)) as mock_upsert:
            r = self.client.post("/vendors/v-1/mapping", json={
                "header_map": {"po_no": "po_number"},
                "line_map": {},
            })
        self.assertEqual(r.status_code, 200)
        kwargs = mock_upsert.await_args.kwargs
        self.assertEqual(kwargs.get("schema_id"), ap["id"])

    def test_client_cannot_save_mapping(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: lambda: _client_user()}):
            main.app.dependency_overrides.pop(require_admin, None)
            r = self.client.post("/vendors/v-1/mapping", json={
                "schema_id": 1, "header_map": {}, "line_map": {},
            })
        self.assertEqual(r.status_code, 403)


class MappingSampleStripsMetaKeysTests(unittest.TestCase):
    """The mapping/sample endpoint must strip leading-underscore meta keys
    (e.g. _page) from both header and line_item before returning — they are
    internal merge bookkeeping and would clutter the source panel."""

    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_underscore_keys_stripped_from_sample_payload(self) -> None:
        extraction = {
            "id": 42,
            "filename": "test.pdf",
            "format_type": "single_po_multipage",
            "result": {
                "bill_to": "ACME",
                "po_no": "P-1",
                "_page": 1,             # internal — must not surface
                "_total_pages": 3,      # internal — must not surface
                "line_items": [
                    {"sku": "X1", "qty": 5, "_page": 1},  # _page must be stripped
                    {"sku": "X2", "qty": 7, "_page": 2},
                ],
            },
        }
        with patch.object(main.db_mod, "list_extractions",
                          new=AsyncMock(return_value=[extraction])):
            r = self.client.get("/vendors/v-1/mapping/sample")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["has_data"])
        # Header contains real fields, no underscore meta keys.
        self.assertEqual(set(body["header"]), {"bill_to", "po_no"})
        self.assertNotIn("_page", body["header"])
        self.assertNotIn("_total_pages", body["header"])
        # Line item also stripped.
        self.assertEqual(set(body["line_item"]), {"sku", "qty"})
        self.assertNotIn("_page", body["line_item"])
        # Total count still reflects all items (filter is presentation-only).
        self.assertEqual(body["line_item_count"], 2)

    def test_returns_no_data_when_no_extractions_exist(self) -> None:
        with patch.object(main.db_mod, "list_extractions",
                          new=AsyncMock(return_value=[])):
            r = self.client.get("/vendors/v-1/mapping/sample")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["has_data"])


if __name__ == "__main__":
    unittest.main()
