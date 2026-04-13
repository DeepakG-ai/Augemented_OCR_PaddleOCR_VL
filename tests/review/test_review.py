"""
test_review.py -- Test PyMuPDF text extraction with bounding box coordinates.

Shows what PyMuPDF returns page-by-page:
  - Every text span with exact pixel coordinates
  - Then demonstrates text matching: given an extracted value, find WHERE on the page it lives.

Usage:
    cd C:/Users/aigroup5/PycharmProjects/Augemented_OCR_PaddleOCR_VL
    python -m tests.review.test_review
"""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor

import fitz  # PyMuPDF

_executor = ThreadPoolExecutor(max_workers=2)

PDF_PATH = r"C:\Users\aigroup5\Downloads\PDF Samples\canada metal\Canada Metal - FA595213 (APV 184468).pdf"


# ── 1. Extract text + bounding boxes from a single page ─────────────────────

def _extract_page_text(page: fitz.Page, page_number: int) -> dict:
    """
    Extract all text spans with bounding boxes from one PDF page.
    
    Returns:
        {
            "page_number": 1,
            "page_width": 612.0,     # points (PDF native units)
            "page_height": 792.0,
            "total_spans": 85,
            "spans": [
                {
                    "text": "PURCHASE ORDER",
                    "x0": 340.5,   "y0": 72.3,    # top-left corner
                    "x1": 510.2,   "y1": 88.7,    # bottom-right corner
                    "font": "Helvetica-Bold",
                    "size": 14.0,
                },
                ...
            ]
        }
    """
    rect = page.rect
    text_dict = page.get_text("dict")  # Full structured text with positions

    spans = []
    for block in text_dict["blocks"]:
        if block["type"] != 0:  # 0 = text block, 1 = image block
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                text = span["text"].strip()
                if not text:  # skip empty spans
                    continue
                spans.append({
                    "text": text,
                    "x0": round(span["bbox"][0], 2),
                    "y0": round(span["bbox"][1], 2),
                    "x1": round(span["bbox"][2], 2),
                    "y1": round(span["bbox"][3], 2),
                    "font": span.get("font", ""),
                    "size": round(span.get("size", 0), 1),
                })

    return {
        "page_number": page_number,
        "page_width": round(rect.width, 2),
        "page_height": round(rect.height, 2),
        "total_spans": len(spans),
        "spans": spans,
    }


# ── 2. Process full PDF page-by-page (sync, offloaded to executor) ──────────

def _extract_all_pages_sync(file_bytes: bytes) -> list[dict]:
    """Process every page of the PDF and extract text + coordinates."""
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    results = []
    try:
        for i, page in enumerate(doc):
            page_data = _extract_page_text(page, page_number=i + 1)
            results.append(page_data)
    finally:
        doc.close()
    return results


async def extract_text_with_positions(file_bytes: bytes) -> list[dict]:
    """Async wrapper — offloads to threadpool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor, _extract_all_pages_sync, file_bytes
    )


# ── 3. Text matching: find WHERE an extracted value lives on the page ───────

def find_value_location(
    value: str,
    page_spans: list[dict],
    page_width: float,
    page_height: float,
) -> dict | None:
    """
    Given an extracted field value (e.g. "Robert Scott"), search through
    the page spans to find the bounding box where it appears.

    Returns coordinates as PERCENTAGES of page dimensions (zoom-independent):
        {"x": 12.5, "y": 8.3, "w": 18.2, "h": 2.1}
    
    Returns None if not found.
    """
    if not value or not page_spans:
        return None

    target = str(value).strip().lower()
    if not target:
        return None

    # Strategy 1: Single span contains the value
    for span in page_spans:
        if target in span["text"].lower():
            return {
                "x": round(span["x0"] / page_width * 100, 2),
                "y": round(span["y0"] / page_height * 100, 2),
                "w": round((span["x1"] - span["x0"]) / page_width * 100, 2),
                "h": round((span["y1"] - span["y0"]) / page_height * 100, 2),
                "matched_text": span["text"],
                "strategy": "single_span",
            }

    # Strategy 2: Multi-span match — combine consecutive spans
    for i in range(len(page_spans)):
        combined_text = ""
        for j in range(i, min(i + 15, len(page_spans))):
            sep = " " if combined_text else ""
            combined_text += sep + page_spans[j]["text"]

            if target in combined_text.lower():
                # Found! Build bounding box from span[i] to span[j]
                x0 = min(page_spans[k]["x0"] for k in range(i, j + 1))
                y0 = min(page_spans[k]["y0"] for k in range(i, j + 1))
                x1 = max(page_spans[k]["x1"] for k in range(i, j + 1))
                y1 = max(page_spans[k]["y1"] for k in range(i, j + 1))

                return {
                    "x": round(x0 / page_width * 100, 2),
                    "y": round(y0 / page_height * 100, 2),
                    "w": round((x1 - x0) / page_width * 100, 2),
                    "h": round((y1 - y0) / page_height * 100, 2),
                    "matched_text": combined_text.strip(),
                    "strategy": "multi_span",
                }

    return None


def compute_field_locations(
    extraction_result: dict,
    text_data: list[dict],
) -> dict:
    """
    Given the full extraction JSON and text data from all pages,
    find where each header field value appears on the document.

    Returns:
        {
            "vendor_name": {"page": 1, "x": 12.5, "y": 8.3, "w": 18.2, "h": 2.1, ...},
            "po_number": {"page": 1, "x": 45.0, "y": 15.6, "w": 12.8, "h": 2.0, ...},
            ...
        }
    """
    locations = {}

    for field_name, field_value in extraction_result.items():
        if field_name == "line_items" or field_value is None:
            continue

        # Convert to string for matching
        val_str = str(field_value).strip()
        if not val_str:
            continue

        # Search through each page
        for page_data in text_data:
            location = find_value_location(
                val_str,
                page_data["spans"],
                page_data["page_width"],
                page_data["page_height"],
            )
            if location:
                location["page"] = page_data["page_number"]
                locations[field_name] = location
                break  # Found on this page, stop searching

    return locations


# ── 4. Main test ────────────────────────────────────────────────────────────

async def main():
    if not os.path.exists(PDF_PATH):
        print(f"ERROR: PDF not found at {PDF_PATH}")
        return

    print(f"{'=' * 80}")
    print(f"  PyMuPDF Text Extraction Test")
    print(f"  PDF: {os.path.basename(PDF_PATH)}")
    print(f"{'=' * 80}\n")

    # Read file
    with open(PDF_PATH, "rb") as f:
        file_bytes = f.read()

    print(f"File size: {len(file_bytes):,} bytes\n")

    # ── Step 1: Extract text + coordinates page by page ──
    print("-" * 60)
    print("  STEP 1: PyMuPDF Text Extraction (page by page)")
    print("-" * 60)

    text_data = await extract_text_with_positions(file_bytes)

    for page_data in text_data:
        pn = page_data["page_number"]
        pw = page_data["page_width"]
        ph = page_data["page_height"]
        total = page_data["total_spans"]

        print(f"\n  PAGE {pn}")
        print(f"     Dimensions: {pw} x {ph} points")
        print(f"     Total text spans: {total}")
        print(f"     {'-' * 50}")

        # Print first 20 spans as sample
        for i, span in enumerate(page_data["spans"][:30]):
            text_preview = span["text"][:50]
            print(
                f"     [{i:3d}] \"{text_preview}\""
                f"  @ ({span['x0']:.0f}, {span['y0']:.0f}) -> ({span['x1']:.0f}, {span['y1']:.0f})"
                f"  [{span['font']}, {span['size']}pt]"
            )
        if total > 30:
            print(f"     ... and {total - 30} more spans")

    # ── Step 2: Simulate extraction result and match ──
    print(f"\n\n{'-' * 60}")
    print("  STEP 2: Text Matching Demo")
    print("  (Simulating Qwen VL extraction result)")
    print("-" * 60)

    # Simulated extraction result -- these are field values that Qwen would return
    # Using values that should appear in an RJ Schinner purchase order
    simulated_extraction = {
        "vendor_name": "RJ SCHINNER",
        "po_number": "563773",
        "purchase_order_no": "563773",
    }

    # Also try to find some values by scanning the actual text
    # Let's grab some real values from the PDF to test matching
    field_locations = {}

    if text_data and text_data[0]["spans"]:
        first_page_spans = text_data[0]["spans"]
        print(f"\n  Trying to match simulated extraction values...\n")

        for field, value in simulated_extraction.items():
            print(f"  Searching for {field} = \"{value}\"")

        field_locations = compute_field_locations(simulated_extraction, text_data)

        print(f"\n  {'-' * 50}")
        print(f"  MATCH RESULTS:")
        print(f"  {'-' * 50}")

        for field, value in simulated_extraction.items():
            loc = field_locations.get(field)
            if loc:
                print(
                    f"  [FOUND] {field} = \"{value}\"\n"
                    f"     Found on page {loc['page']} at ({loc['x']:.1f}%, {loc['y']:.1f}%)"
                    f" size ({loc['w']:.1f}% x {loc['h']:.1f}%)\n"
                    f"     Matched text: \"{loc['matched_text']}\"\n"
                    f"     Strategy: {loc['strategy']}"
                )
            else:
                print(f"  [MISS]  {field} = \"{value}\" -- NOT FOUND on any page")

    # ── Step 3: Full output as JSON ──
    print(f"\n\n{'-' * 60}")
    print("  STEP 3: Full JSON Output (field_locations)")
    print("-" * 60)

    output = {
        "pdf_file": os.path.basename(PDF_PATH),
        "total_pages": len(text_data),
        "pages_summary": [
            {
                "page_number": p["page_number"],
                "page_width": p["page_width"],
                "page_height": p["page_height"],
                "total_spans": p["total_spans"],
            }
            for p in text_data
        ],
        "simulated_extraction": simulated_extraction,
        "field_locations": field_locations if text_data else {},
    }

    print(json.dumps(output, indent=2))

    # ── Step 4: Save full text data to file for inspection ──
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "text_extraction_output.json"
    )
    full_output = {
        "pdf_file": os.path.basename(PDF_PATH),
        "total_pages": len(text_data),
        "text_data": text_data,
        "field_locations": field_locations if text_data else {},
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(full_output, f, indent=2, ensure_ascii=False)

    print(f"\n  Full text extraction saved to:\n     {output_path}")
    print(f"\n{'=' * 80}")
    print(f"  Done!")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    asyncio.run(main())
