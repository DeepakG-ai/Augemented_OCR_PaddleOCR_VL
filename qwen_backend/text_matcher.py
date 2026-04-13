"""
text_matcher.py -- Match Qwen VL extracted field values to PaddleOCR bounding boxes.

Given:
  - extraction_result: {"vendor_name": "FRESH PRODUCTS, INC.", "po_number": "P1416576", ...}
  - ocr_pages: [{"page_number": 1, "words": [{"text": "...", "box": [x0,y0,x1,y1], "score": 0.98}, ...]}]

Returns:
  - field_locations: {"vendor_name": {"page": 1, "box": [128,214,338,229], "matched_text": "...", "score": 0.96, "strategy": "exact"}}

Matching strategies (tried in order):
  1. exact     -- OCR text == extracted value (case-insensitive)
  2. contains  -- one contains the other
  3. multi_span -- combine consecutive OCR spans
  4. fuzzy     -- Levenshtein similarity above threshold
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("text_matcher")


# ---------------------------------------------------------------------------
# Levenshtein distance (no external dependency)
# ---------------------------------------------------------------------------

def _levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


def _similarity(s1: str, s2: str) -> float:
    if not s1 or not s2:
        return 0.0
    max_len = max(len(s1), len(s2))
    if max_len == 0:
        return 1.0
    return 1.0 - (_levenshtein(s1, s2) / max_len)


# ---------------------------------------------------------------------------
# Spatial sanity check for multi-word bounding boxes
# ---------------------------------------------------------------------------

def _box_is_sane(word_boxes: list[list], max_height_ratio: float = 5.0) -> bool:
    """Reject a combined bounding box if it is unreasonably tall or wide.
    
    When OCR words from different rows/columns get merged, the resulting
    box can span the entire page. This guard catches those cases.
    
    Rules:
      - Combined box height must be <= max_height_ratio * avg individual word height
      - Combined box height must be <= combined box width * 3  (no vertical sliver)
    """
    if len(word_boxes) <= 1:
        return True  # single word is always fine
    
    x0 = min(b[0] for b in word_boxes)
    y0 = min(b[1] for b in word_boxes)
    x1 = max(b[2] for b in word_boxes)
    y1 = max(b[3] for b in word_boxes)
    
    combined_h = y1 - y0
    combined_w = x1 - x0
    
    if combined_h <= 0 or combined_w <= 0:
        return False
    
    # Average height of individual words
    avg_word_h = sum(b[3] - b[1] for b in word_boxes) / len(word_boxes)
    if avg_word_h <= 0:
        avg_word_h = 1
    
    # If combined height >> average word height, it spans multiple rows
    if combined_h > avg_word_h * max_height_ratio:
        return False
    
    return True


# ---------------------------------------------------------------------------
# Single-value matching against a page's OCR data
# ---------------------------------------------------------------------------

def find_value_in_ocr(
    value: str,
    ocr_words: list[dict],
    fuzzy_threshold: float = 0.80,
) -> dict | None:
    """
    Search for an extracted field value in PaddleOCR results.

    Args:
        value: The extracted value from Qwen VL (e.g. "FRESH PRODUCTS, INC.")
        ocr_words: List of PaddleOCR results [{text, box, score}, ...]
        fuzzy_threshold: Minimum similarity for fuzzy matching (0.0 - 1.0)

    Returns:
        {box: [x0,y0,x1,y1], matched_text, score, strategy} or None
    """
    if not value or not ocr_words:
        return None

    target = str(value).strip()
    target_lower = target.lower()
    target_words = target.split()

    if not target_lower:
        return None

    # â”€â”€ Strategy 1: Exact match (case-insensitive) â”€â”€
    for word in ocr_words:
        if word["text"].strip().lower() == target_lower:
            return {
                "box": word["box"],
                "matched_text": word["text"],
                "matched_boxes": [word["box"]],
                "score": round(float(word.get("score", 0)), 4),
                "strategy": "exact",
            }

    # â”€â”€ Strategy 2: Contains match â”€â”€
    # Check if OCR text contains the value, or value contains OCR text.
    # We prefer the TIGHTEST fit (shortest word that contains the target).
    best_contains = None
    best_contains_diff = float("inf")

    for word in ocr_words:
        word_lower = word["text"].strip().lower()
        if not word_lower:
            continue

        # OCR span contains the extracted value
        if target_lower in word_lower:
            diff = len(word_lower) - len(target_lower)
            if diff < best_contains_diff:
                best_contains = {
                    "box": word["box"],
                    "matched_text": word["text"],
                    "matched_boxes": [word["box"]],
                    "score": round(float(word.get("score", 0)), 4),
                    "strategy": "contains",
                }
                best_contains_diff = diff

        # Extracted value contains the OCR span.
        # Keep this only for single-token targets; for multi-word targets
        # we want the matcher to continue to multi-span instead of grabbing
        # one partial token like "PEPPER" for "RED PEPPER".
        if len(target_words) == 1 and word_lower in target_lower and len(word_lower) >= len(target_lower) * 0.6:
            diff = len(target_lower) - len(word_lower)
            if diff < best_contains_diff:
                best_contains = {
                    "box": word["box"],
                    "matched_text": word["text"],
                    "matched_boxes": [word["box"]],
                    "score": round(float(word.get("score", 0)), 4),
                    "strategy": "contains",
                }
                best_contains_diff = diff

    if best_contains:
        return best_contains

    # â”€â”€ Strategy 3: Multi-span match â”€â”€
    # Combine consecutive OCR spans and check if they form the target.
    # We restrict the combination length to avoid merging unrelated words.
    for i in range(len(ocr_words)):
        combined = ""
        for j in range(i, min(i + 25, len(ocr_words))):
            sep = " " if combined else ""
            combined += sep + ocr_words[j]["text"].strip()

            # If the combined string gets way longer than the target, stop this j loop
            if len(combined) > len(target_lower) * 1.5 + 5:
                break

            # Check if it equals or tightly contains the target
            if target_lower in combined.lower() and len(combined) <= len(target_lower) + 5:
                # Build bounding box from span[i] to span[j]
                span_boxes = [ocr_words[k]["box"] for k in range(i, j + 1)]
                # Spatial sanity check: reject if box spans too much page area
                if not _box_is_sane(span_boxes):
                    continue
                x0 = min(b[0] for b in span_boxes)
                y0 = min(b[1] for b in span_boxes)
                x1 = max(b[2] for b in span_boxes)
                y1 = max(b[3] for b in span_boxes)
                avg_score = sum(
                    float(ocr_words[k].get("score", 0)) for k in range(i, j + 1)
                ) / (j - i + 1)

                return {
                    "box": [x0, y0, x1, y1],
                    "matched_text": combined.strip(),
                    "score": round(avg_score, 4),
                    "strategy": "multi_span",
                }

    # â”€â”€ Strategy 4: Anchor match (first + last words for long values) â”€â”€
    # For long values like full addresses, find the first few words and
    # last few words separately, then build a box spanning both.
    if len(target_words) >= 6:
        first_anchor = " ".join(target_words[:3]).lower()
        last_anchor = " ".join(target_words[-3:]).lower()

        first_idx = None   # first OCR word index of the start anchor
        last_idx = None    # last OCR word index of the end anchor

        # Find first anchor (scan forward, stop at first match)
        for i in range(len(ocr_words)):
            combined = ""
            for j in range(i, min(i + 6, len(ocr_words))):
                combined = (combined + " " + ocr_words[j]["text"].strip()).strip()
                if first_anchor in combined.lower():
                    first_idx = i
                    break
            if first_idx is not None:
                break

        # Find last anchor (scan forward, start from first_idx)
        if first_idx is not None:
            # We restrict the distance to avoid spanning the whole page
            for i in range(first_idx, min(first_idx + 60, len(ocr_words))):
                combined = ""
                for j in range(i, min(i + 6, len(ocr_words))):
                    combined = (combined + " " + ocr_words[j]["text"].strip()).strip()
                    if last_anchor in combined.lower():
                        last_idx = j
                        break
                if last_idx is not None:
                    break

        if first_idx is not None and last_idx is not None and first_idx <= last_idx:
            anchor_boxes = [ocr_words[k]["box"] for k in range(first_idx, last_idx + 1)]
            # Spatial sanity check: reject if anchor box spans too much page
            if _box_is_sane(anchor_boxes, max_height_ratio=8.0):
                x0 = min(b[0] for b in anchor_boxes)
                y0 = min(b[1] for b in anchor_boxes)
                x1 = max(b[2] for b in anchor_boxes)
                y1 = max(b[3] for b in anchor_boxes)
                avg_score = sum(
                    float(ocr_words[k].get("score", 0)) for k in range(first_idx, last_idx + 1)
                ) / (last_idx - first_idx + 1)

                return {
                    "box": [x0, y0, x1, y1],
                    "matched_text": " ".join(
                        ocr_words[k]["text"].strip() for k in range(first_idx, last_idx + 1)
                    ),
                    "matched_boxes": anchor_boxes,
                    "score": round(avg_score, 4),
                    "strategy": "anchor",
                }

    # â”€â”€ Strategy 5: Fuzzy match (handles OCR typos) â”€â”€
    best_fuzzy = None
    best_sim = fuzzy_threshold

    for word in ocr_words:
        word_text = word["text"].strip()
        if not word_text or len(word_text) < 3:
            continue

        sim = _similarity(target_lower, word_text.lower())
        if sim > best_sim:
            best_sim = sim
            best_fuzzy = {
                "box": word["box"],
                "matched_text": word_text,
                "matched_boxes": [word["box"]],
                "score": round(float(word.get("score", 0)), 4),
                "strategy": f"fuzzy({sim:.2f})",
            }

    # Also try fuzzy on multi-span combinations
    for i in range(len(ocr_words)):
        combined = ""
        for j in range(i, min(i + 25, len(ocr_words))):
            sep = " " if combined else ""
            combined += sep + ocr_words[j]["text"].strip()

            if abs(len(combined) - len(target)) > len(target) * 0.5:
                # Skip if lengths are too different (optimization)
                if len(combined) > len(target):
                    break
                continue

            sim = _similarity(target_lower, combined.lower())
            if sim > best_sim:
                span_boxes = [ocr_words[k]["box"] for k in range(i, j + 1)]
                # Spatial sanity check
                if not _box_is_sane(span_boxes):
                    continue
                best_sim = sim
                x0 = min(b[0] for b in span_boxes)
                y0 = min(b[1] for b in span_boxes)
                x1 = max(b[2] for b in span_boxes)
                y1 = max(b[3] for b in span_boxes)
                avg_score = sum(
                    float(ocr_words[k].get("score", 0)) for k in range(i, j + 1)
                ) / (j - i + 1)

                best_fuzzy = {
                    "box": [x0, y0, x1, y1],
                    "matched_text": combined.strip(),
                    "matched_boxes": span_boxes,
                    "score": round(avg_score, 4),
                    "strategy": f"fuzzy({sim:.2f})",
                }

    return best_fuzzy


# ---------------------------------------------------------------------------
# Confidence classification based on matching strategy
# ---------------------------------------------------------------------------

_HIGH_STRATEGIES = {"exact", "contains", "multi_span"}
_MEDIUM_STRATEGIES = {"anchor"}
# fuzzy(*) strategies â†’ low


def _classify_confidence(strategy: str) -> str:
    """Return 'high', 'medium', or 'low' based on matching strategy."""
    base = strategy.split("(")[0]  # "fuzzy(0.87)" â†’ "fuzzy"
    if base in _HIGH_STRATEGIES:
        return "high"
    if base in _MEDIUM_STRATEGIES:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Compute field locations for all header fields + line item cells
# ---------------------------------------------------------------------------

def compute_field_locations(
    extraction_result: dict[str, Any],
    ocr_pages: list[dict],
    page_results: list[dict] | None = None,
) -> dict[str, dict]:
    """
    Match each extracted field value to its PaddleOCR bounding box.

    Covers:
      - Header fields  â†’  key = field_name  (e.g. "vendor_name")
      - Line item cells â†’  key = "line_item_{row}_{col}"  (e.g. "line_item_0_unit_price")

    Each location dict includes a 'confidence' key: 'high', 'medium', or 'low'.

    Args:
        page_results: Optional list of per-page extraction results. Used to
            determine which page each line item came from so we search the
            correct page first (fixes page 2 mapping).
    """
    t0 = time.perf_counter()
    locations: dict[str, dict] = {}
    skipped = []
    missed = []

    # Handle po_per_page format where extraction_result is a list of dicts.
    # Merge into a single dict: header from first entry, line_items concatenated.
    if isinstance(extraction_result, list):
        merged = {}
        all_items = []
        for entry in extraction_result:
            if not isinstance(entry, dict):
                continue
            for k, v in entry.items():
                if k == "line_items":
                    if isinstance(v, list):
                        all_items.extend(v)
                elif k not in merged and k not in ("_page", "_total_pages", "_error"):
                    merged[k] = v
        merged["line_items"] = all_items
        extraction_result = merged

    # â”€â”€ Header fields â”€â”€
    for field_name, field_value in extraction_result.items():
        if field_name == "line_items" or field_value is None:
            continue

        val_str = str(field_value).strip()
        if not val_str or val_str.lower() in ("null", "none", "n/a", "", "-"):
            skipped.append(field_name)
            continue

        found = False
        for page_data in ocr_pages:
            location = find_value_in_ocr(val_str, page_data.get("words", []))
            if location:
                location["page"] = page_data["page_number"]
                location["confidence"] = _classify_confidence(location["strategy"])
                locations[field_name] = location
                found = True
                logger.debug(
                    "Matched %s='%s' on page %d via %s (%s)",
                    field_name, val_str[:30], page_data["page_number"],
                    location["strategy"], location["confidence"],
                )
                location.pop("matched_boxes", None)
                break

        if not found:
            missed.append(field_name)

    # â”€â”€ Build row â†’ page map from page_results â”€â”€
    # This tells us which page each merged line item row came from,
    # so we can search that page first instead of always starting from page 1.
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

    # â”€â”€ Line item cells â”€â”€
    line_items = extraction_result.get("line_items")
    li_matched = 0
    li_total = 0

    # Track consumed OCR box positions so duplicate values (e.g. two rows
    # with the same qty) map to DIFFERENT OCR locations.
    # Key: (page_number, tuple(box))
    used_boxes: set[tuple] = set()

    if isinstance(line_items, list):
        for row_idx, row in enumerate(line_items):
            if not isinstance(row, dict):
                continue

            # Determine preferred page for this row
            preferred_page = row_page_map.get(row_idx)

            for col_name, cell_value in row.items():
                if cell_value is None:
                    continue
                cell_str = str(cell_value).strip()
                if not cell_str or cell_str.lower() in ("null", "none", "n/a", "", "-"):
                    continue

                li_total += 1
                composite_key = f"line_item_{row_idx}_{col_name}"

                # Sort OCR pages so the preferred page is searched first
                search_pages = ocr_pages
                if preferred_page is not None:
                    search_pages = sorted(
                        ocr_pages,
                        key=lambda p, pp=preferred_page: 0 if p["page_number"] == pp else 1,
                    )

                for page_data in search_pages:
                    page_num = page_data["page_number"]
                    # Filter out already-consumed words for this search
                    available_words = [
                        w for w in page_data.get("words", [])
                        if (page_num, tuple(w["box"])) not in used_boxes
                    ]
                    location = find_value_in_ocr(cell_str, available_words)
                    if location:
                        matched_boxes = location.pop("matched_boxes", [location["box"]])
                        location["page"] = page_num
                        location["confidence"] = _classify_confidence(location["strategy"])
                        location["row_idx"] = row_idx
                        location["col_name"] = col_name
                        locations[composite_key] = location
                        li_matched += 1
                        # Mark all matched OCR spans as consumed so the next
                        # duplicate value finds a different occurrence.
                        for box in matched_boxes:
                            used_boxes.add((page_num, tuple(box)))
                        break

    elapsed_ms = (time.perf_counter() - t0) * 1000
    header_total = len([k for k in locations if not k.startswith("line_item_")]) + len(missed)

    logger.info(
        "Text matching complete: headers=%d/%d, line_items=%d/%d, missed=%d, skipped=%d | %.1fms",
        header_total - len(missed), header_total,
        li_matched, li_total,
        len(missed), len(skipped), elapsed_ms,
    )
    if missed:
        logger.info("  Unmatched header fields: %s", ", ".join(missed))

    return locations
