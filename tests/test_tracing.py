"""
test_tracing.py — Verify full pipeline tracing works end-to-end.

Simulates the complete document extraction workflow and sends
hierarchical traces to Phoenix, just like a real extraction would:

  document_extraction (root)
  ├── file_upload
  ├── pdf_to_images
  ├── prompt_building
  ├── page_1_extraction
  │   ├── build_user_message
  │   └── llm.chat page_1/2
  ├── page_2_extraction
  │   ├── build_user_message
  │   └── llm.chat page_2/2
  ├── merge_results
  ├── paddle_ocr
  │   ├── ocr_page_1
  │   └── ocr_page_2
  ├── text_matching
  ├── db_persist (OCR data)
  └── db_persist (final result)

Run:  python tests/test_tracing.py
View: http://localhost:6006  →  project "augmented_ocr"
"""
import sys
import os
import time

# Add qwen_backend to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "qwen_backend"))

os.environ["PHOENIX_ENABLED"] = "true"
os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = "http://localhost:4317"

from phoenix_tracing import (
    setup_phoenix,
    trace_extraction_pipeline,
    trace_file_upload,
    trace_pdf_rendering,
    trace_prompt_building,
    trace_page_extraction,
    trace_build_user_message,
    trace_llm_call,
    trace_merge_results,
    trace_paddle_ocr,
    trace_ocr_page,
    trace_text_matching,
    trace_db_persist,
)


def simulate_full_pipeline():
    """Simulate the full document extraction pipeline with tracing."""

    print("=" * 60)
    print("  Phoenix Pipeline Tracing Test")
    print("=" * 60)

    setup_phoenix("augmented_ocr")

    extraction_id = 999
    vendor_id = "TEST_VENDOR"
    filename = "test_invoice_2pages.pdf"
    total_pages = 2
    format_type = "single_po_multipage"
    header_fields = ["po_number", "order_date", "vendor_name", "bill_to"]
    line_item_fields = ["item", "qty", "unit_price", "amount"]

    # ── ROOT SPAN: document_extraction ──
    with trace_extraction_pipeline(
        extraction_id, vendor_id, filename, total_pages,
        format_type, header_fields, line_item_fields,
    ) as pipeline:

        # ── Step 1: File upload ──
        with trace_file_upload(filename, 1_500_000, "pdf"):
            time.sleep(0.01)  # Simulate IO
            print("  ✓ file_upload")

        # ── Step 2: PDF to images ──
        with trace_pdf_rendering(filename) as render_ctx:
            time.sleep(0.05)  # Simulate PDF rendering
            render_ctx["pages_rendered"] = 2
            print("  ✓ pdf_to_images (2 pages)")

        # ── Step 3: Prompt building ──
        system_prompt = (
            "You are a highly accurate document data extraction assistant.\n"
            "Extract ONLY what is explicitly visible in the document image.\n"
            "Never guess or fabricate data. If a field is not visible, set it to null."
        )
        with trace_prompt_building(vendor_id) as prompt_ctx:
            prompt_ctx["cache_hit"] = "template_db"
            prompt_ctx["prompt_hash"] = "abc123def456"
            prompt_ctx["system_prompt"] = system_prompt
            print("  ✓ prompt_building (cache: template_db)")

        # ── Step 4: Page extractions ──
        page_results = []

        for page_num in range(1, total_pages + 1):
            with trace_page_extraction(page_num, total_pages) as page_ctx:

                # Build user message
                user_msg = (
                    f"Extract the header fields AND all visible line item rows "
                    f"from this purchase order page (page {page_num} of {total_pages})."
                )
                with trace_build_user_message(page_num, total_pages) as msg_ctx:
                    msg_ctx["user_message"] = user_msg
                    print(f"    ✓ build_user_message (page {page_num})")

                # LLM call
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/SIMULATED"}},
                        {"type": "text", "text": user_msg},
                    ]},
                ]

                with trace_llm_call("qwen3vl", messages, temperature=0.7,
                                    page_num=page_num, total_pages=total_pages) as llm_ctx:
                    time.sleep(0.02)  # Simulate LLM latency

                    if page_num == 1:
                        response = '{"po_number": "P1416576", "order_date": "03/12/2026", "vendor_name": "FRESH PRODUCTS, INC.", "bill_to": "SYSCO FOOD SERVICES", "line_items": [{"item": "Lettuce Iceberg", "qty": 10, "unit_price": 12.50, "amount": 125.00}, {"item": "Tomato Roma", "qty": 5, "unit_price": 8.99, "amount": 44.95}]}'
                        usage = {"prompt_tokens": 1200, "completion_tokens": 350, "total_tokens": 1550}
                    else:
                        response = '{"line_items": [{"item": "Cucumber English", "qty": 8, "unit_price": 6.75, "amount": 54.00}, {"item": "Pepper Bell Red", "qty": 12, "unit_price": 4.50, "amount": 54.00}]}'
                        usage = {"prompt_tokens": 1100, "completion_tokens": 200, "total_tokens": 1300}

                    llm_ctx["response"] = response
                    llm_ctx["usage"] = usage
                    print(f"    ✓ llm.chat page_{page_num}/{total_pages} ({usage['total_tokens']} tokens)")

                page_ctx["result"] = {"_page": page_num, "line_items": [{"item": "test"}]}
                page_results.append(page_ctx["result"])

        # ── Step 5: Merge results ──
        with trace_merge_results(total_pages, format_type) as merge_ctx:
            time.sleep(0.005)
            merge_ctx["merged_line_items"] = 4
            merge_ctx["merged_fields"] = 4
            print("  ✓ merge_results (4 header fields, 4 line items)")

        # ── Step 6: PaddleOCR ──
        with trace_paddle_ocr(total_pages) as ocr_ctx:
            for page_num in range(1, total_pages + 1):
                with trace_ocr_page(page_num) as ocr_page_ctx:
                    time.sleep(0.03)
                    words = 45 + page_num * 10
                    ocr_page_ctx["words_detected"] = words
                    print(f"    ✓ ocr_page_{page_num} ({words} words)")

            ocr_ctx["pages_processed"] = total_pages
            ocr_ctx["total_words"] = 110
            print("  ✓ paddle_ocr (110 total words)")

        # ── Step 7: Text matching ──
        with trace_text_matching(4) as match_ctx:
            time.sleep(0.01)
            match_ctx["matched"] = 3
            match_ctx["missed"] = 1
            match_ctx["strategies"] = {"exact": 2, "contains": 1}
            print("  ✓ text_matching (3/4 matched)")

        # ── Step 8: DB persist (OCR) ──
        with trace_db_persist(extraction_id, "done"):
            time.sleep(0.005)
            print("  ✓ db_persist (OCR data)")

        # ── Step 9: DB persist (final result) ──
        with trace_db_persist(extraction_id, "done"):
            time.sleep(0.005)
            print("  ✓ db_persist (final result + cache)")

        # Set pipeline outcome
        pipeline["status"] = "done"
        pipeline["result"] = {
            "po_number": "P1416576",
            "order_date": "03/12/2026",
            "vendor_name": "FRESH PRODUCTS, INC.",
            "bill_to": "SYSCO FOOD SERVICES",
            "line_items": [
                {"item": "Lettuce Iceberg", "qty": 10, "unit_price": 12.50, "amount": 125.00},
                {"item": "Tomato Roma", "qty": 5, "unit_price": 8.99, "amount": 44.95},
                {"item": "Cucumber English", "qty": 8, "unit_price": 6.75, "amount": 54.00},
                {"item": "Pepper Bell Red", "qty": 12, "unit_price": 4.50, "amount": 54.00},
            ],
        }

    print()
    print("=" * 60)
    print("  ✅ SUCCESS — Full pipeline trace sent to Phoenix!")
    print("  📊 Open http://localhost:6006 → project 'augmented_ocr'")
    print("  👀 Click on 'document_extraction' to see the full tree")
    print("=" * 60)


if __name__ == "__main__":
    simulate_full_pipeline()
