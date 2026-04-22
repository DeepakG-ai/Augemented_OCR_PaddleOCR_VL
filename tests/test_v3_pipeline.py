"""Integration test for Qwen v3 anchor bbox pipeline."""
import sys
sys.path.insert(0, "qwen_backend")

from extractor import PROMPT_VERSION, build_user_message, build_system_prompt, merge_results
from qwen_bbox_parser import build_field_locations
import inspect

# Test 1
assert PROMPT_VERSION == "v3"
print("1. PROMPT_VERSION = v3 OK")

# Test 2
msg = build_user_message(["vendor", "po_number"], ["qty", "item"], 1, 2)
assert '"fields"' in msg
assert '"boxes"' in msg
assert "field LABEL" in msg
print("2. User message has {fields, boxes} template OK")

# Test 3
sp = build_system_prompt(["vendor"], ["qty"], None, [], "single_page")
assert "bounding boxes" in sp.lower()
assert "Do not return bounding boxes for field values" in sp
print("3. System prompt has bbox rules OK")

# Test 4
page_results = [
    {"_page": 1, "fields": {"vendor": "ACME", "line_items": [{"qty": 5}]}, "boxes": {"vendor": [10,20,100,40], "qty": [200,50,280,70]}},
    {"_page": 2, "fields": {"vendor": "ACME", "line_items": [{"qty": 10}]}, "boxes": {"qty": [200,55,280,75]}},
]
merged = merge_results(page_results, ["vendor"], ["qty"])
assert merged["_format"] == "v3"
assert merged["fields"]["vendor"] == "ACME"
assert len(merged["fields"]["line_items"]) == 2
assert len(merged["boxes"]) == 2
print("4. merge_results v3 format OK")

# Test 5
locs = build_field_locations(merged, page_results)
assert locs["vendor"]["strategy"] == "qwen_anchor"
assert locs["line_item_0_qty"]["strategy"] == "qwen_column_header"
assert locs["line_item_1_qty"]["strategy"] == "qwen_column_header"
assert locs["line_item_1_qty"]["page"] == 2
assert locs["line_item_1_qty"]["box"] == [200, 55, 280, 75]
print("5. qwen_bbox_parser multi-page OK")

# Test 6
legacy_pages = [{"_page": 1, "vendor": "OLD", "line_items": [{"qty": 1}]}]
legacy_merged = merge_results(legacy_pages, ["vendor"], ["qty"])
assert "fields" not in legacy_merged
assert legacy_merged["vendor"] == "OLD"
print("6. Legacy merge fallback OK")

# Test 7
src = inspect.getsource(sys.modules["extractor"].extract_document)
assert "PARALLEL_BATCH = 1" in src
print("7. PARALLEL_BATCH = 1 (sequential) OK")

print()
print("=== ALL 7 CHECKS PASSED ===")
