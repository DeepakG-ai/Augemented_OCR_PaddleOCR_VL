from __future__ import annotations

import unittest

from pydantic import ValidationError

from backend.models import (
    ApiKeyCreate,
    CorrectionSaveRequest,
    LoginRequest,
    TemplateCreate,
    UserCreate,
    VendorMappingSave,
)


class AuthEmailValidationTests(unittest.TestCase):
    def test_user_create_rejects_common_bad_email_shapes(self) -> None:
        bad_emails = [
            "a..b@c.com",
            ".a@b.com",
            "a@b..c.com",
            "a@b.c.",
            "a@b_c.com",
        ]

        for email in bad_emails:
            with self.subTest(email=email):
                with self.assertRaises(ValidationError):
                    UserCreate(email=email, password="strongpass1", role="client")

    def test_login_request_validates_email_too(self) -> None:
        with self.assertRaises(ValidationError):
            LoginRequest(email="not-an-email", password="password")

    def test_email_is_trimmed_and_normalized(self) -> None:
        model = UserCreate(email="  JOHN@Example.COM  ", password="strongpass1", role="client")

        self.assertEqual(str(model.email), "john@example.com")


class ApiKeyValidationTests(unittest.TestCase):
    def test_expires_days_allows_only_supported_options(self) -> None:
        owner = "00000000-0000-0000-0000-000000000001"
        for days in (30, 90, 365, None):
            with self.subTest(days=days):
                model = ApiKeyCreate(label="po_automation", owner_user_id=owner, expires_days=days)
                self.assertEqual(model.expires_days, days)

        for days in (0, -1, 31, 99999):
            with self.subTest(days=days):
                with self.assertRaises(ValidationError):
                    ApiKeyCreate(label="po_automation", owner_user_id=owner, expires_days=days)

    def test_owner_user_id_must_be_uuid(self) -> None:
        with self.assertRaises(ValidationError):
            ApiKeyCreate(label="po_automation", owner_user_id="client-A")


class CorrectionPayloadValidationTests(unittest.TestCase):
    def test_corrected_result_must_be_object_or_list_of_objects(self) -> None:
        with self.assertRaises(ValidationError):
            CorrectionSaveRequest(corrected_result="ACME")

        model = CorrectionSaveRequest(corrected_result=[{"vendor_name": "ACME"}])
        self.assertEqual(model.corrected_result[0]["vendor_name"], "ACME")

    def test_location_box_rejects_wrong_length_or_non_finite(self) -> None:
        base = {"corrected_result": {"vendor_name": "ACME"}}
        bad_locations = [
            {"vendor_name": {"page": 1, "box": [1, 2, 3]}},
            {"vendor_name": {"page": 1, "box": [1, 2, float("nan"), 4]}},
            {"vendor_name": {"page": 0, "box": [1, 2, 3, 4]}},
        ]

        for locations in bad_locations:
            with self.subTest(locations=locations):
                with self.assertRaises(ValidationError):
                    CorrectionSaveRequest(**base, field_locations=locations)

    def test_location_box_allows_null_but_rejects_degenerate_boxes(self) -> None:
        # box=None is how unanchored line-item columns round-trip.
        base = {"corrected_result": {"vendor_name": "ACME"}}
        ok_locations = [
            {"line_item_0_qty": {"page": 1, "box": None, "strategy": "qwen_column_header_missing"}},
            {"line_item_0_qty": {"page": 1, "strategy": "qwen_column_header_missing"}},
        ]
        for locations in ok_locations:
            with self.subTest(locations=locations):
                model = CorrectionSaveRequest(**base, field_locations=locations)
                self.assertEqual(len(model.field_locations), 1)

        with self.assertRaises(ValidationError):
            CorrectionSaveRequest(
                **base,
                field_locations={"vendor_name": {"page": 1, "box": [1, 2, 1, 4]}},
            )


class TemplateCreateValidationTests(unittest.TestCase):
    def test_rejects_too_many_fields(self) -> None:
        with self.assertRaises(ValidationError):
            TemplateCreate(
                format_type="single_po_multipage",
                header_fields=[f"f{i}" for i in range(201)],
            )

    def test_rejects_non_string_field(self) -> None:
        with self.assertRaises(ValidationError):
            TemplateCreate(format_type="single_page", header_fields=["po_number", 123])

    def test_strips_fields(self) -> None:
        model = TemplateCreate(
            format_type="single_page",
            header_fields=["  po_number ", "date"],
        )
        self.assertEqual(model.header_fields, ["po_number", "date"])

    def test_rejects_blank_duplicate_and_reserved_fields(self) -> None:
        bad_cases = [
            {"header_fields": [""]},
            {"header_fields": ["po_number", "PO_Number"]},  # duplicate within one list
            {"header_fields": ["line_items"]},
            {"header_fields": ["_internal"]},
        ]
        for payload in bad_cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    TemplateCreate(format_type="single_page", **payload)

    def test_allows_spaces_dots_and_unicode_in_field_names(self) -> None:
        # Field names are free-form labels; the UI may keep spaces/punctuation.
        model = TemplateCreate(
            format_type="single_page",
            header_fields=["Invoice Date", "Núm. Factura", "PO #"],
        )
        self.assertEqual(model.header_fields, ["Invoice Date", "Núm. Factura", "PO #"])

    def test_allows_same_name_in_header_and_line_scopes(self) -> None:
        # A header field and a line-item column with the same name live in
        # different scopes — that is legitimate, not a duplicate.
        model = TemplateCreate(
            format_type="single_page",
            header_fields=["total"],
            line_item_fields=["total"],
        )
        self.assertEqual(model.header_fields, ["total"])
        self.assertEqual(model.line_item_fields, ["total"])


class VendorMappingSaveValidationTests(unittest.TestCase):
    def test_non_numeric_schema_id_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            VendorMappingSave(schema_id="not-a-number")

    def test_too_many_mappings_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            VendorMappingSave(header_map={f"k{i}": "po_number" for i in range(201)})

    def test_valid_mapping_accepted(self) -> None:
        model = VendorMappingSave(
            header_map={" supplier ": " vendor_name "},
            line_map={"qty": "quantity_ordered"},
            schema_id=3,
        )
        self.assertEqual(model.header_map["supplier"], "vendor_name")
        self.assertEqual(model.schema_id, 3)

    def test_mapping_rejects_blank_non_string_extra_and_bad_schema_id(self) -> None:
        bad_payloads = [
            {"header_map": {"": "vendor_name"}},
            {"header_map": {"supplier": ""}},
            {"header_map": {"supplier": 123}},
            {"schema_id": 0},
            {"unknown": "field"},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    VendorMappingSave(**payload)


if __name__ == "__main__":
    unittest.main()
