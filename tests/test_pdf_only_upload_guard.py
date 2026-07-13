"""Locks in the production PDF-only upload guard (`_require_pdf` in backend.main).

Why this matters: per ops directive ahead of the 2026-05-25 production push,
the ingestion pipeline must hard-block .docx, .xlsx, .csv and any other
non-PDF upload. The check has to look at BOTH the extension AND the magic
bytes so a renamed file (e.g. invoice.pdf that is really a .docx) cannot
sneak through and crash a downstream worker.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.main import _require_pdf


_REAL_PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"  # minimal PDF magic
_DOCX_BYTES = b"PK\x03\x04docx-zip-payload"          # ZIP-based, like real .docx
_CSV_BYTES = b"col_a,col_b\n1,2\n"
_XLSX_BYTES = b"PK\x03\x04xlsx-zip-payload"


class RequirePdfTests(unittest.TestCase):

    # ── Happy path ────────────────────────────────────────────────────────
    def test_real_pdf_passes(self):
        _require_pdf("invoice.pdf", _REAL_PDF_BYTES)  # should not raise

    def test_uppercase_extension_passes(self):
        _require_pdf("Invoice.PDF", _REAL_PDF_BYTES)

    # ── Extension blocks ──────────────────────────────────────────────────
    def test_docx_is_blocked(self):
        with self.assertRaises(HTTPException) as ctx:
            _require_pdf("po.docx", _DOCX_BYTES)
        self._assert_415_pdf_only(ctx.exception, "po.docx")

    def test_xlsx_is_blocked(self):
        with self.assertRaises(HTTPException) as ctx:
            _require_pdf("orders.xlsx", _XLSX_BYTES)
        self._assert_415_pdf_only(ctx.exception, "orders.xlsx")

    def test_csv_is_blocked(self):
        with self.assertRaises(HTTPException) as ctx:
            _require_pdf("data.csv", _CSV_BYTES)
        self._assert_415_pdf_only(ctx.exception, "data.csv")

    def test_no_extension_is_blocked(self):
        with self.assertRaises(HTTPException) as ctx:
            _require_pdf("invoice", _REAL_PDF_BYTES)
        self.assertEqual(ctx.exception.status_code, 415)

    def test_empty_filename_is_blocked(self):
        with self.assertRaises(HTTPException) as ctx:
            _require_pdf("", _REAL_PDF_BYTES)
        self.assertEqual(ctx.exception.status_code, 415)

    # ── Magic-byte blocks (defends against renamed files) ─────────────────
    def test_docx_renamed_to_pdf_is_blocked(self):
        """Critical: a .docx renamed invoice.pdf must NOT bypass the guard."""
        with self.assertRaises(HTTPException) as ctx:
            _require_pdf("invoice.pdf", _DOCX_BYTES)
        self.assertEqual(ctx.exception.status_code, 415)

    def test_csv_renamed_to_pdf_is_blocked(self):
        with self.assertRaises(HTTPException) as ctx:
            _require_pdf("invoice.pdf", _CSV_BYTES)
        self.assertEqual(ctx.exception.status_code, 415)

    def test_empty_bytes_with_pdf_name_is_blocked(self):
        with self.assertRaises(HTTPException) as ctx:
            _require_pdf("invoice.pdf", b"")
        self.assertEqual(ctx.exception.status_code, 415)

    # ── Helpers ───────────────────────────────────────────────────────────
    def _assert_415_pdf_only(self, exc: HTTPException, expected_name: str) -> None:
        self.assertEqual(exc.status_code, 415)
        self.assertIsInstance(exc.detail, dict)
        self.assertEqual(exc.detail.get("code"), "UNSUPPORTED_FILE_TYPE")
        self.assertIn(".pdf", exc.detail.get("allowed_extensions", []))
        self.assertIn(expected_name, exc.detail.get("message", ""))


if __name__ == "__main__":
    unittest.main()
