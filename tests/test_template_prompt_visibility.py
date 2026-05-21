from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import backend.main as main
from backend.auth import get_current_user


def _admin() -> dict:
    return {"id": "admin-1", "role": "admin", "email": "admin@test"}


def _client() -> dict:
    return {"id": "client-1", "role": "client", "email": "client@test"}


def _template_row(**overrides) -> dict:
    row = {
        "id": 42,
        "vendor_id": "VENDOR1",
        "format_type": "single_po_multipage",
        "header_fields": ["supplier", "po_number"],
        "line_item_fields": ["item", "qty"],
        "prompt_instructions": "Read supplier from top-left block.",
        "extraction_rules": ["Never guess missing values."],
        "system_prompt": "cached prompt should not leak",
        "prompt_hash": "abc123",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


class _ScopedOverrides:
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


class TemplatePromptVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.app.state.pool = object()
        cls.client = TestClient(main.app)

    def test_client_get_template_hides_generated_prompts_but_keeps_config(self) -> None:
        gold = AsyncMock(return_value=[])
        with _ScopedOverrides(main.app, {get_current_user: _client}), \
             patch.object(main.db_mod, "get_vendor_owner", new=AsyncMock(return_value="client-1")), \
             patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=_template_row())), \
             patch.object(main.db_mod, "get_gold_examples", new=gold):
            response = self.client.get("/vendors/VENDOR1/template")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["prompt_instructions"], "Read supplier from top-left block.")
        self.assertEqual(body["extraction_rules"], ["Never guess missing values."])
        self.assertIsNone(body["system_prompt"])
        self.assertIsNone(body["user_prompt"])
        self.assertIsNone(body["system_prompt_page1"])
        self.assertIsNone(body["user_prompt_page1"])
        self.assertIsNone(body["system_prompt_page2"])
        self.assertIsNone(body["user_prompt_page2"])
        gold.assert_not_awaited()

    def test_admin_get_template_receives_generated_prompt_previews(self) -> None:
        with _ScopedOverrides(main.app, {get_current_user: _admin}), \
             patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=_template_row())), \
             patch.object(main.db_mod, "get_vendor", new=AsyncMock(return_value={"id": "VENDOR1", "name": "Vendor One"})), \
             patch.object(main.db_mod, "get_gold_examples", new=AsyncMock(return_value=[])):
            response = self.client.get("/vendors/VENDOR1/template")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("<document_context>", body["system_prompt_page1"])
        self.assertIn("<extraction_rules>", body["system_prompt_page1"])
        self.assertIn('"boxes"', body["user_prompt_page1"])
        self.assertNotIn("vendor_confirmed", body["user_prompt_page1"])
        self.assertIn("<document_context>", body["system_prompt_page2"])
        self.assertIn("<extraction_rules>", body["system_prompt_page2"])
        self.assertNotIn('"boxes"', body["user_prompt_page2"])

    def test_client_template_save_does_not_return_system_prompt_preview(self) -> None:
        saved = _template_row(system_prompt="new prompt")
        with _ScopedOverrides(main.app, {get_current_user: _client}), \
             patch.object(main.db_mod, "get_vendor", new=AsyncMock(return_value={"id": "VENDOR1", "name": "Vendor One"})), \
             patch.object(main.db_mod, "get_vendor_owner", new=AsyncMock(return_value="client-1")), \
             patch.object(main.db_mod, "get_gold_examples", new=AsyncMock(return_value=[])), \
             patch.object(main.db_mod, "upsert_vendor", new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "upsert_template", new=AsyncMock(return_value=saved)), \
             patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=saved)), \
             patch.object(main.db_mod, "delete_stale_qwen_layout_boxes", new=AsyncMock(return_value=0)), \
             patch.object(main.db_mod, "delete_stale_spatial_memory", new=AsyncMock(return_value=0)):
            response = self.client.post(
                "/vendors/VENDOR1/template",
                json={
                    "format_type": "single_po_multipage",
                    "vendor_name": "Vendor One",
                    "header_fields": ["supplier"],
                    "line_item_fields": ["item"],
                    "prompt_instructions": "Client editable instructions.",
                    "extraction_rules": ["Client editable rule."],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["system_prompt_preview"], "")

    def test_admin_template_save_returns_system_prompt_preview(self) -> None:
        saved = _template_row(system_prompt="new prompt")
        with _ScopedOverrides(main.app, {get_current_user: _admin}), \
             patch.object(main.db_mod, "get_vendor", new=AsyncMock(return_value={"id": "VENDOR1", "name": "Vendor One"})), \
             patch.object(main.db_mod, "get_gold_examples", new=AsyncMock(return_value=[])), \
             patch.object(main.db_mod, "upsert_vendor", new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "upsert_template", new=AsyncMock(return_value=saved)), \
             patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=saved)), \
             patch.object(main.db_mod, "delete_stale_qwen_layout_boxes", new=AsyncMock(return_value=0)), \
             patch.object(main.db_mod, "delete_stale_spatial_memory", new=AsyncMock(return_value=0)):
            response = self.client.post(
                "/vendors/VENDOR1/template",
                json={
                    "format_type": "single_po_multipage",
                    "vendor_name": "Vendor One",
                    "header_fields": ["supplier"],
                    "line_item_fields": ["item"],
                    "prompt_instructions": "Admin editable instructions.",
                    "extraction_rules": ["Admin editable rule."],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("You are a highly accurate", response.json()["system_prompt_preview"])


if __name__ == "__main__":
    unittest.main()
