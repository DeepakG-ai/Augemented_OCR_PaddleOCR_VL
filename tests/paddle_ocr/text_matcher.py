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
# Fuzzy similarity (C++ compiled via rapidfuzz — 10-50× faster than Python)
# ---------------------------------------------------------------------------

from rapidfuzz import fuzz, process


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
    y_band: tuple[float, float] | None = None,
    x_target: float | None = None,
) -> dict | None:
    """
    Search for an extracted field value in PaddleOCR results.

    Args:
        value: The extracted value from Qwen VL (e.g. "FRESH PRODUCTS, INC.")
        ocr_words: List of PaddleOCR results [{text, box, score}, ...]
        fuzzy_threshold: Minimum similarity for fuzzy matching (0.0 - 1.0)
        y_band: Optional (min_y, max_y) to restrict search to a vertical row.
        x_target: Optional x-coordinate to break ties when multiple exact matches exist.

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

    # Filter words by y_band if provided
    filtered_words = []
    for word in ocr_words:
        if y_band:
            center_y = (word["box"][1] + word["box"][3]) / 2.0
            # Add a 15px margin of error for slight tilt or multiline alignment
            if not ((y_band[0] - 15) <= center_y <= (y_band[1] + 15)):
                continue
        filtered_words.append(word)

    if not filtered_words:
        return None
    
    ocr_words = filtered_words

    # Pre-compute stripped/lowered text once per word (avoids redundant
    # .strip().lower() calls across all 5 strategies)
    word_texts_lower = [w["text"].strip().lower() for w in ocr_words]
    word_texts_stripped = [w["text"].strip() for w in ocr_words]

    def _best_match(matches: list[dict]) -> dict:
        if not matches:
            return None
        if x_target is not None:
            # Pick the match whose center X is closest to the x_target
            return min(matches, key=lambda m: abs(((m["box"][0] + m["box"][2]) / 2.0) - x_target))
        return matches[0]

    # ── Strategy 1: Exact match (case-insensitive) ──
    exact_matches = []
    for idx, wl in enumerate(word_texts_lower):
        if wl == target_lower:
            exact_matches.append({
                "box": ocr_words[idx]["box"],
                "matched_text": ocr_words[idx]["text"],
                "matched_boxes": [ocr_words[idx]["box"]],
                "score": round(float(ocr_words[idx].get("score", 0)), 4),
                "strategy": "exact",
            })
    if exact_matches:
        return _best_match(exact_matches)

    # ── Strategy 2: Contains match ──
    contains_matches = []
    for idx, wl in enumerate(word_texts_lower):
        if not wl:
            continue

        # OCR span contains the extracted value
        if target_lower in wl:
            contains_matches.append({
                "box": ocr_words[idx]["box"],
                "matched_text": ocr_words[idx]["text"],
                "matched_boxes": [ocr_words[idx]["box"]],
                "score": round(float(ocr_words[idx].get("score", 0)), 4),
                "strategy": "contains",
                "diff": len(wl) - len(target_lower)
            })

        # Extracted value contains the OCR span (only for single words)
        elif len(target_words) == 1 and wl in target_lower and len(wl) >= len(target_lower) * 0.6:
            contains_matches.append({
                "box": ocr_words[idx]["box"],
                "matched_text": ocr_words[idx]["text"],
                "matched_boxes": [ocr_words[idx]["box"]],
                "score": round(float(ocr_words[idx].get("score", 0)), 4),
                "strategy": "contains",
                "diff": len(target_lower) - len(wl)
            })
            
    if contains_matches:
        # Group by smallest diff first, then pick best by x_target
        min_diff = min(m["diff"] for m in contains_matches)
        best_diff_matches = [m for m in contains_matches if m["diff"] == min_diff]
        best_match = _best_match(best_diff_matches)
        best_match.pop("diff")
        return best_match

    # ── Strategy 3: Multi-span match ──
    multi_span_matches = []
    for i in range(len(ocr_words)):
        combined = ""
        combined_lower = ""
        for j in range(i, min(i + 25, len(ocr_words))):
            sep = " " if combined else ""
            combined += sep + word_texts_stripped[j]
            combined_lower += sep + word_texts_lower[j]

            if len(combined) > len(target_lower) * 1.5 + 5:
                break

            if target_lower in combined_lower and len(combined) <= len(target_lower) + 5:
                span_boxes = [ocr_words[k]["box"] for k in range(i, j + 1)]
                if not _box_is_sane(span_boxes):
                    continue
                x0 = min(b[0] for b in span_boxes)
                y0 = min(b[1] for b in span_boxes)
                x1 = max(b[2] for b in span_boxes)
                y1 = max(b[3] for b in span_boxes)
                avg_score = sum(float(ocr_words[k].get("score", 0)) for k in range(i, j + 1)) / (j - i + 1)

                multi_span_matches.append({
                    "box": [x0, y0, x1, y1],
                    "matched_text": combined.strip(),
                    "matched_boxes": span_boxes,
                    "score": round(avg_score, 4),
                    "strategy": "multi_span",
                })
    if multi_span_matches:
        return _best_match(multi_span_matches)

    # ── Strategy 4: Anchor match (first + last words for long values) ──
    anchor_matches = []
    if len(target_words) >= 6:
        first_anchor = " ".join(target_words[:3]).lower()
        last_anchor = " ".join(target_words[-3:]).lower()

        first_idx = None
        last_idx = None

        for i in range(len(ocr_words)):
            combined = ""
            for j in range(i, min(i + 6, len(ocr_words))):
                combined = (combined + " " + ocr_words[j]["text"].strip()).strip()
                if first_anchor in combined.lower():
                    first_idx = i
                    break
            if first_idx is not None:
                break

        if first_idx is not None:
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
            if _box_is_sane(anchor_boxes, max_height_ratio=8.0):
                x0 = min(b[0] for b in anchor_boxes)
                y0 = min(b[1] for b in anchor_boxes)
                x1 = max(b[2] for b in anchor_boxes)
                y1 = max(b[3] for b in anchor_boxes)
                avg_score = sum(float(ocr_words[k].get("score", 0)) for k in range(first_idx, last_idx + 1)) / (last_idx - first_idx + 1)

                anchor_matches.append({
                    "box": [x0, y0, x1, y1],
                    "matched_text": " ".join(ocr_words[k]["text"].strip() for k in range(first_idx, last_idx + 1)),
                    "matched_boxes": anchor_boxes,
                    "score": round(avg_score, 4),
                    "strategy": "anchor",
                })
    if anchor_matches:
        return _best_match(anchor_matches)

    # ── Strategy 5: Fuzzy match (handles OCR typos) ──
    # Skip fuzzy entirely for short numeric values — Levenshtein is meaningless
    # for values like "1", "2.50", "EA" that appear hundreds of times in OCR
    _is_short_numeric = len(target) <= 4 and any(c.isdigit() for c in target)
    if _is_short_numeric:
        return None

    # Convert threshold from 0.0–1.0 to rapidfuzz's 0–100 scale
    cutoff_100 = fuzzy_threshold * 100.0

    # ── 5A: Single-word fuzzy via process.extractOne ──
    # This runs the ENTIRE comparison loop in C++ with score_cutoff for
    # early exit — no Python overhead per word. Uses a dict mapping so
    # extractOne returns the index as the key.
    candidates = {
        idx: wl for idx, wl in enumerate(word_texts_lower)
        if wl and len(wl) >= 3
    }
    if candidates:
        best = process.extractOne(
            target_lower,
            candidates,
            scorer=fuzz.ratio,
            score_cutoff=cutoff_100,
            processor=None,  # already lowercased
        )
        if best:
            # best = (matched_text, score, key/index)
            best_idx = best[2]
            sim = best[1] / 100.0
            return {
                "box": ocr_words[best_idx]["box"],
                "matched_text": word_texts_stripped[best_idx],
                "matched_boxes": [ocr_words[best_idx]["box"]],
                "score": round(float(ocr_words[best_idx].get("score", 0)), 4),
                "strategy": f"fuzzy({sim:.2f})",
            }

    # ── 5B: Multi-span fuzzy ──
    # Build candidate spans, then use fuzz.ratio with score_cutoff for
    # C++-level early exit on each comparison
    best_span = None
    best_span_sim = 0.0
    for i in range(len(ocr_words)):
        combined = ""
        combined_lower = ""
        for j in range(i, min(i + 25, len(ocr_words))):
            sep = " " if combined else ""
            combined += sep + word_texts_stripped[j]
            combined_lower += sep + word_texts_lower[j]

            if abs(len(combined) - len(target)) > len(target) * 0.3:
                if len(combined) > len(target):
                    break
                continue

            # score_cutoff makes rapidfuzz bail out early in C++ if it
            # detects the score cannot exceed the cutoff
            sim_100 = fuzz.ratio(
                target_lower, combined_lower,
                score_cutoff=cutoff_100,
            )
            if sim_100 > 0:
                sim = sim_100 / 100.0
                if sim > best_span_sim:
                    span_boxes = [ocr_words[k]["box"] for k in range(i, j + 1)]
                    if not _box_is_sane(span_boxes):
                        continue
                    x0 = min(b[0] for b in span_boxes)
                    y0 = min(b[1] for b in span_boxes)
                    x1 = max(b[2] for b in span_boxes)
                    y1 = max(b[3] for b in span_boxes)
                    avg_score = sum(float(ocr_words[k].get("score", 0)) for k in range(i, j + 1)) / (j - i + 1)
                    best_span_sim = sim
                    best_span = {
                        "box": [x0, y0, x1, y1],
                        "matched_text": combined.strip(),
                        "matched_boxes": span_boxes,
                        "score": round(avg_score, 4),
                        "strategy": f"fuzzy({sim:.2f})",
                    }

    if best_span:
        return best_span

    return None


# ---------------------------------------------------------------------------
# Confidence classification based on matching strategy
# ---------------------------------------------------------------------------

_HIGH_STRATEGIES = {"exact", "contains", "multi_span"}
_MEDIUM_STRATEGIES = {"anchor"}
# fuzzy(*) strategies → low


def _classify_confidence(strategy: str) -> str:
    """Return 'high', 'medium', or 'low' based on matching strategy."""
    base = strategy.split("(")[0]  # "fuzzy(0.87)" → "fuzzy"
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
      - Header fields  →  key = field_name  (e.g. "vendor_name")
      - Line item cells →  key = "line_item_{row}_{col}"  (e.g. "line_item_0_unit_price")

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

    # ── Stage 1: Header Fields ──
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

    # ── Stage 2: Line Items ──
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

    # Track consumed OCR box positions so duplicate values (e.g. two rows
    # with the same qty) map to DIFFERENT OCR locations.
    # Key: (page_number, tuple(box))
    used_boxes: set[tuple] = set()

    # Pre-build per-page word lists with pre-computed box tuples
    # to avoid calling tuple(w["box"]) 91,000+ times in the inner loop
    _page_words_cache: dict[int, list[tuple[dict, tuple]]] = {}
    for pd in ocr_pages:
        pn = pd["page_number"]
        _page_words_cache[pn] = [
            (w, (pn, tuple(w["box"])))
            for w in pd.get("words", [])
        ]

    def _get_available_words(page_num: int) -> list[dict]:
        """Return words not yet consumed, using pre-computed box keys."""
        return [w for w, key in _page_words_cache.get(page_num, []) if key not in used_boxes]

    if isinstance(line_items, list):
        row_y_bands = {}   # {row_idx: (min_y, max_y)}
        column_x_centers = {} # {col_name: list of x_centers}
        
        # Pass 2A: Anchors (Strings > 8 chars)
        for row_idx, row in enumerate(line_items):
            if not isinstance(row, dict):
                continue
            
            preferred_page = row_page_map.get(row_idx)
            search_pages = ocr_pages
            if preferred_page is not None:
                search_pages = sorted(ocr_pages, key=lambda p, pp=preferred_page: 0 if p["page_number"] == pp else 1)
            
            row_min_y = float('inf')
            row_max_y = float('-inf')
            
            for col_name, cell_value in row.items():
                if cell_value is None:
                    continue
                cell_str = str(cell_value).strip()
                if not cell_str or cell_str.lower() in ("null", "none", "n/a", "", "-"):
                    continue
                
                # Only process string anchors > 8 length in Pass 1
                if len(cell_str) <= 8 and not cell_str.isalpha():
                    continue

                li_total += 1
                composite_key = f"line_item_{row_idx}_{col_name}"
                
                for page_data in search_pages:
                    page_num = page_data["page_number"]
                    available_words = _get_available_words(page_num)
                    
                    location = find_value_in_ocr(cell_str, available_words)
                    if location:
                        matched_boxes = location.pop("matched_boxes", [location["box"]])
                        location["page"] = page_num
                        location["confidence"] = _classify_confidence(location["strategy"])
                        location["row_idx"] = row_idx
                        location["col_name"] = col_name
                        locations[composite_key] = location
                        li_matched += 1
                        
                        # Consume boxes
                        for box in matched_boxes:
                            used_boxes.add((page_num, tuple(box)))
                        
                        # Update Row Y-Band
                        b = location["box"]
                        row_min_y = min(row_min_y, b[1])
                        row_max_y = max(row_max_y, b[3])
                        
                        # Update Column X-Center
                        center_x = (b[0] + b[2]) / 2.0
                        column_x_centers.setdefault(col_name, []).append(center_x)
                        
                        break # Stop searching other pages
            
            if row_min_y != float('inf'):
                row_y_bands[row_idx] = (row_min_y, row_max_y)

        # Pre-compute X-Column median medians
        col_median_x = {}
        for col_name, x_list in column_x_centers.items():
            col_median_x[col_name] = sorted(x_list)[len(x_list)//2]

        # Pass 2B: Ambiguous numbers and short strings (<= 8 chars)
        for row_idx, row in enumerate(line_items):
            if not isinstance(row, dict):
                continue
            
            preferred_page = row_page_map.get(row_idx)
            search_pages = ocr_pages
            if preferred_page is not None:
                search_pages = sorted(ocr_pages, key=lambda p, pp=preferred_page: 0 if p["page_number"] == pp else 1)
            
            y_band = row_y_bands.get(row_idx)
            
            for col_name, cell_value in row.items():
                if cell_value is None:
                    continue
                cell_str = str(cell_value).strip()
                if not cell_str or cell_str.lower() in ("null", "none", "n/a", "", "-"):
                    continue
                
                # We already processed anchors > 8 length
                if len(cell_str) > 8 or cell_str.isalpha():
                    continue
                
                li_total += 1
                composite_key = f"line_item_{row_idx}_{col_name}"
                x_target = col_median_x.get(col_name)

                for page_data in search_pages:
                    page_num = page_data["page_number"]
                    available_words = _get_available_words(page_num)
                    
                    location = find_value_in_ocr(cell_str, available_words, y_band=y_band, x_target=x_target)
                    if location:
                        matched_boxes = location.pop("matched_boxes", [location["box"]])
                        location["page"] = page_num
                        location["confidence"] = _classify_confidence(location["strategy"])
                        location["row_idx"] = row_idx
                        location["col_name"] = col_name
                        locations[composite_key] = location
                        li_matched += 1
                        
                        # Consume boxes
                        for box in matched_boxes:
                            used_boxes.add((page_num, tuple(box)))
                        
                        # Update Column X-Center (for future rows if not established well)
                        center_x = (location["box"][0] + location["box"][2]) / 2.0
                        col_median_x.setdefault(col_name, center_x)
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
