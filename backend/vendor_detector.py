"""
vendor_detector.py - Detect vendor from page-1 text using alias matching.

The detector always uses current-document text. It prefers explicit aliases
from vendor_aliases, and falls back to normalized vendor names/ids when aliases
have not been seeded yet.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import db as db_mod

logger = logging.getLogger("vendor_detector")

MIN_SCORE = 1
FUZZY_MIN_SCORE = 88.0
FUZZY_MIN_MARGIN = 5.0
FUZZY_MIN_PATTERN_CHARS = 5
FUZZY_MAX_WINDOW_EXTRA = 2


@dataclass
class VendorMatch:
    vendor_id: str
    vendor_name: str
    score: float
    matched_patterns: list[str] = field(default_factory=list)
    match_type: str = "unknown"


def _normalize_text(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", value).strip()


def _alias_variants(pattern: str) -> list[str]:
    normalized = _normalize_text(pattern)
    return [normalized] if normalized else []


def _text_windows(text_blob: str, pattern_word_count: int) -> list[str]:
    """Return token windows near the candidate length for safer fuzzy matching."""
    tokens = text_blob.split()
    if not tokens:
        return []

    min_size = max(1, pattern_word_count - FUZZY_MAX_WINDOW_EXTRA)
    max_size = min(len(tokens), pattern_word_count + FUZZY_MAX_WINDOW_EXTRA)
    windows: list[str] = []
    seen: set[str] = set()
    for size in range(min_size, max_size + 1):
        for start in range(0, len(tokens) - size + 1):
            window = " ".join(tokens[start : start + size])
            if window not in seen:
                windows.append(window)
                seen.add(window)
    return windows


def _pattern_is_safe_for_fuzzy(pattern: str) -> bool:
    """Avoid fuzzy matching tiny ids or numeric-only patterns."""
    compact = pattern.replace(" ", "")
    if len(compact) < FUZZY_MIN_PATTERN_CHARS:
        return False
    return any(ch.isalpha() for ch in compact)


def _best_window_score(pattern: str, text_blob: str) -> tuple[float, str | None]:
    pattern_words = pattern.split()
    if not pattern_words or not _pattern_is_safe_for_fuzzy(pattern):
        return 0.0, None

    best_score = 0.0
    best_window: str | None = None
    for window in _text_windows(text_blob, len(pattern_words)):
        score = fuzz.token_sort_ratio(pattern, window)
        if score > best_score:
            best_score = float(score)
            best_window = window
            if best_score == 100.0:
                break
    return best_score, best_window


def _effective_fuzzy_score(pattern: str, window: str, raw_score: float) -> float:
    """Adjust fuzzy confidence by how distinctive the candidate pattern is."""
    pattern_word_count = len(pattern.split())
    score = raw_score
    if pattern_word_count > 1:
        score += min(4.0, float((pattern_word_count - 1) * 2))
    elif pattern != window:
        score -= 3.0
    return max(0.0, min(100.0, score))


async def _load_detection_aliases(pool, user_id: str | None = None) -> list[dict]:
    aliases = await db_mod.get_all_aliases_for_detection(pool, user_id=user_id)

    # Vendor names are always valid candidates (weight=5 — meaningful but below explicit aliases).
    # Explicit vendor_aliases are additive; they are never generated from document text.
    vendors = await db_mod.list_vendors(pool, user_id=user_id)
    for v in vendors:
        aliases.append(
            {
                "vendor_id": v["id"],
                "vendor_name": v["name"],
                "pattern": v["name"],
                "weight": 5,
                "source": "vendor_name",
            }
        )

    if not aliases:
        logger.warning("No vendors or aliases configured in DB for user_id=%s", user_id)
    else:
        alias_count = sum(1 for a in aliases if a.get("source") != "vendor_name")
        name_count  = sum(1 for a in aliases if a.get("source") == "vendor_name")
        logger.info(
            "Detection aliases loaded: %d explicit alias(es) + %d vendor name(s) for user_id=%s",
            alias_count, name_count, user_id,
        )
    return aliases


def _detect_exact(aliases: list[dict], text_blob: str) -> VendorMatch | None:
    scores: dict[str, dict] = {}
    for alias in aliases:
        variants = _alias_variants(alias["pattern"])
        if not variants:
            continue
        matched_pattern = next((p for p in variants if p in text_blob), None)
        if not matched_pattern:
            continue

        vid = alias["vendor_id"]
        if vid not in scores:
            scores[vid] = {
                "vendor_name": alias["vendor_name"],
                "score": 0.0,
                "matched_patterns": [],
            }
        scores[vid]["score"] += float(alias.get("weight", 1))
        scores[vid]["matched_patterns"].append(matched_pattern)

    if not scores:
        return None

    best_vid = max(scores, key=lambda v: scores[v]["score"])
    best = scores[best_vid]
    if best["score"] < MIN_SCORE:
        logger.info(
            "Best exact vendor match '%s' scored %.1f (below threshold %d)",
            best_vid, best["score"], MIN_SCORE,
        )
        return None

    logger.info(
        "Vendor detected by exact match: %s (score=%.1f, patterns=%s)",
        best_vid, best["score"], best["matched_patterns"],
    )
    return VendorMatch(
        vendor_id=best_vid,
        vendor_name=best["vendor_name"],
        score=best["score"],
        matched_patterns=best["matched_patterns"],
        match_type="exact",
    )


def _detect_fuzzy(aliases: list[dict], text_blob: str) -> VendorMatch | None:
    best_by_vendor: dict[str, dict] = {}
    for alias in aliases:
        for pattern in _alias_variants(alias["pattern"]):
            raw_score, window = _best_window_score(pattern, text_blob)
            if not window:
                continue
            score = _effective_fuzzy_score(pattern, window, raw_score)
            vid = alias["vendor_id"]
            current = best_by_vendor.get(vid)
            if current is None or score > current["score"]:
                best_by_vendor[vid] = {
                    "vendor_name": alias["vendor_name"],
                    "score": score,
                    "matched_patterns": [f"fuzzy:{pattern}~{window}:{raw_score:.1f}"],
                }

    if not best_by_vendor:
        return None

    ranked = sorted(best_by_vendor.items(), key=lambda item: item[1]["score"], reverse=True)
    best_vid, best = ranked[0]
    if best["score"] < FUZZY_MIN_SCORE:
        logger.info(
            "Best fuzzy vendor match '%s' scored %.1f (below threshold %.1f)",
            best_vid,
            best["score"],
            FUZZY_MIN_SCORE,
        )
        return None

    if len(ranked) > 1:
        second_vid, second = ranked[1]
        margin = best["score"] - second["score"]
        if margin < FUZZY_MIN_MARGIN:
            logger.warning(
                "Ambiguous fuzzy vendor match: %s=%.1f, %s=%.1f (margin %.1f < %.1f)",
                best_vid,
                best["score"],
                second_vid,
                second["score"],
                margin,
                FUZZY_MIN_MARGIN,
            )
            return None

    logger.info(
        "Vendor detected by fuzzy match: %s (score=%.1f, patterns=%s)",
        best_vid,
        best["score"],
        best["matched_patterns"],
    )
    return VendorMatch(
        vendor_id=best_vid,
        vendor_name=best["vendor_name"],
        score=best["score"],
        matched_patterns=best["matched_patterns"],
        match_type="fuzzy",
    )


async def detect_vendor(
    pool,
    page_words: list[dict],
    user_id: str | None = None,
) -> VendorMatch | None:
    """Detect vendor from page-1 words using exact matching, then RapidFuzz.

    Pass user_id to restrict matching to vendors owned by that user (tenant isolation).
    Pass None (admin) to match across all vendors.
    """
    if not page_words:
        logger.warning("[VendorDetect] No words provided for vendor detection")
        return None

    raw_words = [w.get("text", "") for w in page_words if w.get("text", "").strip()]
    text_blob = _normalize_text(" ".join(raw_words))
    if not text_blob:
        logger.warning("[VendorDetect] Empty text blob after normalisation (page_words=%d)", len(page_words))
        return None

    logger.info(
        "[VendorDetect] Page-1 text (%d words, %d chars): %s%s",
        len(raw_words),
        len(text_blob),
        text_blob[:300],
        " ..." if len(text_blob) > 300 else "",
    )

    aliases = await _load_detection_aliases(pool, user_id=user_id)
    if not aliases:
        logger.warning("[VendorDetect] No vendors found for user_id=%s", user_id)
        return None

    # Log every pattern that will be tested
    for a in aliases:
        logger.debug(
            "[VendorDetect] candidate  vendor_id=%-10s  weight=%s  source=%-14s  pattern=%r",
            a.get("vendor_id"), a.get("weight"), a.get("source"), a.get("pattern"),
        )

    exact_match = _detect_exact(aliases, text_blob)
    if exact_match:
        logger.info(
            "[VendorDetect] MATCHED (exact)  vendor_id=%s  name=%r  score=%.1f  patterns=%s",
            exact_match.vendor_id, exact_match.vendor_name, exact_match.score, exact_match.matched_patterns,
        )
        return exact_match

    logger.info(
        "[VendorDetect] No exact match found in page-1 text (%d chars) — trying fuzzy",
        len(text_blob),
    )
    fuzzy_match = _detect_fuzzy(aliases, text_blob)
    if fuzzy_match:
        logger.info(
            "[VendorDetect] MATCHED (fuzzy)  vendor_id=%s  name=%r  score=%.1f  patterns=%s",
            fuzzy_match.vendor_id, fuzzy_match.vendor_name, fuzzy_match.score, fuzzy_match.matched_patterns,
        )
    else:
        logger.warning(
            "[VendorDetect] NO MATCH — neither exact nor fuzzy found a vendor for user_id=%s. "
            "Text snippet: %r",
            user_id, text_blob[:200],
        )
    return fuzzy_match
