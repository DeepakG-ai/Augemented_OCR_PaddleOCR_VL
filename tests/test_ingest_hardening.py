import sys
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.main as main
import backend.spatial_memory as spatial_memory
from fastapi import HTTPException

class TestIngestHardening(unittest.IsolatedAsyncioTestCase):
    async def test_submit_ingestion_job_blocks_no_template_without_writing_to_db(self):
        pool = object()
        store = MagicMock()
        
        with patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=None)), \
             patch.object(main.db_mod, "create_document", new=AsyncMock()) as mock_create_doc:
            
            with self.assertRaises(HTTPException) as context:
                await main._submit_ingestion_job(
                    pool,
                    store,
                    file_bytes=b"fake",
                    filename="test.pdf",
                    vendor_id="V1",
                    format_type="",
                    header_fields=[],
                    line_item_fields=[],
                    source_type="ui",
                )
                
            self.assertEqual(context.exception.status_code, 400)
            self.assertEqual(context.exception.detail["reason"], "no_template")
            
            # Verify nothing was written to storage or DB
            store.put_bytes.assert_not_called()
            mock_create_doc.assert_not_called()

    async def test_submit_ingestion_job_blocks_no_fields_without_writing_to_db(self):
        pool = object()
        store = MagicMock()
        
        # Template exists but has no fields
        fake_template = {"id": 1, "format_type": "standard", "header_fields": [], "line_item_fields": []}
        
        with patch.object(main.db_mod, "get_template", new=AsyncMock(return_value=fake_template)), \
             patch.object(main.db_mod, "create_document", new=AsyncMock()) as mock_create_doc:
            
            with self.assertRaises(HTTPException) as context:
                await main._submit_ingestion_job(
                    pool,
                    store,
                    file_bytes=b"fake",
                    filename="test.pdf",
                    vendor_id="V1",
                    format_type="",
                    header_fields=[],
                    line_item_fields=[],
                    source_type="ui",
                )
                
            self.assertEqual(context.exception.status_code, 400)
            self.assertEqual(context.exception.detail["reason"], "no_fields")
            
            store.put_bytes.assert_not_called()
            mock_create_doc.assert_not_called()

    async def test_spatial_memory_apply_filters_stale_fields(self):
        pool = object()
        extraction_id = 1
        result = {"vendor_name": "ACME"}
        field_locations = {}
        
        fake_extraction = {
            "vendor_id": "V1",
            "template_id": 1,
            "header_fields": ["vendor_name"]  # 'old_field' is missing!
        }
        
        # Memory contains 'vendor_name' (valid) and 'old_field' (stale)
        fake_memories = [
            {"field_key": "vendor_name", "page_number": 1, "normalized_box": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}},
            {"field_key": "old_field", "page_number": 1, "normalized_box": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}},
        ]
        
        fake_pages = [{"page_number": 1, "width": 100, "height": 100}]
        fake_ocr = [{"page_number": 1, "words": [{"text": "NEWTEXT", "box": [10,10,20,20]}]}]

        with patch.object(spatial_memory.db_mod, "get_extraction", new=AsyncMock(return_value=fake_extraction)), \
             patch.object(spatial_memory.db_mod, "get_spatial_memory_for_layout", new=AsyncMock(return_value=fake_memories)), \
             patch.object(spatial_memory.db_mod, "get_pages", new=AsyncMock(return_value=fake_pages)), \
             patch.object(spatial_memory, "_load_configured_header_fields", new=AsyncMock(return_value={"vendor_name"})):
            
            updated_result, updated_locations, applied_count = await spatial_memory.apply_to_extraction(
                pool, extraction_id, result, field_locations, fake_ocr
            )
            
            # 'vendor_name' should be updated, 'old_field' should be skipped
            self.assertEqual(applied_count, 1)
            self.assertEqual(updated_result.get("vendor_name"), "NEWTEXT")
            self.assertNotIn("old_field", updated_result)

if __name__ == "__main__":
    unittest.main()
