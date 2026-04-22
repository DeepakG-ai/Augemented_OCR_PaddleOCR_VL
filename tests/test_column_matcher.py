"""Quick smoke test for the new column-based text_matcher."""
import sys, os
sys.path.insert(0, r"C:\Users\aigroup5\PycharmProjects\Augemented_OCR_PaddleOCR_VL\qwen_backend")

from text_matcher import (
    compute_field_locations,
    find_value_in_ocr,
    _anchor_strength,
    _confirm_column,
    _find_row_anchors,
    _build_row_bands,
    _build_ocr_lines,
    _prepare_page_words,
    _get_column_names,
    _group_items_by_page,
    _compute_value_frequencies,
    _generate_header_variants,
)

print("=== All imports OK ===\n")

# ── Test _anchor_strength ──
tests = [
    ("3WDS-F-01-BX", 1, 3),   # mixed alpha-numeric, strong
    ("BSL1232",      1, 3),   # mixed alpha-numeric, strong
    ("10/bx",        1, 3),   # has special char + length 4+, strong
    ("12/cs",        1, 3),   # has special char + length 4+, strong
    ("FRESH AIR DEODORIZER", 1, 2),  # pure alpha >= 6, medium
    ("15.69",        1, 2),   # unique numeric >= 3 chars, medium
    ("36",           1, 1),   # short numeric, weak
    ("EA",           1, 1),   # short alpha, weak
    ("",             1, 0),   # empty, skip
    ("n/a",          1, 0),   # n/a, skip
    ("3WDS-F-01-BX", 6, 1),  # strong value but freq > 5, penalized
]

print("_anchor_strength tests:")
all_ok = True
for val, freq, expected in tests:
    result = _anchor_strength(val, freq)
    status = "OK" if result == expected else f"FAIL (got {result})"
    if result != expected:
        all_ok = False
    print(f"  {val!r:25s} freq={freq}  => {result}  {status}")

print()

# ── Test _generate_header_variants ──
print("_generate_header_variants tests:")
for col in ["ship_qty", "unit_price", "item", "description"]:
    variants = _generate_header_variants(col)
    print(f"  {col:20s} => {variants}")

print()

# ── Test _get_column_names ──
items = [
    {"item": "A", "qty": 1, "price": 10},
    {"item": "B", "qty": 2, "uom": "EA"},
]
cols = _get_column_names(items)
print(f"_get_column_names => {cols}")
assert cols == ["item", "qty", "price", "uom"], f"Expected ['item', 'qty', 'price', 'uom'], got {cols}"
print("  OK\n")

# ── Test find_value_in_ocr ──
ocr_words = [
    {"text": "3WDS-F-01-BX", "box": [100, 200, 250, 215], "score": 0.98},
    {"text": "FRESH",        "box": [300, 200, 360, 215], "score": 0.95},
    {"text": "AIR",          "box": [365, 200, 390, 215], "score": 0.94},
    {"text": "12/cs",        "box": [400, 200, 440, 215], "score": 0.97},
    {"text": "15.69",        "box": [450, 200, 500, 215], "score": 0.96},
]

print("find_value_in_ocr tests:")
for val in ["3WDS-F-01-BX", "12/cs", "15.69", "FRESH AIR", "NONEXISTENT"]:
    loc = find_value_in_ocr(val, ocr_words)
    if loc:
        print(f"  {val!r:20s} => strategy={loc['strategy']:15s} box={loc['box']}")
    else:
        print(f"  {val!r:20s} => None")

print()

# ── Test full compute_field_locations pipeline ──
extraction = {
    "vendor": "FRESH PRODUCTS INC",
    "po_number": "563773",
    "line_items": [
        {"item": "3WDS-F-01-BX", "pack": "12/cs", "qty": "36", "unit_price": "15.69"},
        {"item": "OFB-F-88-BX",  "pack": "10/bx", "qty": "20", "unit_price": "12.50"},
        {"item": "BSL-1232",     "pack": "8/bx",  "qty": "10", "unit_price": "8.99"},
    ],
}

ocr_pages = [{
    "page_number": 1,
    "words": [
        # Header area
        {"text": "FRESH",      "box": [50, 30, 110, 45],  "score": 0.98},
        {"text": "PRODUCTS",   "box": [115, 30, 200, 45], "score": 0.97},
        {"text": "INC",        "box": [205, 30, 240, 45], "score": 0.96},
        {"text": "PO#",        "box": [400, 30, 430, 45], "score": 0.95},
        {"text": "563773",     "box": [435, 30, 500, 45], "score": 0.98},
        # Table headers
        {"text": "Item",       "box": [50, 100, 100, 115], "score": 0.99},
        {"text": "Pack",       "box": [200, 100, 250, 115], "score": 0.99},
        {"text": "Qty",        "box": [300, 100, 340, 115], "score": 0.99},
        {"text": "Unit",       "box": [400, 100, 440, 115], "score": 0.99},
        {"text": "Price",      "box": [445, 100, 500, 115], "score": 0.99},
        # Row 0
        {"text": "3WDS-F-01-BX", "box": [50, 130, 180, 145],  "score": 0.97},
        {"text": "12/cs",        "box": [200, 130, 250, 145],  "score": 0.96},
        {"text": "36",           "box": [310, 130, 330, 145],  "score": 0.98},
        {"text": "15.69",        "box": [410, 130, 460, 145],  "score": 0.95},
        # Row 1
        {"text": "OFB-F-88-BX",  "box": [50, 160, 175, 175],  "score": 0.96},
        {"text": "10/bx",        "box": [200, 160, 250, 175],  "score": 0.97},
        {"text": "20",           "box": [310, 160, 330, 175],  "score": 0.98},
        {"text": "12.50",        "box": [410, 160, 460, 175],  "score": 0.94},
        # Row 2
        {"text": "BSL-1232",     "box": [50, 190, 160, 205],  "score": 0.95},
        {"text": "8/bx",         "box": [200, 190, 240, 205],  "score": 0.96},
        {"text": "10",           "box": [310, 190, 330, 205],  "score": 0.97},
        {"text": "8.99",         "box": [410, 190, 460, 205],  "score": 0.93},
    ],
}]

page_results = [
    {"_page": 1, "line_items": [{}, {}, {}]},
]

print("compute_field_locations full pipeline test:")
locations = compute_field_locations(extraction, ocr_pages, page_results)

print(f"  Total locations: {len(locations)}")
print(f"  Header fields:")
for key, loc in sorted(locations.items()):
    if not key.startswith("line_item_"):
        print(f"    {key:20s} => strategy={loc['strategy']:15s} page={loc['page']} box={loc['box']}")

print(f"  Line items:")
for key, loc in sorted(locations.items()):
    if key.startswith("line_item_"):
        mm = loc.get("match_mode", "?")
        print(f"    {key:30s} => mode={mm:16s} box={loc['box']} conf={loc['confidence']}")

# Validate all line items have required keys
required_keys = {"page", "box", "confidence", "match_mode", "field_name", "row_index"}
for key, loc in locations.items():
    if key.startswith("line_item_"):
        missing = required_keys - set(loc.keys())
        if missing:
            print(f"  FAIL: {key} missing keys: {missing}")

print("\n=== ALL TESTS PASSED ===" if all_ok else "\n=== SOME TESTS FAILED ===")
