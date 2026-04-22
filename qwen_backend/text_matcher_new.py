"""
text_matcher.py -- Match extracted JSON values to PaddleOCR boxes.

Design goals:
1) Qwen JSON is the source of truth for field names and values.
2) Row-first matching: find strong anchor values to establish row y-bands.
3) Column confirmation: validate columns via x-clustering + majority vote.
4) No hardcoded field-name weights or table header cues.
"""

from __future__ import annotations

from collections import defaultdict
import re
import time
from typing import Any

from rapidfuzz import fuzz, process

try:
    from .logging_config import get_logger
except ImportError:
    from logging_config import get_logger

logger = get_logger(__name__)

_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\S+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

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


def _normalize_field_key(key: str) -> str:
    key = str(key).lower()
    key = key.replace("_", " ")
    return _WS_RE.sub(" ", key).strip()


def _get_field_aliases(norm_key: str) -> list[str]:
    if norm_key == "qty":
        return ["qty", "quantity", "qty."]
    if norm_key == "uom":
        return ["uom", "u m", "u/m", "um"]
    return [norm_key]


# ---------------------------------------------------------------------------
# Box geometry helpers
# ---------------------------------------------------------------------------

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


def _int_box(box: Any) -> list[int] | None:
    if not _valid_box(box):
        return None
    return [int(round(float(v))) for v in box]


def _boxes_close(box_a: list[int] | None, box_b: list[int] | None, tol: int = 2) -> bool:
    if not (_valid_box(box_a) and _valid_box(box_b)):
        return False
    return all(abs(int(box_a[i]) - int(box_b[i])) <= tol for i in range(4))


def _box_contains(outer: list[int] | None, inner: list[int] | None, pad: int = 1) -> bool:
    if not (_valid_box(outer) and _valid_box(inner)):
        return False
    return (
        int(outer[0]) <= int(inner[0]) + pad
        and int(outer[1]) <= int(inner[1]) + pad
        and int(outer[2]) >= int(inner[2]) - pad
        and int(outer[3]) >= int(inner[3]) - pad
    )


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


# ---------------------------------------------------------------------------
# Token stream helpers (for token-chain matching inside find_value_in_ocr)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Core value search engine (5 strategies)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Confidence classification
# ---------------------------------------------------------------------------

_HIGH_STRATEGIES = {"exact", "contains", "contains_subspan", "token_chain"}
_MEDIUM_STRATEGIES = {"multi_span"}


def _classify_confidence(strategy: str) -> str:
    base = strategy.split("(")[0]
    if base in _HIGH_STRATEGIES:
        return "high"
    if base in _MEDIUM_STRATEGIES:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    val = str(value).strip()
    return not val or val.lower() in {"null", "none", "n/a", "-"}


def _split_value_lines(value: str) -> list[str]:
    return [ln.strip() for ln in re.split(r"[\r\n]+", str(value)) if ln and ln.strip()]



def _median_int(values: list[float | int]) -> int:
    if not values:
        return 0
    ordered = sorted(float(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return int(round(ordered[mid]))
    return int(round((ordered[mid - 1] + ordered[mid]) / 2.0))


# ---------------------------------------------------------------------------
# OCR page preparation
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------

def _box_from_location(location: dict[str, Any]) -> list[int] | None:
    box = location.get("box")
    if _valid_box(box):
        return [int(round(float(v))) for v in box]
    return None


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Column-Based Line Item Matching
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_ANCHOR_LETTER_RE = re.compile(r"[a-zA-Z]")
_ANCHOR_DIGIT_RE = re.compile(r"\d")
_ANCHOR_SPECIAL_RE = re.compile(r"[/\-_#]")


def _anchor_strength(value: str, freq: int = 1) -> int:
    """
    Rate a cell value's intrinsic uniqueness for row anchoring.

    Returns:
      3 = strong  (mixed alpha-numeric codes, hyphenated IDs, pack strings)
      2 = medium  (longer alpha strings, unique numerics >= 3 chars)
      1 = weak    (short repeated numbers, common tokens)
      0 = skip    (empty / null)
    """
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "n/a", "-"):
        return 0

    has_letter = bool(_ANCHOR_LETTER_RE.search(s))
    has_digit = bool(_ANCHOR_DIGIT_RE.search(s))
    has_special = bool(_ANCHOR_SPECIAL_RE.search(s))
    length = len(s)

    # Penalize highly repeated values
    freq_penalty = 0
    if freq > 5:
        freq_penalty = 2
    elif freq > 3:
        freq_penalty = 1

    # Mixed alpha-numeric with some length: very strong anchor
    if has_letter and has_digit and length >= 5:
        return max(1, 3 - freq_penalty)

    # Has special chars (/, -, #) with some length: strong
    if has_special and length >= 4:
        return max(1, 3 - freq_penalty)

    # Pure alpha, long enough: medium anchor
    if has_letter and not has_digit and length >= 6:
        return max(1, 2 - freq_penalty)

    # Unique numeric with enough digits: medium
    if has_digit and not has_letter and freq == 1 and length >= 3:
        return max(1, 2 - freq_penalty)

    # Everything else: weak
    return 1


def _compute_value_frequencies(
    page_items: list[tuple[int, dict]],
    column_names: list[str],
) -> dict[str, dict[str, int]]:
    """Count how often each value appears in each column on this page."""
    freq: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _, row in page_items:
        if not isinstance(row, dict):
            continue
        for col in column_names:
            val = row.get(col)
            if not _is_empty_value(val):
                freq[col][str(val).strip()] += 1
    return dict(freq)


def _get_column_names(line_items: list[dict]) -> list[str]:
    """Get union of all column names across all non-empty line items, preserving first-seen order."""
    columns: dict[str, int] = {}
    for row in line_items:
        if not isinstance(row, dict):
            continue
        for key in row:
            if key not in columns:
                columns[key] = len(columns)
    return sorted(columns, key=lambda k: columns[k])


def _group_items_by_page(
    line_items: list[dict],
    row_page_map: dict[int, int],
) -> dict[int, list[tuple[int, dict]]]:
    """Group Qwen line items by page number.  Default page = 1."""
    groups: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    for idx, row in enumerate(line_items):
        if not isinstance(row, dict):
            continue
        page = row_page_map.get(idx, 1)
        groups[page].append((idx, row))
    return dict(groups)


def _strategy_priority(strategy: str) -> int:
    base = str(strategy or "").split("(")[0]
    if base == "exact":
        return 4
    if base in {"token_chain", "contains", "contains_subspan"}:
        return 3
    if base == "multi_span":
        return 2
    if base == "fuzzy":
        return 1
    return 0


def _find_page_column_headers(
    column_names: list[str],
    page_words: list[dict],
    ocr_lines: list[dict],
) -> tuple[dict[str, dict], list[int] | None]:
    """
    Find the best table-header window for this page using grouped evaluation.

    Evaluates both 1-line and 2-line candidate windows. Each window is scored
    by the number of distinct column matches, horizontal spread, and position.
    Short single-token matches (≤3 chars) are only counted when the window
    also has ≥2 other column matches, preventing false positives from
    administrative rows like 'Purchase Order No.' or 'Account No.'.

    Returns:
      (header_map, header_line_box)
      header_map   : {col_name: location_dict} for matched columns
      header_line_box : [x0, y0, x1, y1] of the selected header window
    """
    if not column_names or not page_words or not ocr_lines:
        return {}, None

    # Step 1: match each column against each OCR line independently
    line_candidates: dict[int, dict[str, dict]] = defaultdict(dict)
    line_boxes: dict[int, list[int]] = {}

    for line in ocr_lines:
        line_idx = int(line.get("index", -1))
        line_words = line.get("words", [])
        if line_idx < 0 or not line_words:
            continue
        line_boxes[line_idx] = [int(v) for v in line.get("box", [0, 0, 0, 0])]

        for col_name in column_names:
            best_match: dict | None = None
            best_variant: str | None = None
            best_key: tuple[int, int, int] | None = None

            for variant in _generate_header_variants(col_name):
                location = find_value_in_ocr(variant, line_words, fuzzy_threshold=0.70)
                if not location:
                    continue

                variant_len = len(_normalize_token_loose(variant))
                word_count = max(1, len(variant.split()))
                key = (
                    _strategy_priority(location.get("strategy", "")),
                    word_count,
                    variant_len,
                )
                if best_key is None or key > best_key:
                    best_key = key
                    best_match = dict(location)
                    best_variant = variant

            if best_match is None:
                continue

            best_match["header_variant"] = best_variant
            best_match["line_index"] = line_idx
            best_match["line_box"] = line_boxes[line_idx][:]
            line_candidates[line_idx][col_name] = best_match

    if not line_candidates:
        return {}, None

    # Step 2: Build candidate windows (1-line and 2-line)
    sorted_line_indices = sorted(line_candidates.keys())
    windows: list[tuple[list[int], dict[str, dict], list[int]]] = []
    # (line_indices, merged_matches, window_box)

    for i, idx in enumerate(sorted_line_indices):
        # 1-line window
        matches_1 = dict(line_candidates[idx])
        box_1 = line_boxes.get(idx, [0, 0, 0, 0])
        windows.append(([idx], matches_1, box_1[:]))

        # 2-line window (merge with next adjacent line)
        if i + 1 < len(sorted_line_indices):
            next_idx = sorted_line_indices[i + 1]
            next_box = line_boxes.get(next_idx, [0, 0, 0, 0])
            # Only merge if lines are vertically close (within ~40px)
            if _valid_box(box_1) and _valid_box(next_box):
                gap = abs(next_box[1] - box_1[3])
                if gap < 40:
                    merged = dict(line_candidates[idx])
                    # Add columns from the next line (don't overwrite existing)
                    for col, match in line_candidates[next_idx].items():
                        if col not in merged:
                            merged[col] = match
                    merged_box = _box_union([box_1, next_box])
                    windows.append(([idx, next_idx], merged, merged_box))

    # Step 3: Score each window
    def _window_score(window):
        line_indices, matches, wbox = window
        n_cols = len(matches)

        # Count short-token-only matches (≤3 chars in the matched variant)
        short_only = sum(
            1 for m in matches.values()
            if len(_normalize_token_loose(m.get("header_variant", ""))) <= 3
        )
        long_matches = n_cols - short_only

        # Hard rule: short tokens are invalid unless accompanied by ≥2 longer matches
        if short_only > 0 and long_matches < 2:
            # Demote: only count the long matches
            effective_cols = long_matches
        else:
            effective_cols = n_cols

        # Minimum threshold: need at least 2 effective column matches
        if effective_cols < 2:
            return (-1, 0, 0, 0)

        # Horizontal spread
        valid_boxes = [_box_from_location(m) for m in matches.values()]
        valid_boxes = [b for b in valid_boxes if _valid_box(b)]
        span = (max(b[2] for b in valid_boxes) - min(b[0] for b in valid_boxes)) if valid_boxes else 0

        # Position: prefer higher on page (lower y = earlier in document)
        # but not too high (avoid document title lines)
        y_pos = wbox[1] if _valid_box(wbox) else 10**9

        return (effective_cols, span, -y_pos, min(line_indices))

    if not windows:
        return {}, None

    best_window = max(windows, key=_window_score)
    best_score = _window_score(best_window)

    # If the best window didn't meet minimum thresholds, return nothing
    if best_score[0] < 2:
        return {}, None

    _, best_matches, best_box = best_window

    # Filter out short-only matches from the result if they were demoted
    short_only = sum(
        1 for m in best_matches.values()
        if len(_normalize_token_loose(m.get("header_variant", ""))) <= 3
    )
    long_matches = len(best_matches) - short_only
    if short_only > 0 and long_matches < 2:
        # Remove short-token matches
        best_matches = {
            col: m for col, m in best_matches.items()
            if len(_normalize_token_loose(m.get("header_variant", ""))) > 3
        }

    return dict(best_matches), best_box if _valid_box(best_box) else None


def _find_row_anchors(
    page_items: list[tuple[int, dict]],
    page_words: list[dict],
    column_names: list[str],
    value_freq: dict[str, dict[str, int]],
    min_y: float | None = None,
) -> dict[int, dict]:
    """
    For each Qwen row on this page, find the strongest anchor value
    and search it in PaddleOCR.  Enforce monotonic y-order and track
    consumed OCR boxes so the same word is not reused.

    Returns:
      {row_idx: {y_center, box, location, anchor_col, anchor_value}}
    """
    anchors: dict[int, dict] = {}
    last_matched_y = -float("inf")
    used_boxes: set[tuple] = set()

    for row_idx, row in page_items:
        if not isinstance(row, dict):
            continue

        # Rank all cell values by anchor strength
        candidates: list[tuple[int, str, str]] = []
        for col in column_names:
            val = row.get(col)
            if _is_empty_value(val):
                continue
            s = str(val).strip()
            freq = value_freq.get(col, {}).get(s, 1)
            strength = _anchor_strength(s, freq)
            if strength >= 1:
                candidates.append((strength, col, s))

        # Sort: strongest first, then longest (more distinctive)
        candidates.sort(key=lambda c: (-c[0], -len(c[2])))

        # Prefer medium+ anchors; fall back to weak only if no medium+ exist
        strong_candidates = [c for c in candidates if c[0] >= 2]
        search_list = strong_candidates if strong_candidates else candidates

        for _strength, col, val in search_list:
            # Exclude already-consumed OCR boxes
            available_words = [
                w for w in page_words
                if min_y is None or _box_center_y(w["box"]) >= (min_y - 4)
                if tuple(w["box"]) not in used_boxes
            ]
            location = find_value_in_ocr(val, available_words, fuzzy_threshold=0.85)
            if not location:
                continue
            box = location.get("box")
            if not _valid_box(box):
                continue

            y_center = _box_center_y(box)

            # Monotonic: each row must be below the previous matched row
            if y_center < last_matched_y - 5:
                continue

            # Accept this anchor
            int_box = [int(round(float(v))) for v in box]
            anchors[row_idx] = {
                "y_center": y_center,
                "box": int_box,
                "location": location,
                "anchor_col": col,
                "anchor_value": val,
            }
            last_matched_y = y_center

            # Consume matched boxes so they cannot be reused for another row
            for mb in location.get("matched_boxes", [int_box]):
                used_boxes.add(tuple(mb))
            break

    return anchors


def _build_row_bands(
    row_anchors: dict[int, dict],
    ocr_lines: list[dict],
    page_items: list[tuple[int, dict]],
) -> dict[int, tuple[float, float]]:
    """
    Build y-bands [y_min, y_max] for each Qwen row.

    Matched rows use their anchor y_center.
    Unmatched rows are linearly interpolated from neighbors.
    Band boundaries are split at the midpoint between consecutive rows.
    """
    if not page_items:
        return {}

    all_row_indices = [ri for ri, _ in page_items]
    n = len(all_row_indices)

    # â”€â”€ Compute median row spacing â”€â”€
    if len(row_anchors) >= 2:
        matched = sorted(
            (
                (i, row_anchors[ri]["y_center"])
                for i, ri in enumerate(all_row_indices) if ri in row_anchors
            ),
            key=lambda x: x[0],
        )
        gaps = []
        for j in range(1, len(matched)):
            idx_diff = matched[j][0] - matched[j - 1][0]
            y_diff = matched[j][1] - matched[j - 1][1]
            if idx_diff > 0 and y_diff > 0:
                gaps.append(y_diff / idx_diff)
        row_spacing = _median_int(gaps) if gaps else 20
    elif ocr_lines:
        row_spacing = _median_int([ln.get("avg_height", 15) for ln in ocr_lines])
    else:
        row_spacing = 20
    row_spacing = max(10, row_spacing)

    # â”€â”€ Determine y_center for every row â”€â”€
    y_centers: dict[int, float] = {}
    for ri in row_anchors:
        y_centers[ri] = row_anchors[ri]["y_center"]

    for i, ri in enumerate(all_row_indices):
        if ri in y_centers:
            continue
        # Find nearest matched row above and below
        above_y, above_dist = None, None
        below_y, below_dist = None, None

        for j in range(i - 1, -1, -1):
            ari = all_row_indices[j]
            if ari in y_centers:
                above_y = y_centers[ari]
                above_dist = i - j
                break

        for j in range(i + 1, n):
            bri = all_row_indices[j]
            if bri in y_centers:
                below_y = y_centers[bri]
                below_dist = j - i
                break

        if above_y is not None and below_y is not None:
            total = above_dist + below_dist
            y_centers[ri] = above_y + (below_y - above_y) * (above_dist / total)
        elif above_y is not None:
            y_centers[ri] = above_y + above_dist * row_spacing
        elif below_y is not None:
            y_centers[ri] = below_y - below_dist * row_spacing
        else:
            y_centers[ri] = i * row_spacing

    # â”€â”€ Build bands: split space between consecutive rows â”€â”€
    half_h = row_spacing * 0.65
    bands: dict[int, tuple[float, float]] = {}

    for idx_pos, ri in enumerate(all_row_indices):
        yc = y_centers.get(ri, 0)

        # Upper bound: midpoint to previous row, capped at half_h
        if idx_pos > 0:
            prev_ri = all_row_indices[idx_pos - 1]
            prev_yc = y_centers.get(prev_ri, yc - row_spacing)
            upper = max(yc - half_h, (prev_yc + yc) / 2.0)
        else:
            upper = yc - half_h

        # Lower bound: midpoint to next row, capped at half_h
        if idx_pos < n - 1:
            next_ri = all_row_indices[idx_pos + 1]
            next_yc = y_centers.get(next_ri, yc + row_spacing)
            lower = min(yc + half_h, (yc + next_yc) / 2.0)
        else:
            lower = yc + half_h

        bands[ri] = (upper, lower)

    return bands


def _confirm_column(
    col_name: str,
    page_items: list[tuple[int, dict]],
    row_bands: dict[int, tuple[float, float]],
    page_words: list[dict],
    page_num: int,
    x_target: float | None = None,
    x_tolerance: float | None = None,
    blocked_boxes_by_row: dict[int, set[tuple[int, int, int, int]]] | None = None,
) -> tuple[bool, list[int] | None, dict[int, dict]]:
    """
    For one column on one page, search each row's cell value inside its
    row band, then confirm the column via x-clustering + majority vote.

    Returns:
      (confirmed, col_x_band, cell_hits)
      confirmed  : True if dominant x-cluster has > total//2 hits
      col_x_band : [x_min, x_max] of confirmed column, or None
      cell_hits  : {row_idx: location_dict} for rows that matched
    """
    total = 0
    cell_hits: dict[int, dict] = {}
    hit_x_ranges: list[tuple[int, int, int]] = []  # (x0, x1, row_idx)

    for row_idx, row in page_items:
        if not isinstance(row, dict):
            continue
        val = row.get(col_name)
        if _is_empty_value(val):
            continue

        cell_str = str(val).strip()
        total += 1

        rb = row_bands.get(row_idx)
        if not rb:
            continue

        # Filter words to this row's y-band
        blocked = blocked_boxes_by_row.get(row_idx, set()) if blocked_boxes_by_row else set()
        band_words = [
            w for w in page_words
            if rb[0] - 5 <= _box_center_y(w["box"]) <= rb[1] + 5
            if tuple(w["box"]) not in blocked
        ]
        if not band_words:
            continue

        location = find_value_in_ocr(
            cell_str,
            band_words,
            fuzzy_threshold=0.85,
            x_target=x_target,
        )
        if not location:
            continue

        box = location.get("box")
        if not _valid_box(box):
            continue

        if x_target is not None:
            tol = float(x_tolerance) if x_tolerance is not None else 70.0
            if abs(_box_center_x(box) - float(x_target)) > tol:
                continue

        cell_hits[row_idx] = location
        hit_x_ranges.append((box[0], box[2], row_idx))

    if total == 0:
        return False, None, {}

    threshold = total // 2
    found_count = len(cell_hits)

    if found_count <= threshold:
        return False, None, cell_hits

    if not hit_x_ranges:
        return False, None, cell_hits

    # â”€â”€ X-clustering: verify hits form a tight vertical column â”€â”€
    all_x0 = [x0 for x0, _, _ in hit_x_ranges]
    all_x1 = [x1 for _, x1, _ in hit_x_ranges]

    col_x0 = _median_int(all_x0)
    col_x1 = _median_int(all_x1)
    col_width = max(10, col_x1 - col_x0)

    # All hits should be within ~2.5x the median column width
    actual_spread = max(all_x1) - min(all_x0)
    max_spread = col_width * 2.5 + 30

    if actual_spread > max_spread:
        logger.debug(
            "Column '%s' page %d: x-spread %.0f > max %.0f -- not confirmed",
            col_name, page_num, actual_spread, max_spread,
        )
        return False, None, cell_hits

    # â”€â”€ Monotonic y-order check â”€â”€
    sorted_hits = sorted(hit_x_ranges, key=lambda h: cell_hits[h[2]]["box"][1])
    hit_row_indices = [h[2] for h in sorted_hits]

    violations = 0
    for j in range(1, len(hit_row_indices)):
        if hit_row_indices[j] < hit_row_indices[j - 1]:
            violations += 1

    max_violations = max(1, len(hit_row_indices) // 4)
    if violations > max_violations:
        logger.debug(
            "Column '%s' page %d: %d monotonic violations (max %d) -- not confirmed",
            col_name, page_num, violations, max_violations,
        )
        return False, None, cell_hits

    col_band = [min(all_x0), max(all_x1)]
    logger.debug(
        "Column '%s' page %d: CONFIRMED (%d/%d hits, x-band=[%d,%d])",
        col_name, page_num, found_count, total, col_band[0], col_band[1],
    )
    return True, col_band, cell_hits


def _generate_header_variants(col_name: str) -> list[str]:
    """
    Generate search variants for a column name.
    "ship_qty" -> ["ship qty", "ship", "qty", "shipqty", "Ship Qty"]
    """
    variants: list[str] = []
    
    # 1. Base aliases (like qty -> quantity)
    norm = _normalize_field_key(col_name)
    variants.extend(_get_field_aliases(norm))

    # 2. Spaced versions
    spaced = col_name.replace("_", " ")
    variants.append(spaced)

    # 3. Individual tokens
    tokens = spaced.split()
    for token in tokens:
        if len(token) >= 2:
            variants.append(token)

    # 4. Without underscores
    variants.append(col_name.replace("_", ""))
    variants.append(spaced.title())

    # Deduplicate preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for v in variants:
        vl = v.lower()
        if vl not in seen:
            seen.add(vl)
            deduped.append(v)
    return deduped


def _find_column_header_in_ocr(
    col_name: str,
    page_words: list[dict],
    col_x_band: list[int],
    data_top_y: float,
) -> dict | None:
    """
    Search upward near the column x-band for the header label.

    Looks for words ABOVE the data area whose x-center is near
    the column's x-band.  Returns a location dict or None.
    """
    if not page_words or not col_x_band:
        return None

    col_x_center = (col_x_band[0] + col_x_band[1]) / 2.0
    x_tolerance = max(60, (col_x_band[1] - col_x_band[0]) * 1.5)

    # Words above data area and near column x
    header_candidates = [
        w for w in page_words
        if _box_center_y(w["box"]) < data_top_y
        and abs(_box_center_x(w["box"]) - col_x_center) < x_tolerance
    ]

    if not header_candidates:
        return None

    for variant in _generate_header_variants(col_name):
        location = find_value_in_ocr(variant, header_candidates, fuzzy_threshold=0.70)
        if location:
            location["header_variant"] = variant
            return location

    return None


def _build_column_anchor_box(
    col_header_box: list[int] | None,
    col_band: list[int] | None,
    header_line_box: list[int] | None,
) -> list[int] | None:
    """
    Final review anchor for a line-item column.

    The user wants line-item mappings to point to the table column/header,
    not to individual row values. So `loc.box` for line items must resolve
    to a header-level anchor box only.
    """
    if _valid_box(col_header_box):
        return [int(v) for v in col_header_box]
    if _valid_box(header_line_box) and col_band and len(col_band) == 2:
        synthetic = [
            int(col_band[0]),
            int(header_line_box[1]),
            int(col_band[1]),
            int(header_line_box[3]),
        ]
        if _valid_box(synthetic):
            return synthetic
    return None


def _reserve_source_boxes_for_hit(
    location: dict[str, Any],
    reserved_boxes: set[tuple[int, int, int, int]],
) -> None:
    """
    Reserve only the OCR source boxes that were truly consumed.

    For subspan matches inside a larger OCR word (for example "45" and "CS"
    inside one OCR token "45 CS"), reserving the entire source box causes the
    sibling column to disappear. In that case we keep the source box reusable
    so the other column can match its own subspan.
    """
    value_box = _int_box(location.get("box"))
    if value_box is None:
        return

    matched_boxes = location.get("matched_boxes", [])
    if not matched_boxes:
        reserved_boxes.add(tuple(value_box))
        return

    for matched_box in matched_boxes:
        source_box = _int_box(matched_box)
        if source_box is None:
            continue
        # Reserve the full OCR box only when the match consumed that box.
        if _boxes_close(source_box, value_box) or not _box_contains(source_box, value_box, pad=2):
            reserved_boxes.add(tuple(source_box))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Main entry point
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def compute_field_locations(
    extraction_result: dict[str, Any],
    ocr_pages: list[dict],
    page_results: list[dict] | None = None,
) -> dict[str, dict]:
    """
    Match extracted fields to OCR boxes.

    Header keys  :  "vendor", "bill_to", â€¦
    Line item keys:  "line_item_{row}_{column}"

    Algorithm:
      Stage 1 -- Headers: exact -> token_chain -> contains -> fuzzy
      Stage 2 -- Line items (column-based):
        a) Row anchoring  : find strong values -> establish row y-positions
        b) Row bands      : interpolate missing rows from neighbors
        c) Column confirm : search values inside row bands, x-cluster + vote
        d) Header search  : find header labels above confirmed columns
        e) Emit boxes     : found cells get real box, unfound get row âˆ© col band
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

    # â”€â”€ Stage 1: Headers (exact -> token_chain -> contains -> fuzzy) â”€â”€
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

        norm_key = _normalize_field_key(field_name)
        key_variants = _get_field_aliases(norm_key)

        # ── Stage 1a: Find ALL key label locations across pages ──
        key_hits: list[dict] = []  # [{box, page, score, matched_text}]
        for page_data in ocr_pages:
            page_words = page_data.get("words", [])
            page_num_local = page_data.get("page_number", 1)
            for variant in key_variants:
                loc = find_value_in_ocr(variant, page_words, fuzzy_threshold=0.85)
                if loc and _valid_box(loc.get("box")):
                    key_hits.append({
                        "box": loc["box"],
                        "page": page_num_local,
                        "score": loc.get("score", 0),
                        "matched_text": loc.get("matched_text", ""),
                    })

        # ── Stage 1b: Find ALL value candidate locations across pages ──
        # For multiline values, search each line independently first,
        # then group contiguous hits into value blocks.
        value_candidates: list[dict] = []  # [{box, page, line_hits, score}]
        for page_data in ocr_pages:
            page_words = page_data.get("words", [])
            page_num_local = page_data.get("page_number", 1)
            page_line_hits: list[dict] = []
            for line in lines:
                location = find_value_in_ocr(line, page_words)
                if location and _valid_box(location.get("box")):
                    location["page"] = page_num_local
                    location["confidence"] = _classify_confidence(location["strategy"])
                    location.pop("matched_boxes", None)
                    page_line_hits.append(location)

            if page_line_hits:
                # Build a single value block from all line hits on this page
                all_boxes = [h["box"] for h in page_line_hits]
                union_box = _box_union(all_boxes)
                avg_score = sum(h.get("score", 0) for h in page_line_hits) / len(page_line_hits)
                value_candidates.append({
                    "box": union_box,
                    "page": page_num_local,
                    "line_hits": page_line_hits,
                    "score": avg_score,
                    "lines_found": len(page_line_hits),
                    "lines_total": len(lines),
                })

        if not value_candidates:
            missed.append(field_name)
            continue

        # ── Stage 1c: Score key/value pairings ──
        # Pick the value block that is (a) spatially closest to a key label,
        # (b) in a plausible geometric relationship (below or right of key),
        # (c) on the same page as the key, (d) has the most matched lines.
        best_value: dict | None = None
        best_pair_score: float = -1.0

        for vc in value_candidates:
            vc_box = vc["box"]
            vc_page = vc["page"]
            base_score = vc["lines_found"] / max(1, vc["lines_total"])  # completeness

            if key_hits:
                for kh in key_hits:
                    kbox = kh["box"]
                    kpage = kh["page"]

                    # Same page bonus
                    if kpage != vc_page:
                        continue

                    # Spatial distance (center-to-center)
                    kx, ky = _box_center_x(kbox), _box_center_y(kbox)
                    vx, vy = _box_center_x(vc_box), _box_center_y(vc_box)
                    dist = ((kx - vx) ** 2 + (ky - vy) ** 2) ** 0.5

                    # Geometry preference: value below key or to the right
                    geo_bonus = 0.0
                    if vy > ky:  # below
                        geo_bonus = 0.3
                    elif vx > kx and abs(vy - ky) < 30:  # to the right, same row
                        geo_bonus = 0.2

                    # Distance penalty (closer is better)
                    dist_score = max(0.0, 1.0 - dist / 800.0)

                    pair_score = base_score + geo_bonus + dist_score
                    if pair_score > best_pair_score:
                        best_pair_score = pair_score
                        best_value = vc
            else:
                # No key found — just use completeness + value score
                pair_score = base_score + vc["score"]
                if pair_score > best_pair_score:
                    best_pair_score = pair_score
                    best_value = vc

        if best_value:
            primary = dict(best_value["line_hits"][0])
            primary["page"] = best_value["page"]
            same_page_hits = best_value["line_hits"]
            if len(same_page_hits) > 1:
                primary["box"] = _box_union([h["box"] for h in same_page_hits])
                primary["sub_locations"] = [
                    {
                        "page": h["page"],
                        "box": h["box"],
                        "matched_text": h.get("matched_text", ""),
                        "strategy": h.get("strategy", ""),
                        "confidence": h.get("confidence", "low"),
                    }
                    for h in same_page_hits[1:]
                ]
            locations[field_name] = primary
            found = True
            logger.debug(
                "Matched %s='%s' via %s (%s) [key_hits=%d, val_candidates=%d]",
                field_name,
                lines[0][:30],
                primary.get("strategy", "?"),
                primary.get("confidence", "?"),
                len(key_hits),
                len(value_candidates),
            )

        if not found:
            missed.append(field_name)

    # â”€â”€ Stage 2: Line items (column-based matching) â”€â”€
    li_total = 0
    li_matched = 0

    # 2a. Build row -> page map from page_results
    row_page_map: dict[int, int] = {}
    if page_results:
        running_idx = 0
        for pr in page_results:
            page_num = pr.get("_page", 1)
            page_items_list = pr.get("line_items", [])
            if isinstance(page_items_list, list):
                for _ in page_items_list:
                    row_page_map[running_idx] = page_num
                    running_idx += 1

    if isinstance(line_items, list) and line_items:
        # 2b. Get column names from union of all rows
        column_names = _get_column_names(line_items)

        # Count total non-empty cells
        li_total = sum(
            1
            for row in line_items
            if isinstance(row, dict)
            for col in column_names
            for value in [row.get(col)]
            if not _is_empty_value(value)
        )

        # 2c. Group items by page
        page_groups = _group_items_by_page(line_items, row_page_map)

        # 2d. Prepare per-page word lists
        page_words_map: dict[int, list[dict]] = {}
        for pd in ocr_pages:
            pn = int(pd.get("page_number", 0) or 0)
            page_words_map[pn] = _prepare_page_words(pd.get("words", []))

        # 2e. Process each page
        for page_num, page_items in sorted(page_groups.items()):
            page_words = page_words_map.get(page_num, [])
            if not page_words or not page_items:
                continue

            ocr_lines = _build_ocr_lines(page_words)
            page_header_map, header_line_box = _find_page_column_headers(
                column_names,
                page_words,
                ocr_lines,
            )

            # Compute per-column value frequencies for anchor strength
            value_freq = _compute_value_frequencies(page_items, column_names)

            # 2f. Find row anchors (strong values -> establish y-positions)
            anchor_min_y = float(header_line_box[3] + 4) if _valid_box(header_line_box) else None
            row_anchors = _find_row_anchors(
                page_items,
                page_words,
                column_names,
                value_freq,
                min_y=anchor_min_y,
            )
            logger.debug(
                "Page %d: %d/%d rows anchored",
                page_num, len(row_anchors), len(page_items),
            )

            # 2g. Build row bands (interpolate missing rows)
            row_bands = _build_row_bands(row_anchors, ocr_lines, page_items)
            if not row_bands:
                continue

            # Data top y (min y of all row bands) for header search
            data_top_y = (
                float(header_line_box[3] + 4)
                if _valid_box(header_line_box)
                else min(rb[0] for rb in row_bands.values())
            )

            column_order = {name: idx for idx, name in enumerate(column_names)}
            ordered_columns = sorted(
                column_names,
                key=lambda name: (
                    0 if name in page_header_map else 1,
                    page_header_map[name]["box"][0] if name in page_header_map and _valid_box(page_header_map[name].get("box")) else 10**9,
                    column_order[name],
                ),
            )
            reserved_boxes_by_row: dict[int, set[tuple[int, int, int, int]]] = defaultdict(set)

            # 2h. Confirm each column via x-clustering + majority vote
            for col_name in ordered_columns:
                col_header = page_header_map.get(col_name)
                col_header_box = _box_from_location(col_header) if col_header else None
                x_target = _box_center_x(col_header_box) if col_header_box else None
                x_tolerance = None
                if col_header_box:
                    header_width = max(10, col_header_box[2] - col_header_box[0])
                    # Short labels like "LINE" / "UOM" often land slightly off
                    # from the data beneath them, especially when OCR splits or
                    # merges the header text differently per page.
                    x_tolerance = max(60.0, header_width * 2.5)

                confirmed, col_band, cell_hits = _confirm_column(
                    col_name,
                    page_items,
                    row_bands,
                    page_words,
                    page_num,
                    x_target=x_target,
                    x_tolerance=x_tolerance,
                    blocked_boxes_by_row=reserved_boxes_by_row,
                )

                # Search for column header in OCR (above data area)
                if confirmed and col_band and col_header_box is None:
                    col_header = _find_column_header_in_ocr(
                        col_name, page_words, col_band, data_top_y,
                    )
                    if col_header:
                        col_header.pop("matched_boxes", None)
                        col_header_box = _box_from_location(col_header)

                column_anchor_box = _build_column_anchor_box(
                    col_header_box,
                    col_band,
                    header_line_box,
                )

                if confirmed:
                    for row_idx, location in cell_hits.items():
                        value_box = _box_from_location(location)
                        if value_box and _valid_box(value_box):
                            reserved_boxes_by_row[row_idx].add(tuple(value_box))
                        _reserve_source_boxes_for_hit(
                            location,
                            reserved_boxes_by_row[row_idx],
                        )

                # 2i. Emit locations for this column's cells
                for row_idx, row in page_items:
                    if not isinstance(row, dict):
                        continue
                    val = row.get(col_name)
                    if _is_empty_value(val):
                        continue

                    key_name = f"line_item_{row_idx}_{col_name}"

                    if not confirmed:
                        continue

                    # If column is confirmed but no header label was found,
                    # synthesize an anchor from the column x-band + topmost cell hit
                    if column_anchor_box is None and col_band:
                        top_y = None
                        for _ri, _loc in sorted(cell_hits.items()):
                            _b = _box_from_location(_loc)
                            if _b and _valid_box(_b):
                                top_y = _b[1]
                                break
                        if top_y is not None:
                            column_anchor_box = [col_band[0], max(0, top_y - 20), col_band[1], top_y]

                    if column_anchor_box is None:
                        continue

                    rb = row_bands.get(row_idx)
                    if row_idx in cell_hits:
                        # â”€â”€ Column confirmed: map this row to the column/header only â”€â”€
                        location = cell_hits[row_idx]
                        value_box = _box_from_location(location)
                        if not value_box:
                            continue

                        strategy = location.get("strategy", "")
                        is_fuzzy = strategy.startswith("fuzzy")

                        locations[key_name] = {
                            "page": page_num,
                            "box": column_anchor_box[:],
                            "matched_text": location.get("matched_text", ""),
                            "score": location.get("score", 0.0),
                            "strategy": strategy,
                            "confidence": _classify_confidence(strategy),
                            "row_index": row_idx,
                            "field_name": col_name,
                            "match_mode": "column_fuzzy" if is_fuzzy else "column_exact",
                            "value_box": value_box[:],
                            "column_header_box": column_anchor_box[:],
                            "column_band_box": list(col_band) if col_band else None,
                            "row_band_box": list(rb) if rb else None,
                        }
                        li_matched += 1
                    else:
                        # â”€â”€ Column confirmed: unfound row still maps to the same column/header â”€â”€
                        locations[key_name] = {
                            "page": page_num,
                            "box": column_anchor_box[:],
                            "matched_text": "",
                            "score": 0.0,
                            "strategy": "column_inferred",
                            "confidence": "low",
                            "row_index": row_idx,
                            "field_name": col_name,
                            "match_mode": "column_inferred",
                            "value_box": None,
                            "column_header_box": column_anchor_box[:],
                            "column_band_box": list(col_band) if col_band else None,
                            "row_band_box": list(rb) if rb else None,
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
