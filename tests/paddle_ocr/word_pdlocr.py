from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

_RUNTIME_DIR = Path(__file__).resolve().parents[2] / ".paddlex_runtime"
_TMP_DIR = _RUNTIME_DIR / "tmp"
_PDX_CACHE_DIR = _RUNTIME_DIR / "pdx_cache"
_MS_CACHE_DIR = _RUNTIME_DIR / "modelscope_cache"
_HF_HOME_DIR = _RUNTIME_DIR / "hf_home"

os.environ.setdefault("HUB_DATASET_ENDPOINT", "https://modelscope.cn/api/v1/datasets")
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
# Avoid permission issues on locked user-profile cache folders.
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(_PDX_CACHE_DIR))
# Avoid temp/cache ACL issues under user profile.
os.environ.setdefault("TMP", str(_TMP_DIR))
os.environ.setdefault("TEMP", str(_TMP_DIR))
os.environ.setdefault("MODELSCOPE_CACHE", str(_MS_CACHE_DIR))
os.environ.setdefault("HF_HOME", str(_HF_HOME_DIR))

for _d in (_TMP_DIR, _PDX_CACHE_DIR, _MS_CACHE_DIR, _HF_HOME_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _to_plain(value: Any) -> Any:
    """Convert numpy-like objects to plain Python recursively."""
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    if hasattr(value, "tolist"):
        return _to_plain(value.tolist())
    return value


def _normalize_box(raw_box: Any) -> list[int]:
    box = _to_plain(raw_box)
    if not isinstance(box, list) or len(box) < 4:
        raise RuntimeError(f"Invalid box format: {box!r}")
    return [int(round(float(box[0]))), int(round(float(box[1]))), int(round(float(box[2]))), int(round(float(box[3])))]


def _unit_text_len(unit: str) -> int:
    return len(re.sub(r"\s+", "", unit))


def _split_units(char_units: Any) -> list[str]:
    """Convert Paddle character/word unit payload into a normalized list[str]."""
    units = _to_plain(char_units)
    if isinstance(units, str):
        return list(units)
    if isinstance(units, list):
        out: list[str] = []
        for u in units:
            s = str(u)
            if s:
                out.append(s)
        return out
    raise RuntimeError(f"Unsupported text_word format: {type(units).__name__}")


def _line_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _distribute_flat_units_to_lines(
    rec_texts: list[str],
    units: list[str],
    unit_boxes: list[list[int]],
) -> tuple[list[list[str]], list[list[list[int]]]]:
    """Split flattened word/char units into per-line groups using rec_texts lengths."""
    per_line_units: list[list[str]] = []
    per_line_boxes: list[list[list[int]]] = []
    ptr = 0
    total_units = len(units)
    for line_text in rec_texts:
        need = _line_char_count(line_text)
        take_units: list[str] = []
        take_boxes: list[list[int]] = []
        got = 0
        while ptr < total_units and got < need:
            u = units[ptr]
            b = unit_boxes[ptr]
            take_units.append(u)
            take_boxes.append(b)
            got += _unit_text_len(u)
            ptr += 1
        if got != need:
            raise RuntimeError(
                "Unable to align text_word/text_word_boxes to rec_texts at line "
                f"'{line_text}'. Needed {need} chars, got {got}."
            )
        per_line_units.append(take_units)
        per_line_boxes.append(take_boxes)

    if ptr != total_units:
        raise RuntimeError(
            f"Unused text_word units remain after line distribution: {total_units - ptr}"
        )
    return per_line_units, per_line_boxes


def _extract_per_line_word_geometry(
    res_obj: Any,
) -> tuple[list[str], list[float], list[list[int]], list[list[str]], list[list[list[int]]]]:
    """
    Extract line-level OCR outputs plus per-line word/char geometry.

    Requires Paddle `return_word_box=True` style payload with `text_word_boxes`.
    """
    rec_texts = [str(t) for t in _to_plain(res_obj["rec_texts"])]
    rec_scores = [float(s) for s in _to_plain(res_obj["rec_scores"])]
    rec_boxes = [_normalize_box(b) for b in _to_plain(res_obj["rec_boxes"])]

    if "text_word_boxes" not in res_obj:
        raise RuntimeError(
            "return_word_box=True did not provide word/char boxes for this sample/version"
        )

    text_word_boxes_raw = _to_plain(res_obj["text_word_boxes"])
    text_word_raw = _to_plain(res_obj.get("text_word"))
    if text_word_raw is None:
        raise RuntimeError(
            "return_word_box=True did not provide text_word for this sample/version"
        )

    # Case A: per-line nested payload
    if (
        isinstance(text_word_boxes_raw, list)
        and len(text_word_boxes_raw) == len(rec_texts)
        and all(isinstance(x, list) for x in text_word_boxes_raw)
    ):
        per_line_boxes: list[list[list[int]]] = []
        per_line_units: list[list[str]] = []
        if not isinstance(text_word_raw, list) or len(text_word_raw) != len(rec_texts):
            raise RuntimeError(
                "text_word/text_word_boxes shape mismatch with rec_texts"
            )
        for line_units_raw, line_boxes_raw in zip(text_word_raw, text_word_boxes_raw):
            units = _split_units(line_units_raw)
            boxes = [_normalize_box(b) for b in line_boxes_raw]
            if len(units) != len(boxes):
                raise RuntimeError(
                    f"text_word/text_word_boxes count mismatch in line payload: {len(units)} vs {len(boxes)}"
                )
            per_line_units.append(units)
            per_line_boxes.append(boxes)
        return rec_texts, rec_scores, rec_boxes, per_line_units, per_line_boxes

    # Case B: flattened payload
    flat_units = _split_units(text_word_raw)
    flat_boxes = [_normalize_box(b) for b in text_word_boxes_raw]
    if len(flat_units) != len(flat_boxes):
        raise RuntimeError(
            f"Flattened text_word/text_word_boxes count mismatch: {len(flat_units)} vs {len(flat_boxes)}"
        )
    per_line_units, per_line_boxes = _distribute_flat_units_to_lines(rec_texts, flat_units, flat_boxes)
    return rec_texts, rec_scores, rec_boxes, per_line_units, per_line_boxes


def _union_boxes(boxes: list[list[int]]) -> list[int]:
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _tokens_from_line(line_text: str) -> list[str]:
    return re.findall(r"\S+", line_text)


def _build_word_mapping(
    line_index: int,
    line_text: str,
    line_score: float,
    line_box: list[int],
    units: list[str],
    unit_boxes: list[list[int]],
) -> list[dict[str, Any]]:
    if len(units) != len(unit_boxes):
        raise RuntimeError(
            f"Unit and box counts differ for line {line_index}: {len(units)} vs {len(unit_boxes)}"
        )

    expected = _line_char_count(line_text)
    actual = sum(_unit_text_len(u) for u in units)
    if expected != actual:
        raise RuntimeError(
            f"text_word alignment mismatch on line {line_index}: expected {expected}, got {actual}"
        )

    out: list[dict[str, Any]] = []
    ptr = 0
    for token in _tokens_from_line(line_text):
        need = _line_char_count(token)
        got = 0
        token_boxes: list[list[int]] = []
        while ptr < len(units) and got < need:
            unit = units[ptr]
            unit_len = _unit_text_len(unit)
            token_boxes.append(unit_boxes[ptr])
            got += unit_len
            ptr += 1
        if got != need:
            raise RuntimeError(
                f"Token alignment mismatch on line {line_index} token '{token}': needed {need}, got {got}"
            )
        out.append(
            {
                "text": token,
                "box": _union_boxes(token_boxes),
                "score": round(float(line_score), 4),
                "line_index": line_index,
                "line_text": line_text,
            }
        )

    if ptr != len(units):
        raise RuntimeError(
            f"Unused text_word units remain on line {line_index}: {len(units) - ptr}"
        )
    return out


def extract_mappings_from_result(result: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    word_mapping: list[dict[str, Any]] = []
    line_mapping: list[dict[str, Any]] = []
    word_texts: list[str] = []
    line_global_index = 0

    for res in result:
        (
            rec_texts,
            rec_scores,
            rec_boxes,
            per_line_units,
            per_line_boxes,
        ) = _extract_per_line_word_geometry(res)

        for line_text, line_score, line_box, units, unit_boxes in zip(
            rec_texts, rec_scores, rec_boxes, per_line_units, per_line_boxes
        ):
            line_mapping.append(
                {
                    "line_index": line_global_index,
                    "text": line_text,
                    "score": round(float(line_score), 4),
                    "box": line_box,
                }
            )
            token_rows = _build_word_mapping(
                line_global_index, line_text, line_score, line_box, units, unit_boxes
            )
            word_mapping.extend(token_rows)
            word_texts.extend(t["text"] for t in token_rows)
            line_global_index += 1

    return word_mapping, line_mapping, word_texts


def run_word_pdlocr(input_path: str, output_dir: str, device: str = "cpu") -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:  # pragma: no cover - import depends on local env
        raise RuntimeError(f"Failed to import PaddleOCR: {exc}") from exc

    ocr = PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        return_word_box=True,
        device=device,
    )

    result = ocr.predict(input_path)
    word_mapping, line_mapping, word_texts = extract_mappings_from_result(result)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "word_mapping.json").write_text(
        json.dumps(word_mapping, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "line_mapping.json").write_text(
        json.dumps(line_mapping, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "word_raw_text.txt").write_text(
        "\n".join(word_texts),
        encoding="utf-8",
    )
    return word_mapping, line_mapping, word_texts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PaddleOCR with return_word_box=True and export word-level boxes.")
    parser.add_argument("--input", required=True, help="Input image path (or page image path).")
    parser.add_argument(
        "--output-dir",
        default=str(Path("tests") / "paddle_ocr" / "output_word"),
        help="Output directory for word_mapping.json, line_mapping.json, word_raw_text.txt",
    )
    parser.add_argument("--device", default="cpu", choices=["cpu", "gpu"], help="Paddle device to use.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    word_mapping, _, _ = run_word_pdlocr(args.input, args.output_dir, device=args.device)
    print(f"Saved {len(word_mapping)} word boxes to {args.output_dir}")


if __name__ == "__main__":
    main()
