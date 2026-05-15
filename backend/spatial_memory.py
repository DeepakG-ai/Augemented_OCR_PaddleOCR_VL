"""
spatial_memory.py — Reusable geometry memory for manual corrections.

Phase 3 (write): save_from_corrections() persists normalized regions from
manual drag-box corrections as reusable spatial memory keyed by vendor+layout.

Phase 4 (read): apply_to_extraction() looks up saved regions for the current
vendor+layout, reads the current document text inside those regions, and
overrides extraction results with the current text.

Critical rule (AGENTS.md §2–3): We store WHERE the field is (geometry),
never WHAT the old value was. On reuse, we always read the current
document's text inside the saved region.
"""
from __future__ import annotations

import logging
from typing import Any

from . import db as db_mod

from .layout_key import compute_layout_key

logger = logging.getLogger("spatial_memory")

LINE_ITEM_FIELD_PREFIX = "line_item_"
LINE_ITEM_CONTAINER = "line_items"


def _configured_field_names(fields: Any) -> set[str]:
    """Return configured header field names from list-like template data."""
    if not isinstance(fields, list):
        return set()

    names: set[str] = set()
    for field in fields:
        if isinstance(field, str):
            name = field.strip()
        elif isinstance(field, dict):
            raw = (
                field.get("field_key")
                or field.get("key")
                or field.get("name")
                or field.get("id")
            )
            name = str(raw).strip() if raw is not None else ""
        else:
            name = ""
        if name:
            names.add(name)
    return names


async def _load_configured_header_fields(pool, extraction: dict, vendor_id: str) -> set[str]:
    """Load dynamic header field names for this client/template."""
    names = _configured_field_names(extraction.get("header_fields"))
    if names:
        return names

    try:
        template = await db_mod.get_template(pool, vendor_id)
    except Exception as exc:
        logger.debug("Could not load template fields for vendor=%s: %s", vendor_id, exc)
        return set()

    return _configured_field_names((template or {}).get("header_fields"))


def _is_reusable_header_field(field_key: str, configured_header_fields: set[str]) -> bool:
    """True when a review field is a reusable top-level template field."""
    if not isinstance(field_key, str) or not field_key.strip():
        return False
    if field_key == LINE_ITEM_CONTAINER or field_key.startswith(LINE_ITEM_FIELD_PREFIX):
        return False
    if configured_header_fields:
        return field_key in configured_header_fields
    return False




def _normalize_box(
    box: dict | list,
    page_width: int,
    page_height: int,
) -> dict | None:
    """Convert a pixel-space box to normalized 0..1 coords.

    Accepts box as {x0,y0,x1,y1} dict or [x0,y0,x1,y1] list.
    Returns {x0,y0,x1,y1} with values in [0..1] relative to page size.
    """
    if page_width <= 0 or page_height <= 0:
        return None

    if isinstance(box, list) and len(box) == 4:
        x0, y0, x1, y1 = box
    elif isinstance(box, dict):
        x0 = box.get("x0", box.get("left", 0))
        y0 = box.get("y0", box.get("top", 0))
        x1 = box.get("x1", box.get("right", 0))
        y1 = box.get("y1", box.get("bottom", 0))
    else:
        return None

    return {
        "x0": round(x0 / page_width, 6),
        "y0": round(y0 / page_height, 6),
        "x1": round(x1 / page_width, 6),
        "y1": round(y1 / page_height, 6),
    }


def _denormalize_box(
    normalized: dict,
    page_width: int,
    page_height: int,
) -> list[int]:
    """Convert normalized 0..1 box back to pixel coordinates [x0,y0,x1,y1]."""
    return [
        int(round(normalized["x0"] * page_width)),
        int(round(normalized["y0"] * page_height)),
        int(round(normalized["x1"] * page_width)),
        int(round(normalized["y1"] * page_height)),
    ]


def _words_in_box(words: list[dict], box: list[int]) -> list[dict]:
    """Find words whose center falls inside the given [x0,y0,x1,y1] box."""
    x0, y0, x1, y1 = box
    matched = []
    for w in words:
        wb = w.get("box", [])
        if len(wb) != 4:
            continue
        cx = (wb[0] + wb[2]) / 2
        cy = (wb[1] + wb[3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            matched.append(w)
    return matched


def _reading_order(words: list[dict]) -> list[dict]:
    """Sort matched words top-to-bottom, then left-to-right."""
    return sorted(
        words,
        key=lambda w: (
            int((w.get("box") or [0, 0, 0, 0])[1] / 10),
            (w.get("box") or [0, 0, 0, 0])[0],
        ),
    )


# ── Phase 3: Write path ──────────────────────────────────────────────────────

async def save_from_corrections(
    pool,
    extraction_id: int,
    field_locations: dict | list,
    corrected_result: Any,
) -> int:
    """Persist spatial memory from manual review corrections.

    For each eligible header field that has a field_location with a box,
    normalizes the box and upserts into spatial_memory.

    Args:
        pool: asyncpg connection pool.
        extraction_id: The extraction being corrected.
        field_locations: The field_locations dict from the correction payload.
        corrected_result: The corrected extraction result.

    Returns:
        Number of spatial memory rows written.
    """
    # Load extraction metadata
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        logger.warning("Extraction %d not found for spatial memory save", extraction_id)
        return 0

    vendor_id = extraction.get("vendor_id")
    template_id = extraction.get("template_id")
    page_results = extraction.get("page_results") or []

    if not vendor_id:
        logger.warning("No vendor_id on extraction %d — skipping spatial memory", extraction_id)
        return 0

    # Compute layout key
    lk = compute_layout_key(vendor_id, template_id, page_results)
    base = {
        "extraction_id": extraction_id,
        "vendor_id": vendor_id,
        "vendor_name": extraction.get("vendor_name"),
        "filename": extraction.get("filename"),
    }

    # Load page dimensions
    pages = await db_mod.get_pages(pool, extraction_id)
    page_dims = {p["page_number"]: (p.get("width", 0), p.get("height", 0)) for p in pages}
    page_sources = {p["page_number"]: p.get("source", "paddleocr") for p in pages}

    # Handle list-format field_locations (po_per_page)
    if isinstance(field_locations, list):
        # Flatten: merge all per-doc field_locations into one
        merged = {}
        for fl in field_locations:
            if isinstance(fl, dict):
                merged.update(fl)
        field_locations = merged

    if not isinstance(field_locations, dict):
        return 0

    configured_header_fields = await _load_configured_header_fields(pool, extraction, vendor_id)
    logger.info("Spatial memory save: layout=%s, %d locations submitted", lk, len(field_locations))
    saved = 0
    for field_key, loc in field_locations.items():
        # Save reusable memory for configured top-level fields only.
        if not _is_reusable_header_field(field_key, configured_header_fields):
            continue

        if not isinstance(loc, dict):
            continue
            
        strategy = str(loc.get("strategy") or "").lower()
        # AGENTS.md rule: only human drag-box corrections become reusable memory.
        # Qwen anchors and previously applied memory are not value regions.
        if strategy != "manual":
            logger.debug("Spatial memory skip: field=%s strategy=%s (not manual)", field_key, strategy)
            continue

        box = loc.get("box")
        page_num = loc.get("page", 1)

        if not box:
            continue

        dims = page_dims.get(page_num)
        if not dims or dims[0] <= 0 or dims[1] <= 0:
            continue

        normalized = _normalize_box(box, dims[0], dims[1])
        if not normalized:
            continue

        source_engine = "pypdfium" if page_sources.get(page_num) == "pypdfium" else "paddleocr"

        try:
            await db_mod.upsert_spatial_memory(
                pool,
                vendor_id=vendor_id,
                layout_key=lk,
                field_key=field_key,
                page_number=page_num,
                normalized_box=normalized,
                source_engine=source_engine,
                created_from_extraction_id=extraction_id,
            )
            saved += 1

            logger.info(
                "Spatial memory saved: vendor=%s layout=%s field=%s page=%d",
                vendor_id, lk, field_key, page_num,
            )
        except Exception as exc:
            logger.warning(
                "Failed to save spatial memory for field=%s: %s",
                field_key, exc,
            )

    logger.info("Spatial memory save done: layout=%s, %d saved", lk, saved)
    return saved


# ── Phase 4: Read path ──────────────────────────────────────────────────────

async def _apply_to_po_per_page(
    pool,
    extraction_id: int,
    extraction: dict,
    result: list,
    field_locations: list | dict,
    page_geometry: list[dict] | None,
) -> tuple[list, list, int]:
    """Apply spatial memory to po_per_page list results.

    result[i] corresponds to page i+1. Memories are applied page-by-page so
    a correction saved for page 2 only touches result[1].
    """
    vendor_id = extraction.get("vendor_id")
    template_id = extraction.get("template_id")
    page_results = extraction.get("page_results") or []

    if not vendor_id:
        return result, field_locations, 0

    lk = compute_layout_key(vendor_id, template_id, page_results)
    base = {
        "extraction_id": extraction_id,
        "vendor_id": vendor_id,
        "vendor_name": extraction.get("vendor_name"),
        "filename": extraction.get("filename"),
    }

    memories = await db_mod.get_spatial_memory_for_layout(pool, vendor_id, lk)
    logger.info("Spatial memory loaded: layout=%s, %d regions (po_per_page)", lk, len(memories))
    if not memories:
        return result, field_locations, 0

    configured_header_fields = await _load_configured_header_fields(pool, extraction, vendor_id)

    pages = await db_mod.get_pages(pool, extraction_id)
    page_dims = {p["page_number"]: (p.get("width", 0), p.get("height", 0)) for p in pages}

    if page_geometry is None:
        page_geometry = extraction.get("ocr_data") or []

    words_by_page: dict[int, list] = {}
    for entry in page_geometry:
        pn = entry.get("page_number", 0)
        words_by_page[pn] = entry.get("words", [])

    # Normalise field_locations into a per-page list matching result
    if isinstance(field_locations, list):
        fl_list: list[dict] = [dict(fl) if isinstance(fl, dict) else {} for fl in field_locations]
    else:
        fl_list = [dict(field_locations) for _ in result]

    while len(fl_list) < len(result):
        fl_list.append({})

    applied = 0
    for mem in memories:
        field_key = mem["field_key"]
        if not configured_header_fields or field_key not in configured_header_fields:
            logger.debug("Spatial memory skip: field=%s not in template (or template unavailable)", field_key)
            continue

        page_num = mem["page_number"]
        normalized = mem["normalized_box"]

        idx = page_num - 1
        if idx < 0 or idx >= len(result):
            continue

        page_result = result[idx]
        if not isinstance(page_result, dict):
            continue

        dims = page_dims.get(page_num)
        if not dims or dims[0] <= 0 or dims[1] <= 0:
            continue

        pixel_box = _denormalize_box(normalized, dims[0], dims[1])
        matched_words = _reading_order(_words_in_box(words_by_page.get(page_num, []), pixel_box))
        current_text = " ".join(w.get("text", "") for w in matched_words).strip()

        if len(current_text) < 2:
            logger.debug(
                "Spatial memory skip: field=%s page=%d text too short (%d chars)",
                field_key, page_num, len(current_text),
            )

            continue

        if field_key in page_result:
            old_val = page_result[field_key]
            page_result[field_key] = current_text

            logger.info(
                "Spatial memory applied (po_per_page): field=%s page=%d old='%s' new='%s'",
                field_key, page_num, str(old_val)[:50], current_text[:50],
            )
        else:
            page_result[field_key] = current_text

            logger.info(
                "Spatial memory added (po_per_page): field=%s page=%d value='%s'",
                field_key, page_num, current_text[:50],
            )

        fl_list[idx][field_key] = {
            "page": page_num,
            "box": pixel_box,
            "strategy": "spatial_memory",
            "confidence": "high",
            "matched_text": current_text,
        }
        applied += 1

    if applied:
        logger.info(
            "Spatial memory: %d field(s) applied (po_per_page) for extraction %d (vendor=%s, layout=%s)",
            applied, extraction_id, vendor_id, lk,
        )

    return result, fl_list, applied


async def apply_to_extraction(
    pool,
    extraction_id: int,
    result: dict | list,
    field_locations: dict,
    page_geometry: list[dict] | None = None,
) -> tuple[dict | list, dict, int]:
    """Apply saved spatial memory regions to an extraction result.

    For each active memory entry matching the current vendor+layout:
    1. Convert normalized box to pixel coords using current page dimensions
    2. Find current-document words inside that box
    3. If words found: override the result field with current text
    4. If no words found: skip (Qwen's answer stays)

    Args:
        pool: asyncpg connection pool.
        extraction_id: Current extraction ID.
        result: The extraction result dict (or list for po_per_page).
        field_locations: The current field_locations dict.
        page_geometry: Unified page geometry (from ocr_data). If None, loaded from DB.

    Returns:
        (updated_result, updated_field_locations, fields_applied_count)
    """
    extraction = await db_mod.get_extraction(pool, extraction_id)
    if not extraction:
        return result, field_locations, 0

    if isinstance(result, list):
        return await _apply_to_po_per_page(
            pool, extraction_id, extraction, result, field_locations, page_geometry
        )

    if isinstance(field_locations, list):
        logger.debug("Skipping spatial memory apply: field_locations is list but result is not.")
        return result, field_locations, 0

    vendor_id = extraction.get("vendor_id")
    template_id = extraction.get("template_id")
    page_results = extraction.get("page_results") or []

    if not vendor_id:
        return result, field_locations, 0

    # Compute layout key
    lk = compute_layout_key(vendor_id, template_id, page_results)
    base = {
        "extraction_id": extraction_id,
        "vendor_id": vendor_id,
        "vendor_name": extraction.get("vendor_name"),
        "filename": extraction.get("filename"),
    }

    # Load spatial memory for this vendor+layout
    memories = await db_mod.get_spatial_memory_for_layout(pool, vendor_id, lk)
    logger.info("Spatial memory loaded: layout=%s, %d regions", lk, len(memories))
    if not memories:
        return result, field_locations, 0

    configured_header_fields = await _load_configured_header_fields(pool, extraction, vendor_id)

    # Load page dimensions
    pages = await db_mod.get_pages(pool, extraction_id)
    page_dims = {p["page_number"]: (p.get("width", 0), p.get("height", 0)) for p in pages}

    # Load page geometry (words) if not provided
    if page_geometry is None:
        ocr_data = extraction.get("ocr_data") or []
        page_geometry = ocr_data

    words_by_page = {}
    for entry in page_geometry:
        pn = entry.get("page_number", 0)
        words_by_page[pn] = entry.get("words", [])

    applied = 0
    for mem in memories:
        field_key = mem["field_key"]
        if not configured_header_fields or field_key not in configured_header_fields:
            logger.debug("Spatial memory skip: field=%s not in template (or template unavailable)", field_key)
            continue

        page_num = mem["page_number"]
        normalized = mem["normalized_box"]

        dims = page_dims.get(page_num)
        if not dims or dims[0] <= 0 or dims[1] <= 0:
            continue

        # Convert normalized box to current pixel coords
        pixel_box = _denormalize_box(normalized, dims[0], dims[1])

        # Find current words in the region
        current_words = words_by_page.get(page_num, [])
        matched_words = _reading_order(_words_in_box(current_words, pixel_box))
        current_text = " ".join(w.get("text", "") for w in matched_words).strip()

        # Skip if empty or too short (staleness guard per risk register)
        if len(current_text) < 2:
            logger.debug(
                "Spatial memory skip: field=%s text too short (%d chars)",
                field_key, len(current_text),
            )

            continue

        # Override result value
        if isinstance(result, dict) and field_key in result:
            old_val = result[field_key]
            result[field_key] = current_text

            logger.info(
                "Spatial memory applied: field=%s old='%s' new='%s' (from region on page %d)",
                field_key, str(old_val)[:50], current_text[:50], page_num,
            )
        elif isinstance(result, dict):
            # Field exists in memory but not in result — add it
            result[field_key] = current_text

            logger.info(
                "Spatial memory added: field=%s value='%s' (from region on page %d)",
                field_key, current_text[:50], page_num,
            )

        # Update field_locations
        field_locations[field_key] = {
            "page": page_num,
            "box": pixel_box,
            "strategy": "spatial_memory",
            "confidence": "high",
            "matched_text": current_text,
        }
        applied += 1

    if applied:
        logger.info(
            "Spatial memory: %d field(s) applied for extraction %d (vendor=%s, layout=%s)",
            applied, extraction_id, vendor_id, lk,
        )

    return result, field_locations, applied
