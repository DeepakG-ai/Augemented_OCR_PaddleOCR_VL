import os, json
from pathlib import Path

os.environ.setdefault('HUB_DATASET_ENDPOINT', 'https://modelscope.cn/api/v1/datasets')
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

from paddleocr import PaddleOCR

IMAGE_PATH = r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-24 172803.png"
OUT_DIR = Path("output")
OUT_DIR.mkdir(exist_ok=True)

ocr = PaddleOCR(
    text_detection_model_name   = "PP-OCRv5_mobile_det",
    text_recognition_model_name = "PP-OCRv5_mobile_rec",
    use_doc_orientation_classify = False,
    use_doc_unwarping            = False,
    use_textline_orientation     = False,
    device        = "cpu",
    enable_mkldnn = False,
)

result = ocr.predict(IMAGE_PATH)

texts, boxes, mapping = [], [], []

for res in result:
    res.save_to_json(str(OUT_DIR))   # ← paddle raw JSON to disk
    res.save_to_img(str(OUT_DIR))    # ← annotated image to disk

    rec_texts  = res["rec_texts"]
    rec_scores = res["rec_scores"]
    dt_polys   = res["dt_polys"]
    rec_boxes  = res["rec_boxes"]

    for text, score, poly, box in zip(rec_texts, rec_scores, dt_polys, rec_boxes):
        texts.append(text)
        boxes.append(poly.tolist())
        mapping.append({
            "text"  : text,
            "score" : round(float(score), 4),
            "poly"  : poly.tolist(),
            "box"   : box.tolist(),
        })

(OUT_DIR / "raw_text.txt").write_text("\n".join(texts), encoding="utf-8")
(OUT_DIR / "bboxes.json").write_text(json.dumps(boxes, indent=2), encoding="utf-8")
(OUT_DIR / "mapping.json").write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")

print(f"✅ {len(texts)} regions")
for m in mapping:
    print(f"  {m['score']:.2f}  {m['text']}")