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


def _template() -> dict:
    return {
        "id": 10,
        "header_fields": ["supplier", "po_no"],
        "line_item_fields": ["qty", "sku"],
    }


def _schema() -> dict:
    return {
        "id": 3,
        "name": "AP Automation",
        "header_fields": ["vendor_name", "po_number"],
        "line_fields": ["quantity_ordered", "item"],
    }


class VendorMappingApiValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def test_save_mapping_rejects_unknown_source_field(self) -> None:
        with patch.object(main, "assert_vendor_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=_template())), \
             patch.object(main.db_mod, "get_schema_by_slug", new=AsyncMock(return_value=_schema())), \
             patch.object(main.db_mod, "upsert_field_mapping", new=AsyncMock()) as mock_upsert:
            response = self.client.post(
                "/vendors/V1/mapping",
                json={
                    "header_map": {"unknown_supplier": "vendor_name"},
                    "line_map": {},
                },
            )

        self.assertEqual(response.status_code, 400)
        mock_upsert.assert_not_awaited()

    def test_save_mapping_rejects_unknown_target_field(self) -> None:
        with patch.object(main, "assert_vendor_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=_template())), \
             patch.object(main.db_mod, "get_schema_by_slug", new=AsyncMock(return_value=_schema())), \
             patch.object(main.db_mod, "upsert_field_mapping", new=AsyncMock()) as mock_upsert:
            response = self.client.post(
                "/vendors/V1/mapping",
                json={
                    "header_map": {"supplier": "not_a_real_target"},
                    "line_map": {},
                },
            )

        self.assertEqual(response.status_code, 400)
        mock_upsert.assert_not_awaited()

    def test_save_mapping_rejects_unknown_schema_id(self) -> None:
        with patch.object(main, "assert_vendor_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=_template())), \
             patch.object(main.db_mod, "get_schema_by_id", new=AsyncMock(return_value=None)):
            response = self.client.post(
                "/vendors/V1/mapping",
                json={
                    "schema_id": 999,
                    "header_map": {"supplier": "vendor_name"},
                    "line_map": {},
                },
            )

        self.assertEqual(response.status_code, 404)

    def test_save_mapping_persists_valid_mapping_without_pruning(self) -> None:
        async def fake_upsert(
            _pool,
            _vendor_id,
            _template_id,
            header_map,
            line_map,
            *_args,
            schema_id=None,
        ):
            return {
                "schema_id": schema_id,
                "header_map": header_map,
                "line_map": line_map,
            }

        with patch.object(main, "assert_vendor_access", new=AsyncMock()), \
             patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=_template())), \
             patch.object(main.db_mod, "get_schema_by_slug", new=AsyncMock(return_value=_schema())), \
             patch.object(main.db_mod, "get_field_mapping", new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "upsert_field_mapping", new=fake_upsert):
            response = self.client.post(
                "/vendors/V1/mapping",
                json={
                    "header_map": {"supplier": "vendor_name"},
                    "line_map": {"qty": "quantity_ordered"},
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["header_map"], {"supplier": "vendor_name"})
        self.assertEqual(payload["line_map"], {"qty": "quantity_ordered"})


if __name__ == "__main__":
    unittest.main()
