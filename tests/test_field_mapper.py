from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import field_mapper


class ApplyMappingTests(unittest.TestCase):
    def test_dict_result_renames_header_and_line_fields(self):
        result = {
            "supplier": "ACME CORP",
            "po_no": "H1583-6900",
            "line_items": [
                {"part_number": "GHO-5035", "list_cost": 70, "qty": 6},
                {"part_number": "GHO-9001", "list_cost": 12.5, "qty": 3},
            ],
        }
        mapping = {
            "header_map": {"supplier": "vendor_name", "po_no": "po_number"},
            "line_map": {"part_number": "item", "list_cost": "unit_price"},
        }
        mapped = field_mapper.apply_mapping(result, mapping)

        self.assertEqual(mapped["vendor_name"], "ACME CORP")
        self.assertEqual(mapped["po_number"], "H1583-6900")
        # Every canonical header target is present, unmapped ones are None.
        for target in field_mapper.HEADER_TARGETS:
            self.assertIn(target, mapped)
        self.assertIsNone(mapped["invoice_total"])
        # Line items are renamed under "line_items".
        self.assertEqual(len(mapped["line_items"]), 2)
        self.assertEqual(mapped["line_items"][0]["item"], "GHO-5035")
        self.assertEqual(mapped["line_items"][0]["unit_price"], 70)
        self.assertIsNone(mapped["line_items"][0]["line_total"])
        # Source-only fields (qty) are dropped — not a canonical target.
        self.assertNotIn("qty", mapped["line_items"][0])

    def test_list_result_maps_each_document(self):
        result = [
            {"po_no": "A-1", "line_items": [{"part_number": "X1"}]},
            {"po_no": "A-2", "line_items": []},
        ]
        mapping = {"header_map": {"po_no": "po_number"}, "line_map": {"part_number": "item"}}
        mapped = field_mapper.apply_mapping(result, mapping)

        self.assertIsInstance(mapped, list)
        self.assertEqual(mapped[0]["po_number"], "A-1")
        self.assertEqual(mapped[0]["line_items"][0]["item"], "X1")
        self.assertEqual(mapped[1]["po_number"], "A-2")
        self.assertEqual(mapped[1]["line_items"], [])

    def test_unknown_target_in_map_is_ignored(self):
        result = {"po_no": "P-9", "line_items": []}
        mapping = {"header_map": {"po_no": "not_a_real_field"}, "line_map": {}}
        mapped = field_mapper.apply_mapping(result, mapping)
        self.assertNotIn("not_a_real_field", mapped)

    def test_empty_mapping_yields_canonical_skeleton(self):
        result = {"po_no": "P-1", "line_items": [{"part_number": "X"}]}
        mapped = field_mapper.apply_mapping(result, {})
        self.assertEqual(set(mapped) - {"line_items"}, set(field_mapper.HEADER_TARGETS))
        self.assertTrue(all(mapped[t] is None for t in field_mapper.HEADER_TARGETS))
        self.assertEqual(mapped["line_items"], [{t: None for t in field_mapper.LINE_TARGETS}])

    def test_non_dict_result_returned_unchanged(self):
        self.assertIsNone(field_mapper.apply_mapping(None, {"header_map": {}, "line_map": {}}))
        self.assertEqual(field_mapper.apply_mapping("oops", {}), "oops")

    def test_non_dict_line_items_are_skipped(self):
        result = {"line_items": [{"part_number": "X1"}, "garbage", None]}
        mapping = {"header_map": {}, "line_map": {"part_number": "item"}}
        mapped = field_mapper.apply_mapping(result, mapping)
        self.assertEqual(len(mapped["line_items"]), 1)
        self.assertEqual(mapped["line_items"][0]["item"], "X1")

    def test_missing_source_field_maps_to_none(self):
        result = {"po_no": "P-7", "line_items": []}
        mapping = {"header_map": {"supplier": "vendor_name", "po_no": "po_number"}, "line_map": {}}
        mapped = field_mapper.apply_mapping(result, mapping)
        self.assertEqual(mapped["po_number"], "P-7")
        self.assertIsNone(mapped["vendor_name"])  # source 'supplier' absent in doc


class ApplyMappingWithSchemaTests(unittest.TestCase):
    """The schema= kwarg lets callers override the canonical target list."""

    def test_custom_schema_replaces_default_header_targets(self):
        # SyteLine-style PO Automation schema (no overlap with default AP fields).
        po_schema = {
            "header_fields": ["CustNum", "Client", "CustPo", "OrderDate"],
            "line_fields": ["Line", "Item", "QtyOrdered", "Price"],
        }
        result = {
            "buyer_code": 105240,
            "buyer_name": "ACME",
            "po_no": "PO-1",
            "line_items": [
                {"sku": "X1", "qty": 5, "price": 19.99},
            ],
        }
        mapping = {
            "header_map": {"buyer_code": "CustNum", "buyer_name": "Client", "po_no": "CustPo"},
            "line_map": {"sku": "Item", "qty": "QtyOrdered", "price": "Price"},
        }
        mapped = field_mapper.apply_mapping(result, mapping, schema=po_schema)

        # Output carries every PO header target, unmapped → None.
        self.assertEqual(set(mapped) - {"line_items"}, set(po_schema["header_fields"]))
        self.assertEqual(mapped["CustNum"], 105240)
        self.assertEqual(mapped["Client"], "ACME")
        self.assertEqual(mapped["CustPo"], "PO-1")
        self.assertIsNone(mapped["OrderDate"])
        # AP defaults must NOT appear when a schema is supplied.
        self.assertNotIn("vendor_name", mapped)
        self.assertNotIn("po_number", mapped)
        # Line items use PO line schema.
        self.assertEqual(set(mapped["line_items"][0]), set(po_schema["line_fields"]))
        self.assertEqual(mapped["line_items"][0]["Item"], "X1")
        self.assertEqual(mapped["line_items"][0]["QtyOrdered"], 5)
        self.assertEqual(mapped["line_items"][0]["Price"], 19.99)

    def test_none_schema_falls_back_to_module_defaults(self):
        result = {"supplier": "ACME", "line_items": [{"part": "X1"}]}
        mapping = {"header_map": {"supplier": "vendor_name"},
                   "line_map": {"part": "item"}}
        mapped = field_mapper.apply_mapping(result, mapping, schema=None)
        # Falls back to HEADER_TARGETS / LINE_TARGETS — vendor_name is present.
        self.assertIn("vendor_name", mapped)
        self.assertEqual(mapped["vendor_name"], "ACME")
        self.assertEqual(set(mapped) - {"line_items"}, set(field_mapper.HEADER_TARGETS))

    def test_schema_with_missing_fields_falls_back(self):
        # Schema with empty/missing field lists should not crash — falls back.
        result = {"supplier": "ACME", "line_items": []}
        mapping = {"header_map": {"supplier": "vendor_name"}, "line_map": {}}
        mapped = field_mapper.apply_mapping(result, mapping, schema={"header_fields": [], "line_fields": []})
        # Empty schema → falls back to module-level defaults (per || in apply_mapping).
        self.assertIn("vendor_name", mapped)

    def test_schema_drops_mappings_to_unknown_targets(self):
        po_schema = {
            "header_fields": ["CustNum", "Client"],
            "line_fields": ["Item"],
        }
        result = {"x": 1, "y": 2, "line_items": [{"sku": "A"}]}
        # 'vendor_name' is not in the PO schema → mapping is silently dropped
        # because output skeleton only includes the schema's fields.
        mapping = {"header_map": {"x": "CustNum", "y": "vendor_name"},
                   "line_map": {"sku": "Item"}}
        mapped = field_mapper.apply_mapping(result, mapping, schema=po_schema)
        self.assertEqual(mapped["CustNum"], 1)
        self.assertNotIn("vendor_name", mapped)
        self.assertEqual(mapped["line_items"][0]["Item"], "A")

    def test_schema_applied_to_list_result_shape(self):
        po_schema = {
            "header_fields": ["CustPo"],
            "line_fields": ["Item"],
        }
        result = [
            {"po_no": "PO-A", "line_items": [{"sku": "X"}]},
            {"po_no": "PO-B", "line_items": []},
        ]
        mapping = {"header_map": {"po_no": "CustPo"}, "line_map": {"sku": "Item"}}
        mapped = field_mapper.apply_mapping(result, mapping, schema=po_schema)
        self.assertIsInstance(mapped, list)
        self.assertEqual(mapped[0]["CustPo"], "PO-A")
        self.assertEqual(mapped[0]["line_items"][0]["Item"], "X")
        self.assertEqual(mapped[1]["CustPo"], "PO-B")
        self.assertNotIn("vendor_name", mapped[0])


class RenameDetectionTests(unittest.TestCase):
    def test_single_position_rename_detected(self):
        old = ["po_number", "vendor", "date"]
        new = ["order_number", "vendor", "date"]
        self.assertEqual(field_mapper.detect_renames(old, new), [("po_number", "order_number")])

    def test_length_change_yields_no_renames(self):
        old = ["po_number", "vendor"]
        new = ["po_number", "vendor", "date"]
        self.assertEqual(field_mapper.detect_renames(old, new), [])

    def test_apply_renames_carries_mapping_forward(self):
        field_map = {"po_number": "po_number", "supplier": "vendor_name"}
        renamed = field_mapper.apply_renames(field_map, [("po_number", "order_number")])
        self.assertEqual(renamed["order_number"], "po_number")
        self.assertNotIn("po_number", renamed)
        self.assertEqual(renamed["supplier"], "vendor_name")

    def test_none_and_empty_inputs_yield_no_renames(self):
        self.assertEqual(field_mapper.detect_renames(None, None), [])
        self.assertEqual(field_mapper.detect_renames([], []), [])

    def test_identical_lists_yield_no_renames(self):
        fields = ["a", "b", "c"]
        self.assertEqual(field_mapper.detect_renames(fields, fields), [])

    def test_multiple_position_renames_detected(self):
        old = ["po", "vendor", "date"]
        new = ["po_number", "vendor", "doc_date"]
        self.assertEqual(
            field_mapper.detect_renames(old, new),
            [("po", "po_number"), ("date", "doc_date")],
        )

    def test_apply_renames_ignores_unmapped_old_name(self):
        field_map = {"supplier": "vendor_name"}
        renamed = field_mapper.apply_renames(field_map, [("po", "po_number")])
        self.assertEqual(renamed, {"supplier": "vendor_name"})


if __name__ == "__main__":
    unittest.main()
