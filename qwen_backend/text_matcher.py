"""
text_matcher.py -- Match extracted JSON values to PaddleOCR boxes.

Design goals:
1) Avoid giant "block" boxes for multi-line fields (vendor/bill_to/ship_to).
2) Support matching token values inside long OCR chunks (item codes in row text).
3) Keep output contract stable for the Review UI.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from rapidfuzz import fuzz, process

logger = logging.getLogger("text_matcher")

_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\S+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


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
            primary = line_hits[0]
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

    line_items = extraction_result.get("line_items")
    li_matched = 0
    li_total = 0

    # Key: (page_number, tuple(source_box)) -> row_idx that owns this source box.
    used_boxes: dict[tuple[int, tuple[int, int, int, int]], int] = {}

    page_words_cache: dict[int, list[tuple[dict, tuple[int, tuple[int, int, int, int]]]]] = {}
    for page_data in ocr_pages:
        page_num = page_data["page_number"]
        cached: list[tuple[dict, tuple[int, tuple[int, int, int, int]]]] = []
        for word in page_data.get("words", []):
            box = word.get("box")
            if _valid_box(box):
                key = (page_num, tuple(int(round(float(v))) for v in box))
                cached.append((word, key))
        page_words_cache[page_num] = cached

    def _get_available_words(page_num: int, row_idx: int) -> list[dict]:
        return [
            word
            for word, key in page_words_cache.get(page_num, [])
            if key not in used_boxes or used_boxes[key] == row_idx
        ]

    if isinstance(line_items, list):
        row_y_bands: dict[int, tuple[float, float]] = {}
        column_x_centers: dict[str, list[float]] = {}

        # Pass A: longer anchor-like values first
        for row_idx, row in enumerate(line_items):
            if not isinstance(row, dict):
                continue
            preferred_page = row_page_map.get(row_idx)
            search_pages = _ordered_pages(ocr_pages, preferred_page)
            row_min_y = float("inf")
            row_max_y = float("-inf")

            for col_name, cell_value in row.items():
                if _is_empty_value(cell_value):
                    continue
                cell_str = str(cell_value).strip()

                # Keep short/ambiguous values for pass B.
                if len(cell_str) <= 8 and not cell_str.isalpha():
                    continue

                li_total += 1
                key_name = f"line_item_{row_idx}_{col_name}"

                for page_data in search_pages:
                    page_num = page_data["page_number"]
                    available_words = _get_available_words(page_num, row_idx)
                    location = find_value_in_ocr(cell_str, available_words)
                    if not location:
                        continue

                    matched_boxes = location.pop("matched_boxes", [location["box"]])
                    location["page"] = page_num
                    location["confidence"] = _classify_confidence(location["strategy"])
                    location["row_idx"] = row_idx
                    location["col_name"] = col_name
                    locations[key_name] = location
                    li_matched += 1

                    for box in matched_boxes:
                        if _valid_box(box):
                            src_key = (page_num, tuple(int(round(float(v))) for v in box))
                            if src_key not in used_boxes:
                                used_boxes[src_key] = row_idx

                    bx = location["box"]
                    row_min_y = min(row_min_y, bx[1])
                    row_max_y = max(row_max_y, bx[3])
                    column_x_centers.setdefault(col_name, []).append(_box_center_x(bx))
                    break

            if row_min_y != float("inf"):
                row_y_bands[row_idx] = (row_min_y, row_max_y)

        col_median_x: dict[str, float] = {}
        for col_name, centers in column_x_centers.items():
            sorted_centers = sorted(centers)
            col_median_x[col_name] = sorted_centers[len(sorted_centers) // 2]

        # Pass B: short/ambiguous values constrained by row band + col X target
        for row_idx, row in enumerate(line_items):
            if not isinstance(row, dict):
                continue
            preferred_page = row_page_map.get(row_idx)
            search_pages = _ordered_pages(ocr_pages, preferred_page)
            y_band = row_y_bands.get(row_idx)

            for col_name, cell_value in row.items():
                if _is_empty_value(cell_value):
                    continue
                cell_str = str(cell_value).strip()

                if len(cell_str) > 8 or cell_str.isalpha():
                    continue

                key_name = f"line_item_{row_idx}_{col_name}"
                if key_name in locations:
                    continue

                li_total += 1
                x_target = col_median_x.get(col_name)

                for page_data in search_pages:
                    page_num = page_data["page_number"]
                    available_words = _get_available_words(page_num, row_idx)
                    location = find_value_in_ocr(cell_str, available_words, y_band=y_band, x_target=x_target)
                    if not location:
                        continue

                    matched_boxes = location.pop("matched_boxes", [location["box"]])
                    location["page"] = page_num
                    location["confidence"] = _classify_confidence(location["strategy"])
                    location["row_idx"] = row_idx
                    location["col_name"] = col_name
                    locations[key_name] = location
                    li_matched += 1

                    for box in matched_boxes:
                        if _valid_box(box):
                            src_key = (page_num, tuple(int(round(float(v))) for v in box))
                            if src_key not in used_boxes:
                                used_boxes[src_key] = row_idx

                    center_x = _box_center_x(location["box"])
                    col_median_x.setdefault(col_name, center_x)
                    break

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
