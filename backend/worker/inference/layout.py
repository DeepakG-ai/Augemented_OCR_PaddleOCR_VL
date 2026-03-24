import logging
import numpy as np

logger = logging.getLogger(__name__)

# Initialize once at module level — expensive to load
_layout_model = None


def get_layout_model():
    """Lazy-load PP-DocLayoutV3 model."""
    global _layout_model
    if _layout_model is None:
        try:
            from paddleocr import PPStructureV3

            _layout_model = PPStructureV3(layout=True, table=False, ocr=False)
            logger.info("PP-DocLayoutV3 layout model loaded successfully")
        except Exception as e:
            logger.warning(f"Failed to load PP-DocLayoutV3: {e}. Layout analysis will be skipped.")
            _layout_model = "unavailable"
    return _layout_model


def run_layout_analysis(image: np.ndarray) -> list[dict]:
    """
    Returns list of detected regions.
    Each: {"type": "table"|"text"|"title"|"figure"|"seal", "bbox": [x1,y1,x2,y2]}
    """
    model = get_layout_model()
    if model == "unavailable":
        return []

    try:
        results = model(image)
        return [
            {
                "type": r.get("type", "unknown"),
                "bbox": r.get("bbox", []),
            }
            for r in results
            if r.get("type")
        ]
    except Exception as e:
        logger.error(f"Layout analysis failed: {e}")
        return []


def format_layout_summary(layout_map: list[dict]) -> str:
    """Format layout regions as text summary for VLM context."""
    if not layout_map:
        return "No structural regions detected."
    lines = [f"- {r['type']} region at bbox {r['bbox']}" for r in layout_map]
    return "\n".join(lines)
