"""
bbox_agent.py — One-shot layout detector for Qwen3-VL.

Runs once per (vendor, template) on page 1 of the first document seen.
Returns the bounding boxes of LABEL text (not values) for requested field_keys.
Output is stored in qwen_layout_boxes and reused for all subsequent pages/docs.

Uses ONLY Qwen-VL vision output for label detection — no pypdfium2 or PaddleOCR
word geometry is used in this module.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

if __package__:
    from .config import LLM_TEMPERATURE, LLM_TOP_P, LLM_MAX_TOKENS_BBOX
    from . import logging_config as plog
    from .phoenix_tracing import trace_span, trace_llm_call as _trace_llm
else:
    from config import LLM_TEMPERATURE, LLM_TOP_P, LLM_MAX_TOKENS_BBOX  # type: ignore[no-redef]
    import logging_config as plog  # type: ignore[no-redef]
    from phoenix_tracing import trace_span, trace_llm_call as _trace_llm  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?", re.MULTILINE)
_FENCE_END_RE  = re.compile(r"\n?```\s*$", re.MULTILINE)


def _build_bbox_system_prompt() -> str:
    return """\
You are a Document Layout Analyzer.
Your ONLY task: locate the exact position of where specific label text and table column header text appears in the document image.

<field_types>
There are exactly two types of fields:
1. HEADER FIELDS — These are standalone label-value pairs scattered across the page (e.g., "Ship To:", "PO Number:", "Date:"). You must return the bounding box of the LABEL text only (e.g., the words "PO Number"), NOT the value next to it.
2. LINE ITEM COLUMNS — These are column headers in the document's item table (e.g., "Qty", "Unit Price", "Material"). First locate the table region in the document, then identify the header row at the top of that table. Return the bounding box of each column header cell text.
</field_types>

<rules>
- Strictly return ONLY the bounding box of the LABEL or HEADER text region — never the value, never the cell content.
- Every coordinate uses bbox_2d format: [x1, y1, x2, y2] in a 0-1000 normalized grid relative to the full page image.
- If a label or header is not visible on this page, return null for that entry.
- Strictly return ONLY valid JSON. No markdown fences, no explanation, no extra text.
- If you find multiple occurrences of the same field name in the document, map to the one that is contextually correct. eg: material_no is user requested, u found 2 words, header or line item header. don't map everywhere u see. 
</rules>

<critical> Count the number of fields which user mentioned FIRST, then extract that exact number of boxes. </critical>
"""


def _build_bbox_user_message(
    header_field_keys: list[str],
    line_item_column_keys: list[str],
) -> str:
    header_section = ""
    if header_field_keys:
        bullets = "\n".join(f"  - {k}" for k in header_field_keys)
        header_section = f"\n<header_labels>\n{bullets}\n</header_labels>"

    line_section = ""
    if line_item_column_keys:
        bullets = "\n".join(f"  - {k}" for k in line_item_column_keys)
        line_section = f"\n<table_headers>\n{bullets}\n</table_headers>"

    all_keys = header_field_keys + line_item_column_keys
    example = "{\n  \"boxes\": {\n"
    for i, k in enumerate(all_keys):
        comma = "," if i < len(all_keys) - 1 else ""
        example += f"    \"{k}\": null{comma}\n"
    example += "  }\n}"

    return f"""Locate each label and table column header listed below in this document image.
Return their bounding box coordinates in bbox_2d format [x1, y1, x2, y2], normalized to a 0-1000 grid.
Strictly return the LABEL text region only — NOT its value.
{header_section}
{line_section}

Return JSON in exactly this shape:
{example}

Strictly return ONLY valid JSON matching the structure above."""


async def learn_layout_for_vendor(
    *,
    page1_image_b64: str,
    page1_width: int,
    page1_height: int,
    header_field_keys: list[str],
    line_item_column_keys: list[str],
    llm_url: str,
    model: str,
    pipeline_context: dict | None = None,
) -> dict[str, dict]:
    """Call Qwen3-VL on page 1 to locate field labels.

    Returns {field_key: {"normalized_box": {"x0":..,"y0":..,"x1":..,"y1":..} in 0..1,
                          "field_type": "header"|"line_item_column"}}

    Uses the FACTOR=32 alignment correction documented in docs/qwen_bbox_github_issue.md.
    Coordinates are mapped through the aligned (padded) image dimensions to remove drift.
    """
    all_requested = set(header_field_keys) | set(line_item_column_keys)
    if not all_requested:
        return {}

    # ── Build prompt ──
    with trace_span(
        "bbox_agent.build_prompt",
        kind="TOOL",
        input_data={
            "header_field_keys": header_field_keys,
            "line_item_column_keys": line_item_column_keys,
        },
    ) as prompt_ctx:
        system_prompt = _build_bbox_system_prompt()
        user_message  = _build_bbox_user_message(header_field_keys, line_item_column_keys)
        prompt_ctx["output"] = {
            "system_prompt": system_prompt,
            "user_message": user_message,
        }

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{page1_image_b64}"},
                },
                {"type": "text", "text": user_message},
            ],
        },
    ]

    payload = {
        "model": model,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "top_p": LLM_TOP_P,
        "max_tokens": LLM_MAX_TOKENS_BBOX,
    }

    # ── LLM call with full OpenInference tracing ──
    start = time.perf_counter()
    with _trace_llm(
        model=model,
        messages=messages,
        temperature=LLM_TEMPERATURE,
    ) as llm_ctx:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(llm_url, json=payload)
                resp.raise_for_status()
            resp_json = resp.json()
            raw: str = resp_json["choices"][0]["message"]["content"].strip()
            llm_ctx["response"] = raw
            llm_ctx["usage"] = resp_json.get("usage", {})
        except Exception as exc:
            logger.warning("BBox agent HTTP error for model=%s: %s", model, exc)
            if pipeline_context:
                plog.event("bbox_agent_http_error", stage="llm", status="error", error=str(exc), **(pipeline_context or {}))
            return {}

    # ── Parse (pure Qwen output, no OCR snapping) ──
    with trace_span(
        "bbox_agent.parse",
        kind="TOOL",
        input_data={"raw_response_length": len(raw)},
    ) as parse_ctx:
        # Strip markdown fences if present
        raw = _JSON_FENCE_RE.sub("", raw)
        raw = _FENCE_END_RE.sub("", raw).strip()

        try:
            parsed = json.loads(raw)
            raw_boxes: dict = parsed.get("boxes") or {}
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.warning("BBox agent JSON parse failed: %s — raw: %.200s", exc, raw)
            if pipeline_context:
                plog.event("bbox_agent_parse_error", stage="llm", status="error", raw_preview=raw[:200], **(pipeline_context or {}))
            parse_ctx["output"] = {"error": str(exc), "raw_preview": raw[:200]}
            return {}

        result: dict[str, dict] = {}
        # Qwen3-VL coordinates are relative to the 32px-aligned image, not the original.
        # See docs/qwen_bbox_github_issue.md for the full explanation.
        FACTOR = 32
        w_bar = max(FACTOR, int(round(page1_width / FACTOR) * FACTOR))
        h_bar = max(FACTOR, int(round(page1_height / FACTOR) * FACTOR))

        for field_key in all_requested:
            box_raw = raw_boxes.get(field_key)
            field_type = "header" if field_key in header_field_keys else "line_item_column"

            if not isinstance(box_raw, list) or len(box_raw) != 4:
                # LLM explicitly returned null, omitted it, or it's malformed.
                # Only persist fields where a usable box was learned.
                continue

            # Step 1: Map 0-1000 grid → pixel coords on the aligned image
            x0_px = (box_raw[0] / 1000.0) * w_bar
            y0_px = (box_raw[1] / 1000.0) * h_bar
            x1_px = (box_raw[2] / 1000.0) * w_bar
            y1_px = (box_raw[3] / 1000.0) * h_bar

            # Step 2: Normalize relative to the original image dimensions
            nx0 = x0_px / page1_width
            ny0 = y0_px / page1_height
            nx1 = x1_px / page1_width
            ny1 = y1_px / page1_height

            nx0, ny0 = max(0.0, nx0), max(0.0, ny0)
            nx1, ny1 = min(1.0, nx1), min(1.0, ny1)

            if nx1 <= nx0 or ny1 <= ny0:
                continue

            result[field_key] = {
                "normalized_box": {"x0": nx0, "y0": ny0, "x1": nx1, "y1": ny1},
                "field_type": field_type,
            }

        parse_ctx["output"] = {
            "fields_learned": list(result.keys()),
            "fields_missed": [f for f in all_requested if f not in result],
            "raw_boxes_from_llm": {k: v for k, v in raw_boxes.items() if k in all_requested},
        }

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    logger.info(
        "BBox agent: learned %d/%d fields in %dms (model=%s)",
        len(result), len(all_requested), elapsed_ms, model,
    )
    if pipeline_context:
        plog.event(
            "bbox_agent_layout_learned",
            stage="llm",
            fields_learned=list(result.keys()),
            fields_requested=list(all_requested),
            duration_ms=elapsed_ms,
            **(pipeline_context or {}),
        )

    return result
