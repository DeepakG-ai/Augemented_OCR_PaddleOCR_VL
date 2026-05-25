"""
test_extractor_merge.py -- Unit tests for extractor.merge_results and
                           worker._strip_internal_keys.

Rules under test:
  - merge_results combines header fields from page 1 and line items from all pages.
  - Each merged line item is tagged with _page (source page number) so the review
    UI can show only the current page's rows. _page is stripped by the frontend
    before COPY/DOWNLOAD JSON and before sending corrected_result to the backend.
  - _-prefixed keys on page_result entries (e.g. _page, _total_pages) are internal
    pipeline metadata.
  - Error pages (_error key) are silently skipped.
  - _strip_internal_keys (worker.py) strips _page for use-cases that need clean output.

Run:
  .venv\\Scripts\\python.exe -m pytest tests/test_extractor_merge.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backend.extractor as extractor
from backend.worker import _strip_internal_keys


class MergeResultsTests(unittest.TestCase):

    # ── legacy flat format ───────────────────────────────────────────────────

    def test_legacy_header_from_page1(self):
        """Header fields come from page 1; line items from all pages with _page tag."""
        page_results = [
            {"_page": 1, "vendor_name": "ACME", "po_number": "PO-001",
             "line_items": [{"item": "A", "qty": 1}]},
            {"_page": 2, "vendor_name": "IGNORED",
             "line_items": [{"item": "B", "qty": 2}]},
        ]
        merged = extractor.merge_results(
            page_results,
            header_fields=["vendor_name", "po_number"],
            line_item_fields=["item", "qty"],
        )
        self.assertEqual(merged["vendor_name"], "ACME")
        self.assertEqual(merged["po_number"], "PO-001")
        self.assertEqual(merged["line_items"], [
            {"item": "A", "qty": 1, "_page": 1},
            {"item": "B", "qty": 2, "_page": 2},
        ])

    def test_legacy_page_key_in_line_items(self):
        """_page IS present in each merged line item for UI per-page filtering."""
        page_results = [
            {"_page": 1, "line_items": [{"item": "X"}]},
            {"_page": 2, "line_items": [{"item": "Y"}]},
        ]
        merged = extractor.merge_results(page_results, header_fields=[], line_item_fields=[])
        self.assertEqual(merged["line_items"][0]["_page"], 1)
        self.assertEqual(merged["line_items"][1]["_page"], 2)

    def test_legacy_duplicate_line_items_kept(self):
        """Identical items on different pages are both kept (not deduplicated)."""
        page_results = [
            {"_page": 1, "line_items": [{"item": "A", "qty": 1}]},
            {"_page": 2, "line_items": [{"item": "A", "qty": 1}]},
        ]
        merged = extractor.merge_results(page_results, header_fields=[], line_item_fields=[])
        self.assertEqual(len(merged["line_items"]), 2)
        self.assertEqual(merged["line_items"], [
            {"item": "A", "qty": 1, "_page": 1},
            {"item": "A", "qty": 1, "_page": 2},
        ])

    def test_legacy_error_pages_skipped(self):
        """Pages with _error are ignored; their line items must not appear."""
        page_results = [
            {"_page": 1, "vendor_name": "ACME", "line_items": [{"item": "A"}]},
            {"_page": 2, "_error": "LLM timeout", "line_items": [{"item": "MUST_NOT_APPEAR"}]},
            {"_page": 3, "line_items": [{"item": "B"}]},
        ]
        merged = extractor.merge_results(
            page_results,
            header_fields=["vendor_name"],
            line_item_fields=["item"],
        )
        self.assertEqual(merged["vendor_name"], "ACME")
        self.assertEqual(merged["line_items"], [
            {"item": "A", "_page": 1},
            {"item": "B", "_page": 3},
        ])

    def test_legacy_auto_extract_first_valid_page_as_header(self):
        """When page 1 has _error, headers come from the first valid page."""
        page_results = [
            {"_page": 1, "_error": "render failed"},
            {"_page": 2, "vendor_name": "ACME", "po_number": "PO-1",
             "line_items": [{"item": "A"}]},
        ]
        merged = extractor.merge_results(page_results, header_fields=[], line_item_fields=[])
        self.assertEqual(merged["vendor_name"], "ACME")
        self.assertEqual(merged["po_number"], "PO-1")
        self.assertEqual(merged["line_items"], [{"item": "A", "_page": 2}])

    def test_all_pages_error_returns_failure_sentinel(self):
        """All pages errored → returns sentinel with _all_pages_failed=True and error list."""
        page_results = [
            {"_page": 1, "_error": "bad"},
            {"_page": 2, "_error": "bad"},
        ]
        merged = extractor.merge_results(page_results, header_fields=[], line_item_fields=[])
        self.assertTrue(merged.get("_all_pages_failed"))
        self.assertEqual(merged.get("errors"), ["bad", "bad"])

    # ── v3 format (fields wrapper) ───────────────────────────────────────────

    def test_v3_line_items_merged_across_pages(self):
        """v3 format: line items from each page's fields dict are combined with _page tag."""
        page_results = [
            {"_page": 1, "fields": {"vendor_name": "ACME",
                                    "line_items": [{"item": "Page 1 Item"}]}},
            {"_page": 2, "fields": {"line_items": [{"item": "Page 2 A"},
                                                    {"item": "Page 2 B"}]}},
        ]
        merged = extractor.merge_results(
            page_results, header_fields=[], line_item_fields=["item"]
        )
        self.assertEqual(merged["line_items"], [
            {"item": "Page 1 Item", "_page": 1},
            {"item": "Page 2 A", "_page": 2},
            {"item": "Page 2 B", "_page": 2},
        ])

    def test_v3_page_key_in_line_items(self):
        """v3 format: _page IS present in each merged line item for UI per-page filtering."""
        page_results = [
            {"_page": 1, "fields": {"line_items": [{"item": "A"}]}},
            {"_page": 2, "fields": {"line_items": [{"item": "B"}]}},
        ]
        merged = extractor.merge_results(page_results, header_fields=[], line_item_fields=[])
        self.assertEqual(merged["line_items"][0]["_page"], 1)
        self.assertEqual(merged["line_items"][1]["_page"], 2)

    def test_v3_nested_header_flattened(self):
        """v3 format: nested address objects in header fields are flattened to strings."""
        page_results = [
            {
                "_page": 1,
                "fields": {
                    "bill_to": {
                        "name": "SLACAN",
                        "address": "145 ROY BLVD.\nBRANTFORD ON N3T 6E3",
                        "postal_code": "N3T 6E3",
                    },
                    "line_items": [{"item": "A"}],
                },
            },
        ]
        merged = extractor.merge_results(
            page_results, header_fields=["bill_to"], line_item_fields=["item"]
        )
        self.assertEqual(
            merged["bill_to"],
            "SLACAN\n145 ROY BLVD.\nBRANTFORD ON N3T 6E3\nN3T 6E3",
        )
        self.assertEqual(merged["line_items"], [{"item": "A", "_page": 1}])

    def test_v3_error_pages_skipped(self):
        """v3 format: pages with _error are ignored."""
        page_results = [
            {"_page": 1, "fields": {"line_items": [{"item": "A"}]}},
            {"_page": 2, "_error": "timeout", "fields": {"line_items": [{"item": "BAD"}]}},
        ]
        merged = extractor.merge_results(page_results, header_fields=[], line_item_fields=[])
        self.assertEqual(merged["line_items"], [{"item": "A", "_page": 1}])

    # ── normalize_header_values ──────────────────────────────────────────────

    def test_normalize_leaves_line_items_unchanged(self):
        """normalize_header_values must not flatten objects inside line_items."""
        result = {
            "ship_to": {"name": "SLACAN", "address": "145 ROY BLVD."},
            "line_items": [{"description": {"name": "KEEP", "address": "OBJECT"}}],
        }
        normalized = extractor.normalize_header_values(result)
        self.assertEqual(normalized["ship_to"], "SLACAN\n145 ROY BLVD.")
        self.assertEqual(
            normalized["line_items"],
            [{"description": {"name": "KEEP", "address": "OBJECT"}}],
        )


class StripInternalKeysTests(unittest.TestCase):
    """Tests for worker._strip_internal_keys — the DB-save safety net."""

    def test_strips_page_from_line_items(self):
        result = {
            "vendor_name": "ACME",
            "line_items": [
                {"item": "A", "_page": 1},
                {"item": "B", "_page": 2},
            ],
        }
        cleaned = _strip_internal_keys(result)
        self.assertEqual(cleaned["line_items"], [{"item": "A"}, {"item": "B"}])

    def test_leaves_header_fields_untouched(self):
        result = {"vendor_name": "ACME", "po_number": "PO-1", "line_items": []}
        cleaned = _strip_internal_keys(result)
        self.assertEqual(cleaned["vendor_name"], "ACME")
        self.assertEqual(cleaned["po_number"], "PO-1")

    def test_handles_po_per_page_list(self):
        """po_per_page format returns a list — strip handles lists too."""
        result = [
            {"vendor_name": "ACME", "line_items": [{"item": "A", "_page": 1}]},
            {"vendor_name": "ACME", "line_items": [{"item": "B", "_page": 2}]},
        ]
        cleaned = _strip_internal_keys(result)
        self.assertIsInstance(cleaned, list)
        self.assertEqual(cleaned[0]["line_items"], [{"item": "A"}])
        self.assertEqual(cleaned[1]["line_items"], [{"item": "B"}])

    def test_handles_v3_nested_fields(self):
        """v3 format wraps line_items under a 'fields' key."""
        result = {
            "fields": {
                "vendor_name": "ACME",
                "line_items": [{"item": "A", "_page": 1}],
            }
        }
        cleaned = _strip_internal_keys(result)
        self.assertEqual(cleaned["fields"]["line_items"], [{"item": "A"}])

    def test_handles_none_and_empty(self):
        self.assertIsNone(_strip_internal_keys(None))
        self.assertEqual(_strip_internal_keys({}), {})
        self.assertEqual(_strip_internal_keys([]), [])

    def test_does_not_strip_normal_keys(self):
        result = {"line_items": [{"item": "A", "unit_price": "10.00", "qty": "2"}]}
        cleaned = _strip_internal_keys(result)
        self.assertEqual(cleaned["line_items"], [{"item": "A", "unit_price": "10.00", "qty": "2"}])


if __name__ == "__main__":
    unittest.main()
