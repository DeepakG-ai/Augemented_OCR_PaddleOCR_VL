"""
text_matcher.py -- Match extracted JSON values to PaddleOCR boxes.

Design goals:
1) Avoid giant "block" boxes for multi-line fields (vendor/bill_to/ship_to).
2) Support matching token values inside long OCR chunks (item codes in row text).
3) Keep output contract stable for the Review UI.
"""
from __future__ import annotations

from collections import defaultdict
import logging
import re
import time
from typing import Any

from rapidfuzz import fuzz, process

logger = logging.getLogger("text_matcher")

_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\S+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_ROW_STRONG_FIELDS = {"item", "no", "part_number", "item_number", "item_code", "supplier_code"}
_ROW_MEDIUM_FIELDS = {
    "qty",
    "ship_qty",
    "order_qty",
    "req_quantity",
    "ordered_qty",
    "shipped_qty",
    "uom",
    "um",
    "pack",
}
_ROW_WEAK_FIELDS = {
    "unit_cost",
    "unit_price",
    "net_price",
    "amount",
    "extended_price",
    "extendnd_price",
    "due_date",
    "variant",
}
_ROW_DESCRIPTION_FIELDS = {"description"}
_TABLE_HEADER_CUES = (
    "item",
    "description",
    "qty",
    "uom",
    "u m",
    "unit",
    "cost",
    "price",
    "amount",
    "variant",
    "pack",
    "due",
    "order",
    "ship",
    "no",
    "supplier code",
)


def _normalize_text(text: str) -> str:
    text = str(text or "")
    text = text.replace("\u00a0", " ")
    text = text.replace("\u2010", "-").replace("\u2011", "-").replace("\u2012", "-")
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    return _WS_RE.sub(" ", text).strip().lower()


def _normalize_token(token: str) -> str:
    token = _normalize_text(token)
    return token.strip(".,:;()[]{}\"'")


def _normalize_token_loose(token: str) -> str:
    return _NON_ALNUM_RE.sub("", _normalize_token(token))


_ROW_STRONG_FIELDS_NORM = {_normalize_token_loose(name) for name in _ROW_STRONG_FIELDS}
_ROW_MEDIUM_FIELDS_NORM = {_normalize_token_loose(name) for name in _ROW_MEDIUM_FIELDS}
_ROW_WEAK_FIELDS_NORM = {_normalize_token_loose(name) for name in _ROW_WEAK_FIELDS}
_ROW_DESCRIPTION_FIELDS_NORM = {_normalize_token_loose(name) for name in _ROW_DESCRIPTION_FIELDS}


def _valid_box(box: Any) -> bool:
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    try:
        x0, y0, x1, y1 = [float(v) for v in box]
    except Exception:
        return False
    return x1 > x0 and y1 > y0


def _box_union(boxes: list[list[int]]) -> list[int]:
    return [
        int(min(b[0] for b in boxes)),
        int(min(b[1] for b in boxes)),
        int(max(b[2] for b in boxes)),
        int(max(b[3] for b in boxes)),
    ]


def _box_center_x(box: list[int]) -> float:
    return (box[0] + box[2]) / 2.0


def _box_center_y(box: list[int]) -> float:
    return (box[1] + box[3]) / 2.0


def _box_area(box: list[int]) -> int:
    return max(1, box[2] - box[0]) * max(1, box[3] - box[1])


def _estimate_sub_box(box: list[int], text: str, start: int, end: int) -> list[int]:
    """Estimate a tighter sub-box for substring [start:end] inside OCR chunk text."""
    x0, y0, x1, y1 = [int(v) for v in box]
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)
    text_len = max(1, len(text))

    start = max(0, min(start, text_len - 1))
    end = max(start + 1, min(end, text_len))

    # Most OCR boxes here are horizontal. Keep a vertical fallback for safety.
    if width >= height:
        sx = int(round(x0 + (start / text_len) * width))
        ex = int(round(x0 + (end / text_len) * width))
        if ex <= sx:
            ex = min(x1, sx + 2)
        return [max(x0, sx), y0, min(x1, ex), y1]

    sy = int(round(y0 + (start / text_len) * height))
    ey = int(round(y0 + (end / text_len) * height))
    if ey <= sy:
        ey = min(y1, sy + 2)
    return [x0, max(y0, sy), x1, min(y1, ey)]


def _box_is_sane(word_boxes: list[list[int]], max_height_ratio: float = 3.5) -> bool:
    """Reject merged boxes that span too many rows."""
    if len(word_boxes) <= 1:
        return True

    union = _box_union(word_boxes)
    combined_h = union[3] - union[1]
    combined_w = union[2] - union[0]
    if combined_h <= 0 or combined_w <= 0:
        return False

    heights = [max(1, b[3] - b[1]) for b in word_boxes]
    avg_h = sum(heights) / len(heights)
    if combined_h > avg_h * max_height_ratio:
        return False

    # Also reject if Y centers are spread too far apart.
    centers = [((b[1] + b[3]) / 2.0) for b in word_boxes]
    if max(centers) - min(centers) > avg_h * 2.4:
        return False

    return True


def _choose_best(matches: list[dict], x_target: float | None = None) -> dict | None:
    if not matches:
        return None
    if x_target is not None:
        return min(matches, key=lambda m: (abs(_box_center_x(m["box"]) - x_target), _box_area(m["box"])))
    return min(matches, key=lambda m: (_box_area(m["box"]), -float(m.get("score", 0.0))))


def _tokenize_with_spans(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def _build_token_stream(entries: list[dict]) -> list[dict]:
    stream: list[dict] = []
    for entry_idx, entry in enumerate(entries):
        raw_text = entry["raw"]
        for token_raw, start, end in _tokenize_with_spans(raw_text):
            token_norm = _normalize_token(token_raw)
            token_loose = _normalize_token_loose(token_raw)
            if not token_norm:
                continue
            stream.append(
                {
                    "entry_idx": entry_idx,
                    "token_raw": token_raw,
                    "token_norm": token_norm,
                    "token_loose": token_loose,
                    "token_box": _estimate_sub_box(entry["box"], raw_text, start, end),
                    "source_box": entry["box"],
                }
            )
    return stream


def _match_token_sequence(
    stream: list[dict],
    target_tokens: list[str],
    entries: list[dict],
    use_loose: bool,
) -> list[dict]:
    matches: list[dict] = []
    if not stream or not target_tokens:
        return matches

    field = "token_loose" if use_loose else "token_norm"
    n = len(target_tokens)
    if n > len(stream):
        return matches

    for i in range(len(stream) - n + 1):
        window = stream[i : i + n]
        if any(window[j][field] != target_tokens[j] for j in range(n)):
            continue

        display_boxes = [t["token_box"] for t in window]
        source_boxes_map: dict[tuple[int, int, int, int], list[int]] = {}
        for t in window:
            key = tuple(t["source_box"])
            source_boxes_map[key] = t["source_box"]

        entry_indices = sorted({t["entry_idx"] for t in window})
        avg_score = sum(float(entries[idx]["score"]) for idx in entry_indices) / max(1, len(entry_indices))
        matches.append(
            {
                "box": _box_union(display_boxes),
                "matched_text": " ".join(t["token_raw"] for t in window),
                "matched_boxes": list(source_boxes_map.values()),
                "score": round(avg_score, 4),
                "strategy": "token_chain",
            }
        )
    return matches


def find_value_in_ocr(
    value: str,
    ocr_words: list[dict],
    fuzzy_threshold: float = 0.80,
    y_band: tuple[float, float] | None = None,
    x_target: float | None = None,
) -> dict | None:
    """
    Search for one extracted value in OCR words.

    Returns:
      {box, matched_text, matched_boxes, score, strategy} or None
    """
    if not value or not ocr_words:
        return None

    target_raw = str(value).strip()
    target_norm = _normalize_text(target_raw)
    if not target_norm:
        return None

    target_tokens = [_normalize_token(t) for t in target_raw.split() if _normalize_token(t)]
    target_loose_tokens = [_normalize_token_loose(t) for t in target_raw.split() if _normalize_token_loose(t)]

    # Filter words first (and normalize once).
    entries: list[dict] = []
    target_raw_lower = target_raw.lower()
    for word in ocr_words:
        raw = str(word.get("text", "")).strip()
        box = word.get("box")
        if not raw or not _valid_box(box):
            continue
        if y_band is not None:
            center_y = (float(box[1]) + float(box[3])) / 2.0
            if not ((y_band[0] - 15) <= center_y <= (y_band[1] + 15)):
                continue
        entries.append(
            {
                "raw": raw,
                "raw_lower": raw.lower(),
                "norm": _normalize_text(raw),
                "loose": _NON_ALNUM_RE.sub("", _normalize_text(raw)),
                "box": [int(round(float(v))) for v in box],
                "score": float(word.get("score", 0.0)),
            }
        )
    if not entries:
        return None

    # Strategy 1: exact text
    exact_matches: list[dict] = []
    for entry in entries:
        if entry["norm"] == target_norm:
            exact_matches.append(
                {
                    "box": entry["box"],
                    "matched_text": entry["raw"],
                    "matched_boxes": [entry["box"]],
                    "score": round(entry["score"], 4),
                    "strategy": "exact",
                }
            )
    best = _choose_best(exact_matches, x_target=x_target)
    if best:
        return best

    # Strategy 2: token chain (tight box for values inside long chunks)
    if target_tokens:
        stream = _build_token_stream(entries)
        chain_matches = _match_token_sequence(stream, target_tokens, entries, use_loose=False)
        if not chain_matches and target_loose_tokens:
            chain_matches = _match_token_sequence(stream, target_loose_tokens, entries, use_loose=True)
        best = _choose_best(chain_matches, x_target=x_target)
        if best:
            return best

    # Strategy 3: contains / subspan on one OCR chunk
    contains_matches: list[dict] = []
    for entry in entries:
        raw = entry["raw"]
        raw_lower = entry["raw_lower"]
        if target_raw_lower in raw_lower:
            idx = raw_lower.find(target_raw_lower)
            sub_box = _estimate_sub_box(entry["box"], raw, idx, idx + len(target_raw_lower))
            strategy = "contains_subspan" if len(raw) > len(target_raw) + 4 else "contains"
            contains_matches.append(
                {
                    "box": sub_box,
                    "matched_text": raw[idx : idx + len(target_raw)],
                    "matched_boxes": [entry["box"]],
                    "score": round(entry["score"], 4),
                    "strategy": strategy,
                    "diff": len(raw) - len(target_raw),
                }
            )
        elif len(target_tokens) == 1 and raw_lower in target_raw_lower and len(raw_lower) >= max(2, int(len(target_raw_lower) * 0.6)):
            contains_matches.append(
                {
                    "box": entry["box"],
                    "matched_text": raw,
                    "matched_boxes": [entry["box"]],
                    "score": round(entry["score"], 4),
                    "strategy": "contains",
                    "diff": len(target_raw) - len(raw),
                }
            )

    if contains_matches:
        min_diff = min(m["diff"] for m in contains_matches)
        best = _choose_best([m for m in contains_matches if m["diff"] == min_diff], x_target=x_target)
        if best:
            best.pop("diff", None)
            return best

    # Strategy 4: multi-span chunks (no anchor expansion to avoid giant boxes)
    multi_span_matches: list[dict] = []
    max_span = min(10, len(entries))
    for i in range(len(entries)):
        combined_parts: list[str] = []
        for j in range(i, min(i + max_span, len(entries))):
            combined_parts.append(entries[j]["raw"])
            combined = " ".join(combined_parts)
            combined_norm = _normalize_text(combined)

            # Early break if span became much larger than target.
            if len(combined_norm) > len(target_norm) * 1.8 + 12:
                break

            if target_norm in combined_norm:
                span_boxes = [entries[k]["box"] for k in range(i, j + 1)]
                if not _box_is_sane(span_boxes):
                    continue
                avg_score = sum(entries[k]["score"] for k in range(i, j + 1)) / (j - i + 1)
                multi_span_matches.append(
                    {
                        "box": _box_union(span_boxes),
                        "matched_text": combined,
                        "matched_boxes": span_boxes,
                        "score": round(avg_score, 4),
                        "strategy": "multi_span",
                    }
                )

    best = _choose_best(multi_span_matches, x_target=x_target)
    if best:
        return best

    # Strategy 5: fuzzy
    is_short_numeric = len(target_raw) <= 4 and any(c.isdigit() for c in target_raw)
    if is_short_numeric:
        return None

    cutoff_100 = fuzzy_threshold * 100.0
    candidate_map = {idx: e["norm"] for idx, e in enumerate(entries) if e["norm"] and len(e["norm"]) >= 3}
    if candidate_map:
        best_single = process.extractOne(
            target_norm,
            candidate_map,
            scorer=fuzz.ratio,
            score_cutoff=cutoff_100,
            processor=None,
        )
        if best_single:
            idx = best_single[2]
            sim = best_single[1] / 100.0
            entry = entries[idx]
            return {
                "box": entry["box"],
                "matched_text": entry["raw"],
                "matched_boxes": [entry["box"]],
                "score": round(entry["score"], 4),
                "strategy": f"fuzzy({sim:.2f})",
            }

    best_span: dict | None = None
    best_sim = 0.0
    for i in range(len(entries)):
        parts: list[str] = []
        for j in range(i, min(i + max_span, len(entries))):
            parts.append(entries[j]["raw"])
            combined = " ".join(parts)
            combined_norm = _normalize_text(combined)

            if abs(len(combined_norm) - len(target_norm)) > max(6, int(len(target_norm) * 0.35)):
                if len(combined_norm) > len(target_norm):
                    break
                continue

            sim_100 = fuzz.ratio(target_norm, combined_norm, score_cutoff=cutoff_100)
            if sim_100 <= 0:
                continue
            sim = sim_100 / 100.0
            if sim <= best_sim:
                continue

            span_boxes = [entries[k]["box"] for k in range(i, j + 1)]
            if not _box_is_sane(span_boxes):
                continue
            avg_score = sum(entries[k]["score"] for k in range(i, j + 1)) / (j - i + 1)
            best_sim = sim
            best_span = {
                "box": _box_union(span_boxes),
                "matched_text": combined,
                "matched_boxes": span_boxes,
                "score": round(avg_score, 4),
                "strategy": f"fuzzy({sim:.2f})",
            }

    return best_span


_HIGH_STRATEGIES = {"exact", "contains", "contains_subspan", "token_chain"}
_MEDIUM_STRATEGIES = {"multi_span"}


def _classify_confidence(strategy: str) -> str:
    base = strategy.split("(")[0]
    if base in _HIGH_STRATEGIES:
        return "high"
    if base in _MEDIUM_STRATEGIES:
        return "medium"
    return "low"


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    val = str(value).strip()
    return not val or val.lower() in {"null", "none", "n/a", "-"}


def _split_value_lines(value: str) -> list[str]:
    return [ln.strip() for ln in re.split(r"[\r\n]+", str(value)) if ln and ln.strip()]


def _ordered_pages(ocr_pages: list[dict], preferred_page: int | None = None) -> list[dict]:
    if preferred_page is None:
        return ocr_pages
    return sorted(ocr_pages, key=lambda p, pref=preferred_page: 0 if p.get("page_number") == pref else 1)


def _median_int(values: list[float | int]) -> int:
    if not values:
        return 0
    ordered = sorted(float(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return int(round(ordered[mid]))
    return int(round((ordered[mid - 1] + ordered[mid]) / 2.0))


def _field_weight(field_name: str) -> int:
    normalized = _normalize_token_loose(field_name)
    if normalized in _ROW_STRONG_FIELDS_NORM:
        return 5
    if normalized in _ROW_MEDIUM_FIELDS_NORM:
        return 3
    if normalized in _ROW_WEAK_FIELDS_NORM:
        return 2
    if normalized in _ROW_DESCRIPTION_FIELDS_NORM:
        return 1
    return 2


def _strategy_score_factor(strategy: str) -> float:
    base = strategy.split("(")[0]
    if base == "exact":
        return 1.0
    if base in {"token_chain", "contains", "contains_subspan", "multi_span"}:
        return 0.85
    if base == "fuzzy":
        return 0.60
    return 0.0


def _strategy_to_match_mode(strategy: str) -> str:
    base = strategy.split("(")[0]
    if base in {"exact", "token_chain", "contains", "contains_subspan"}:
        return "cell_exact"
    if base == "multi_span":
        return "cell_span"
    if base == "fuzzy":
        return "cell_fuzzy"
    return "cell_exact"


def _prepare_page_words(page_words: list[dict]) -> list[dict]:
    prepared: list[dict] = []
    for word in page_words:
        raw = str(word.get("text", "")).strip()
        box = word.get("box")
        if not raw or not _valid_box(box):
            continue
        prepared.append(
            {
                "text": raw,
                "box": [int(round(float(v))) for v in box],
                "score": float(word.get("score", 0.0)),
            }
        )
    prepared.sort(key=lambda w: (_box_center_y(w["box"]), w["box"][0]))
    return prepared


def _build_ocr_lines(page_words: list[dict]) -> list[dict]:
    lines: list[dict] = []
    for word in page_words:
        word_box = word["box"]
        word_h = max(1, word_box[3] - word_box[1])
        word_cy = _box_center_y(word_box)

        if not lines:
            lines.append(
                {
                    "words": [word],
                    "box": word_box[:],
                    "center_y": word_cy,
                    "avg_height": float(word_h),
                }
            )
            continue

        last = lines[-1]
        tolerance = max(8.0, min(20.0, max(last["avg_height"], float(word_h)) * 0.65))
        if abs(word_cy - last["center_y"]) <= tolerance:
            last["words"].append(word)
            last["words"].sort(key=lambda w: w["box"][0])
            last["box"] = _box_union([w["box"] for w in last["words"]])
            last["center_y"] = sum(_box_center_y(w["box"]) for w in last["words"]) / len(last["words"])
            last["avg_height"] = sum(max(1, w["box"][3] - w["box"][1]) for w in last["words"]) / len(last["words"])
        else:
            lines.append(
                {
                    "words": [word],
                    "box": word_box[:],
                    "center_y": word_cy,
                    "avg_height": float(word_h),
                }
            )

    for idx, line in enumerate(lines):
        line["index"] = idx
        line["text"] = " ".join(w["text"] for w in sorted(line["words"], key=lambda w: w["box"][0]))
        line["norm"] = _normalize_text(line["text"])
    return lines


def _detect_table_region(lines: list[dict], line_items: list[dict]) -> dict[str, int | None]:
    if not lines:
        return {"header_y": None, "data_start_y": 0, "data_end_y": 0}

    header_idx: int | None = None
    for idx, line in enumerate(lines):
        norm = line.get("norm", "")
        cue_hits = sum(1 for cue in _TABLE_HEADER_CUES if cue in norm)
        if cue_hits >= 2:
            header_idx = idx
            break

    if header_idx is None and line_items:
        sample_rows = [row for row in line_items[: min(3, len(line_items))] if isinstance(row, dict)]
        for idx, line in enumerate(lines):
            line_score = 0.0
            for row in sample_rows:
                for field_name, cell_value in row.items():
                    if _is_empty_value(cell_value):
                        continue
                    if find_value_in_ocr(str(cell_value).strip(), line.get("words", []), fuzzy_threshold=0.88):
                        line_score += _field_weight(field_name)
            if line_score >= 4.0:
                header_idx = max(0, idx - 1)
                break

    data_start_idx = min(len(lines) - 1, header_idx + 1) if header_idx is not None else 0
    data_start_y = lines[data_start_idx]["box"][1]
    data_end_y = max(line["box"][3] for line in lines[data_start_idx:]) if lines[data_start_idx:] else lines[-1]["box"][3]
    header_y = lines[header_idx]["box"][1] if header_idx is not None else None
    return {"header_y": header_y, "data_start_y": data_start_y, "data_end_y": data_end_y}


def _line_has_left_anchor(line: dict, table_left: int) -> bool:
    words = line.get("words", [])
    if not words:
        return False
    first_box = words[0]["box"]
    return first_box[0] <= table_left + 45


def _should_merge_row_line(current_row: dict, next_line: dict, table_left: int, median_height: int) -> bool:
    gap = next_line["box"][1] - current_row["box"][3]
    if gap > max(10, int(median_height * 1.2)):
        return False
    if next_line["box"][0] <= table_left + 45:
        return False
    if _line_has_left_anchor(next_line, table_left):
        return False
    return True


def _build_logical_rows(lines: list[dict], table_region: dict[str, int | None]) -> list[dict]:
    data_start_y = int(table_region.get("data_start_y") or 0)
    data_end_y = int(table_region.get("data_end_y") or 0)
    data_lines = [line for line in lines if line["box"][1] >= data_start_y - 4 and line["box"][3] <= data_end_y + 4]
    if not data_lines:
        return []

    table_left = min(line["box"][0] for line in data_lines)
    median_height = max(10, _median_int([line["avg_height"] for line in data_lines]))
    rows: list[dict] = []
    for line in data_lines:
        if not rows or not _should_merge_row_line(rows[-1], line, table_left, median_height):
            rows.append({"lines": [line], "box": line["box"][:]})
            continue
        rows[-1]["lines"].append(line)
        rows[-1]["box"] = _box_union([ln["box"] for ln in rows[-1]["lines"]])

    logical_rows: list[dict] = []
    for idx, row in enumerate(rows):
        row_words = [word for line in row["lines"] for word in line.get("words", [])]
        if not row_words:
            continue
        row_words.sort(key=lambda word: (_box_center_y(word["box"]), word["box"][0]))
        row_box = _box_union([word["box"] for word in row_words])
        row_text = " ".join(word["text"] for word in row_words)
        logical_rows.append(
            {
                "index": idx,
                "box": row_box,
                "words": row_words,
                "text": row_text,
                "norm": _normalize_text(row_text),
            }
        )
    return logical_rows


def _build_page_models(ocr_pages: list[dict], line_items: list[dict]) -> dict[int, dict]:
    page_models: dict[int, dict] = {}
    for page_data in ocr_pages:
        page_number = int(page_data.get("page_number", 0) or 0)
        prepared_words = _prepare_page_words(page_data.get("words", []))
        lines = _build_ocr_lines(prepared_words)
        table_region = _detect_table_region(lines, line_items)
        rows = _build_logical_rows(lines, table_region)
        for row in rows:
            row["page"] = page_number
        page_models[page_number] = {
            "page_number": page_number,
            "ocr_words": prepared_words,
            "ocr_lines": lines,
            "ocr_rows": rows,
            "table_region": table_region,
        }
    return page_models


def _score_qwen_row_to_ocr_row(row: dict[str, Any], candidate_row: dict) -> tuple[float, list[dict]]:
    score = 0.0
    evidence: list[dict] = []
    has_anchor = False

    for field_name, cell_value in row.items():
        if _is_empty_value(cell_value):
            continue
        weight = _field_weight(field_name)
        if weight <= 0:
            continue
        location = find_value_in_ocr(str(cell_value).strip(), candidate_row.get("words", []), fuzzy_threshold=0.88)
        if not location:
            continue
        factor = _strategy_score_factor(location.get("strategy", ""))
        if factor <= 0:
            continue
        weighted = weight * factor
        score += weighted
        if weight >= 3:
            has_anchor = True
        evidence.append(
            {
                "field_name": field_name,
                "strategy": location.get("strategy", ""),
                "weight": weight,
                "score": weighted,
            }
        )

    if not has_anchor and score < 4.0:
        return 0.0, []
    return score, evidence


def _assign_qwen_rows_to_ocr_rows(
    line_items: list[dict],
    row_candidates: list[dict],
    row_page_map: dict[int, int],
) -> tuple[dict[int, int], dict[tuple[int, int], list[dict]]]:
    n = len(line_items)
    m = len(row_candidates)
    if not n or not m:
        return {}, {}

    score_matrix: list[list[float]] = [[0.0 for _ in range(m)] for _ in range(n)]
    evidence_map: dict[tuple[int, int], list[dict]] = {}
    for row_idx, row in enumerate(line_items):
        if not isinstance(row, dict):
            continue
        preferred_page = row_page_map.get(row_idx)
        for cand_idx, candidate in enumerate(row_candidates):
            if preferred_page is not None and candidate.get("page") != preferred_page:
                continue
            score, evidence = _score_qwen_row_to_ocr_row(row, candidate)
            if score <= 0:
                continue
            score_matrix[row_idx][cand_idx] = score
            evidence_map[(row_idx, cand_idx)] = evidence

    dp: list[list[float]] = [[0.0 for _ in range(m + 1)] for _ in range(n + 1)]
    parent: list[list[tuple[int, int, str] | None]] = [[None for _ in range(m + 1)] for _ in range(n + 1)]

    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0]
        parent[i][0] = (i - 1, 0, "skip_qwen")
    for j in range(1, m + 1):
        parent[0][j] = (0, j - 1, "skip_ocr")

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best_score = dp[i - 1][j]
            best_parent = (i - 1, j, "skip_qwen")

            if dp[i][j - 1] > best_score:
                best_score = dp[i][j - 1]
                best_parent = (i, j - 1, "skip_ocr")

            candidate_score = score_matrix[i - 1][j - 1]
            if candidate_score > 0 and dp[i - 1][j - 1] + candidate_score > best_score:
                best_score = dp[i - 1][j - 1] + candidate_score
                best_parent = (i - 1, j - 1, "match")

            dp[i][j] = best_score
            parent[i][j] = best_parent

    assignments: dict[int, int] = {}
    i, j = n, m
    while i > 0 or j > 0:
        step = parent[i][j]
        if step is None:
            break
        prev_i, prev_j, action = step
        if action == "match" and score_matrix[i - 1][j - 1] > 0:
            assignments[i - 1] = j - 1
        i, j = prev_i, prev_j

    return assignments, evidence_map


def _box_from_location(location: dict[str, Any]) -> list[int] | None:
    box = location.get("box")
    if _valid_box(box):
        return [int(round(float(v))) for v in box]
    return None


def compute_field_locations(
    extraction_result: dict[str, Any],
    ocr_pages: list[dict],
    page_results: list[dict] | None = None,
) -> dict[str, dict]:
    """
    Match extracted fields to OCR boxes.

    Header keys:
      "vendor", "bill_to", ...
    Line item keys:
      "line_item_{row}_{column}"
    """
    t0 = time.perf_counter()
    locations: dict[str, dict] = {}
    skipped: list[str] = []
    missed: list[str] = []

    # Handle po_per_page style result list.
    if isinstance(extraction_result, list):
        merged: dict[str, Any] = {}
        all_items: list[dict] = []
        for entry in extraction_result:
            if not isinstance(entry, dict):
                continue
            for key, val in entry.items():
                if key == "line_items":
                    if isinstance(val, list):
                        all_items.extend(val)
                elif key not in merged and key not in {"_page", "_total_pages", "_error"}:
                    merged[key] = val
        merged["line_items"] = all_items
        extraction_result = merged

    line_items = extraction_result.get("line_items")

    # Stage 1: headers
    for field_name, field_value in extraction_result.items():
        if field_name == "line_items":
            continue
        if _is_empty_value(field_value):
            skipped.append(field_name)
            continue

        val_str = str(field_value).strip()
        lines = _split_value_lines(val_str)
        if not lines:
            skipped.append(field_name)
            continue

        found = False
        line_hits: list[dict] = []
        preferred_page: int | None = None
        for line in lines:
            hit_for_line: dict | None = None
            for page_data in _ordered_pages(ocr_pages, preferred_page):
                location = find_value_in_ocr(line, page_data.get("words", []))
                if not location:
                    continue
                location["page"] = page_data["page_number"]
                location["confidence"] = _classify_confidence(location["strategy"])
                location.pop("matched_boxes", None)
                hit_for_line = location
                preferred_page = page_data["page_number"]
                break
            if hit_for_line:
                line_hits.append(hit_for_line)

        if line_hits:
            primary = dict(line_hits[0])
            same_page_hits = [hit for hit in line_hits if hit["page"] == primary["page"]]
            if len(same_page_hits) > 1:
                primary["box"] = _box_union([hit["box"] for hit in same_page_hits])
            if len(line_hits) > 1:
                primary["sub_locations"] = [
                    {
                        "page": h["page"],
                        "box": h["box"],
                        "matched_text": h["matched_text"],
                        "strategy": h["strategy"],
                        "confidence": h["confidence"],
                    }
                    for h in line_hits[1:]
                ]
            locations[field_name] = primary
            found = True
            logger.debug(
                "Matched %s='%s' via %s (%s)",
                field_name,
                lines[0][:30],
                primary["strategy"],
                primary["confidence"],
            )

        if not found:
            missed.append(field_name)

    # Stage 2: line items
    li_total = 0
    li_matched = 0

    row_page_map: dict[int, int] = {}
    if page_results:
        running_idx = 0
        for pr in page_results:
            page_num = pr.get("_page", 1)
            page_items = pr.get("line_items", [])
            if isinstance(page_items, list):
                for _ in page_items:
                    row_page_map[running_idx] = page_num
                    running_idx += 1

    if isinstance(line_items, list):
        li_total = sum(
            1
            for row in line_items
            if isinstance(row, dict)
            for value in row.values()
            if not _is_empty_value(value)
        )

        page_models = _build_page_models(ocr_pages, [row for row in line_items if isinstance(row, dict)])
        row_candidates: list[dict] = []
        for page_num in sorted(page_models):
            page_rows = page_models[page_num].get("ocr_rows", [])
            row_candidates.extend(sorted(page_rows, key=lambda row: (row["box"][1], row["box"][0])))

        row_assignments, _ = _assign_qwen_rows_to_ocr_rows(line_items, row_candidates, row_page_map)
        missing_fields_by_row: dict[int, list[tuple[str, str, dict]]] = defaultdict(list)

        for row_idx, row in enumerate(line_items):
            if not isinstance(row, dict):
                continue
            assigned_candidate = row_candidates[row_assignments[row_idx]] if row_idx in row_assignments else None

            for col_name, cell_value in row.items():
                if _is_empty_value(cell_value):
                    continue
                cell_str = str(cell_value).strip()
                key_name = f"line_item_{row_idx}_{col_name}"

                if assigned_candidate is not None:
                    location = find_value_in_ocr(cell_str, assigned_candidate.get("words", []), fuzzy_threshold=0.88)
                    if location:
                        value_box = _box_from_location(location)
                        if value_box:
                            location.pop("matched_boxes", None)
                            location["page"] = assigned_candidate["page"]
                            location["box"] = value_box
                            location["value_box"] = value_box[:]
                            location["row_box"] = assigned_candidate["box"][:]
                            location["row_index"] = row_idx
                            location["field_name"] = col_name
                            location["match_mode"] = _strategy_to_match_mode(location["strategy"])
                            location["confidence"] = _classify_confidence(location["strategy"])
                            locations[key_name] = location
                            li_matched += 1
                            continue

                    missing_fields_by_row[row_idx].append((col_name, cell_str, assigned_candidate))
                    continue

                preferred_page = row_page_map.get(row_idx)
                for page_data in _ordered_pages(ocr_pages, preferred_page):
                    location = find_value_in_ocr(cell_str, page_data.get("words", []), fuzzy_threshold=0.88)
                    if not location:
                        continue
                    value_box = _box_from_location(location)
                    if not value_box:
                        continue
                    location.pop("matched_boxes", None)
                    location["page"] = page_data["page_number"]
                    location["box"] = value_box
                    location["value_box"] = value_box[:]
                    location["row_box"] = None
                    location["row_index"] = row_idx
                    location["field_name"] = col_name
                    location["match_mode"] = "value_fallback_global"
                    location["confidence"] = "low"
                    locations[key_name] = location
                    li_matched += 1
                    break

        fallback_spans: dict[tuple[int, str], list[list[int]]] = defaultdict(list)
        for field_name, location in locations.items():
            if not field_name.startswith("line_item_"):
                continue
            if location.get("match_mode") not in {"cell_exact", "cell_span"}:
                continue
            page = location.get("page")
            column = location.get("field_name")
            value_box = location.get("value_box") or location.get("box")
            if page is None or not column or not _valid_box(value_box):
                continue
            fallback_spans[(int(page), str(column))].append([int(round(float(v))) for v in value_box])

        for row_idx, missing_fields in missing_fields_by_row.items():
            for col_name, _cell_str, assigned_candidate in missing_fields:
                key_name = f"line_item_{row_idx}_{col_name}"
                if key_name in locations:
                    continue

                spans = fallback_spans.get((assigned_candidate["page"], col_name), [])
                if len(spans) < 2:
                    continue

                left = _median_int([box[0] for box in spans])
                right = _median_int([box[2] for box in spans])
                row_box = [int(v) for v in assigned_candidate["box"]]
                fallback_box = [max(row_box[0], left), row_box[1], min(row_box[2], right), row_box[3]]
                if not _valid_box(fallback_box):
                    continue

                locations[key_name] = {
                    "page": assigned_candidate["page"],
                    "box": fallback_box,
                    "matched_text": "",
                    "score": 0.0,
                    "strategy": "row_fallback_vertical",
                    "confidence": "low",
                    "row_index": row_idx,
                    "field_name": col_name,
                    "row_box": row_box,
                    "value_box": None,
                    "fallback_column_box": fallback_box[:],
                    "match_mode": "row_fallback_vertical",
                }
                li_matched += 1

    elapsed_ms = (time.perf_counter() - t0) * 1000
    header_matched = len([k for k in locations if not k.startswith("line_item_")])
    header_total = header_matched + len(missed)
    logger.info(
        "Text matching complete: headers=%d/%d, line_items=%d/%d, missed=%d, skipped=%d | %.1fms",
        header_matched,
        header_total,
        li_matched,
        li_total,
        len(missed),
        len(skipped),
        elapsed_ms,
    )
    if missed:
        logger.info("  Unmatched header fields: %s", ", ".join(missed))
    return locations
