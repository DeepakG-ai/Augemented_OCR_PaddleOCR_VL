from pathlib import Path
from ocr_pipeline import run_pdf_ocr, save_ocr_outputs
import asyncio
import time

pdf_bytes = open(r"C:\Users\aigroup5\Downloads\PDF Samples\RJ Schinner\RJ SCHINNER 563773.pdf", "rb").read()

async def main():
    t0 = time.perf_counter()

    pages = await run_pdf_ocr(
        pdf_bytes,
        out_dir=Path("output"),
        n_workers=4,
    )

    t1 = time.perf_counter()

    save_ocr_outputs(pages, Path("output"))

    t2 = time.perf_counter()

    print(f" OCR time       : {t1 - t0:.2f} sec")
    print(f" Save time      : {t2 - t1:.2f} sec")
    print(f" Total time     : {t2 - t0:.2f} sec")
    print(f" Pages          : {len(pages)}")
    print(f" Avg/page       : {(t1 - t0)/len(pages):.2f} sec")

    for page in pages:
        print(page["page_number"], page["rec_texts"])

asyncio.run(main())