import os
import sys
import glob
import asyncio
import json
import base64
import cv2
import numpy as np

# Ensure qwen_backend modules can be imported
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from qwen_backend.processor import pdf_to_images
from qwen_backend.ocr_runner import run_ocr_on_pages

INPUT_DIR = r"C:\Users\aigroup5\Downloads\PDF Samples\input"
OUTPUT_DIR = r"C:\Users\aigroup5\PycharmProjects\Augemented_OCR_PaddleOCR_VL\pdle_output"

async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    pdf_files = glob.glob(os.path.join(INPUT_DIR, "*.pdf"))
    if not pdf_files:
        print(f"No PDFs found in {INPUT_DIR}")
        return

    print(f"Found {len(pdf_files)} PDFs in input directory.")

    for pdf_path in pdf_files:
        filename = os.path.basename(pdf_path)
        print(f"\n--- Processing {filename} ---")
        
        with open(pdf_path, 'rb') as f:
            pdf_bytes = f.read()

        # Phase 1: Convert to images using processor.py engine
        print("Converting PDF to base64 images...")
        pages = await pdf_to_images(pdf_bytes)

        # Phase 2: Run PaddleOCR using ocr_runner.py engine
        print(f"Running PaddleOCR on {len(pages)} pages...")
        ocr_results = await run_ocr_on_pages(pages)

        # Phase 3: Save analysis results
        for idx, (page, ocr) in enumerate(zip(pages, ocr_results)):
            page_num = page["page_number"]
            base_name = f"{os.path.splitext(filename)[0]}_page_{page_num}"
            
            # Save raw image
            img_bytes = base64.b64decode(page["image_b64"])
            img_path = os.path.join(OUTPUT_DIR, f"{base_name}.jpg")
            with open(img_path, 'wb') as img_f:
                img_f.write(img_bytes)

            # Save OCR result as raw JSON for inspection
            json_path = os.path.join(OUTPUT_DIR, f"{base_name}.json")
            with open(json_path, 'w', encoding='utf-8') as json_f:
                json.dump(ocr["words"], json_f, indent=2, ensure_ascii=False)

            # Bonus: Draw bounding boxes directly on the image for human visual analysis!
            img_array = np.frombuffer(img_bytes, dtype=np.uint8)
            img_cv = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

            for word in ocr["words"]:
                box = word["box"] # [x0, y0, x1, y1]
                text = word["text"]
                # Draw rectangle (Green)
                cv2.rectangle(img_cv, (box[0], box[1]), (box[2], box[3]), (0, 255, 0), 2)
                # Draw text above the box (Blue)
                cv2.putText(img_cv, text, (box[0], max(0, box[1] - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)

            vis_path = os.path.join(OUTPUT_DIR, f"{base_name}_vis.jpg")
            cv2.imwrite(vis_path, img_cv)

            print(f"  Saved analysis files for page {page_num}:")
            print(f"    - Raw Image: {img_path}")
            print(f"    - JSON Bounding Boxes: {json_path}")
            print(f"    - Visualised Bounding Boxes: {vis_path}")

if __name__ == "__main__":
    asyncio.run(main())
