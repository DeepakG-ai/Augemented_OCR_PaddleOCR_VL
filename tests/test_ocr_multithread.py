import asyncio
import os
import sys
import time
import logging

# Set up logging for our modules to see their debug lines
logging.basicConfig(level=logging.INFO)

# Ensure we can import from backend
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.processor import pdf_to_images
from backend.ocr_runner import run_ocr_on_pages

async def main():
    target_pdf = r"C:\Users\aigroup5\Downloads\PDF Samples\Robert Scott\ROBERT SCOTT 542799.pdf"
    
    if not os.path.exists(target_pdf):
        print(f"Error: Could not find file {target_pdf}")
        # Find some files nearby just in case they're available
        dir_path = os.path.dirname(target_pdf)
        if os.path.exists(dir_path):
            print("Files available in directory:")
            for f in os.listdir(dir_path):
                print(f" - {f}")
        return

    print("="*60)
    print(f"1. Reading PDF into memory...")
    with open(target_pdf, "rb") as f:
        file_bytes = f.read()
        
    print(f"2. Pre-processing: Converting PDF directly to Image Base64s...")
    t0_proc = time.perf_counter()
    pages = await pdf_to_images(file_bytes)
    t1_proc = time.perf_counter()
    print(f"   -> Rendered {len(pages)} pages in {t1_proc - t0_proc:.2f} seconds.")
    
    print(f"3. Running Multi-Threaded PaddleOCR Phase...")
    print(f"   (Watch the terminal output above to see multiple PaddleOCR instances initialize!)")
    
    t0_ocr = time.perf_counter()
    ocr_results = await run_ocr_on_pages(pages)
    t1_ocr = time.perf_counter()
    
    print("="*60)
    print(f"RESULTS FOR {len(pages)} PAGES:")
    print(f" - CPU Extraction Time:  {t1_ocr - t0_ocr:.2f} seconds")
    print(f"   -> Average Time/Page: {(t1_ocr - t0_ocr)/max(1,len(pages)):.2f} seconds")
    
    for page in ocr_results:
        print(f"Page {page['page_number']}: Found {len(page['words'])} words.")

if __name__ == "__main__":
    # Disable tracing locally for the test run so it runs cleanly
    class DummyTrace:
        def __enter__(self): return {}
        def __exit__(self, exc_type, exc_val, exc_tb): pass
        
    try:
        from backend import ocr_runner
        ocr_runner.trace_ocr_page = lambda x: DummyTrace()
    except Exception:
        pass
        
    asyncio.run(main())
