"""
extractor.py -- Core LLM extraction, prompt building, result merging, prompt caching.

Two modes:
  - Auto Extract: no fields → generic extraction, model returns everything
  - Extract Fields: user-defined fields dynamically injected into user message

Same prompt for every page. Merger takes header from page 1, line_items from all pages.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any, Awaitable, Callable


import httpx

from . import db as db_mod

from .logging_config import get_logger
from .config import (
    LLM_TEMPERATURE,
    LLM_TOP_P,
    LLM_PRESENCE_PENALTY,
    LLM_MAX_TOKENS_FIELDS,
    LLM_TIMEOUT,
)
from .mlflow_tracing import (
    trace_llm_call,
    trace_page_extraction,
    trace_build_user_message,
    trace_merge_results,
)

logger = get_logger(__name__)


def _strip_newlines(obj: Any) -> Any:
    """Recursively replace embedded newlines in all string values with a space."""
    if isinstance(obj, str):
        return obj.replace("\n", " ").replace("\r", " ").strip()
    if isinstance(obj, dict):
        return {k: _strip_newlines(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_newlines(item) for item in obj]
    return obj


# v5.2 = vendor verification on page 1, value-redacted gold hints,
# removed format descriptions / duplicate bbox / contradictory type rules.
PROMPT_VERSION = "v5.2"


def _has_value(value: Any) -> bool:
    """Return True when a correction side contains user-visible content."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _correction_hint(correction: Any) -> str:
    """Summarize a correction without leaking the old or corrected value."""
    if not isinstance(correction, dict):
        return "reviewer changed this field"
    original_has_value = _has_value(correction.get("original"))
    corrected_has_value = _has_value(correction.get("corrected"))
    if not original_has_value and corrected_has_value:
        return "reviewer filled a missing value"
    if original_has_value and not corrected_has_value:
        return "reviewer cleared a value that was not visible"
    return "reviewer replaced the extracted value"


def _safe_gold_correction_hints(gold_examples: list[dict] | None) -> dict[str, dict[str, str]]:
    """Return field-level correction hints with all document values redacted.

    Gold examples are useful for identifying fields where the model commonly
    makes mistakes, but passing prior corrected values into the prompt causes
    value leakage on future documents. Keep only the field names and correction
    categories.
    """
    hints: dict[str, dict[str, str]] = {}
    for ex in gold_examples or []:
        correction_diff = ex.get("correction_diff")
        if not isinstance(correction_diff, dict):
            continue
        for field_key, correction in correction_diff.items():
            key = str(field_key).strip()
            if not key:
                continue
            hints[key] = {
                "history": _correction_hint(correction),
                "instruction": "extract only the current visible document value; never reuse a prior correction",
            }
    return hints

# ── System Prompt (built once, stored in DB + Redis) ─────────────────

def build_system_prompt(
    header_fields: list[str],
    line_item_fields: list[str],
    instructions: str | None,
    rules: list[str],
    format_type: str,
    gold_examples: list[dict] | None = None,
    include_boxes: bool = False,
    vendor_name: str | None = None,
) -> str:
    """Build the reusable system prompt.

    Args:
        gold_examples: Value-redacted correction hints from past human review.
        include_boxes: If True (page 1), also request bounding boxes.
        vendor_name: Detected vendor name for LLM-side verification (page 1).
    """
    context_section = ""
    if instructions and instructions.strip():
        context_section = f"""
<document_context>
{instructions.strip()}
</document_context>"""

    rules_section = ""
    if rules:
        numbered = "\n".join(f"  {i}. {rule}" for i, rule in enumerate(rules, 1))
        rules_section = f"""
<extraction_rules>
{numbered}
</extraction_rules>"""

    # Value-safe correction hints — no old values leaked into prompt
    gold_section = ""
    correction_hints = _safe_gold_correction_hints(gold_examples)
    if correction_hints:
        hints_json = json.dumps(correction_hints, indent=2, ensure_ascii=False)
        if hints_json.strip():
            gold_section = f"""
<correction_hints>
Human review has corrected these fields before. Values are intentionally redacted.
Use this only as a warning that the field needs careful current-document reading:

{hints_json}
</correction_hints>"""

    # ── Page 1: fields + boxes + vendor verification ──
    if include_boxes:
        return_keys = """Return three top-level keys:
- `vendor_confirmed`: true if the document belongs to the detected vendor, false otherwise
- `fields`: extracted values
- `boxes`: bounding box of the LABEL text for each field"""

        bbox_rules = """
<bbox_rules>
- For each header field, return the bounding box of the LABEL text (e.g., word "PO Number:"), NOT the value next to it.
- For each line item column, return the bounding box of the COLUMN HEADER text in the table header row.
- Each box value MUST be a plain JSON array: [x1, y1, x2, y2] — four integers in a 0-1000 normalized grid relative to the full page image. Do NOT nest it in a dict or use any key like "bbox_2d".
- If a label or column header is not visible on this page, set its box to null.
</bbox_rules>"""

        vendor_section = ""
        if vendor_name:
            vendor_section = f"""
<vendor_verification>
System detected this document belongs to: "{vendor_name}"
Check the document header, letterhead, or company name in the image.
Return vendor_confirmed: true if correct, false if the document belongs to a different company.
</vendor_verification>"""
    else:
        return_keys = """Return one top-level key:
- `fields`: extracted values"""
        bbox_rules = ""
        vendor_section = ""

    return f"""You are a highly accurate document data extraction assistant.
This request is processed one page at a time.

{return_keys}
{context_section}
{rules_section}
{gold_section}
{vendor_section}
{bbox_rules}
<critical>
Count the number of rows in the line items table FIRST, then extract that exact number of items.
</critical>

<output_rules>
- Extract ONLY what is explicitly visible in the document image.
- Never guess or fabricate data.
- STRICTLY return ONLY valid JSON. No markdown fences, no explanation, no extra text.
- Use null for missing fields, never omit them.
- For line_items, return an array even if only one item exists.
- Dates should be in the format they appear in the document.
</output_rules>"""


# ── User Message (per-call, same for every page) ────────────────────

def build_user_message(
    header_fields: list[str],
    line_item_fields: list[str],
    page_num: int,
    total_pages: int,
    include_boxes: bool = False,
) -> str:
    """
    Build the user message for a single page.

    Args:
        include_boxes: If True (page 1 only), ask the LLM to also return
            a ``boxes`` dict with label bounding boxes alongside ``fields``.
    """

    if header_fields or line_item_fields:
        # ── Extract Fields mode ──
        fields_template: dict[str, Any] = {}

        for f in header_fields:
            fields_template[f] = None

        if line_item_fields:
            fields_template["line_items"] = [{col: None for col in line_item_fields}]

        # ── Build JSON shape: page 1 includes vendor_confirmed + boxes ──
        if include_boxes:
            all_keys = list(header_fields) + list(line_item_fields)
            boxes_template = {k: None for k in all_keys}
            full_template: dict[str, Any] = {
                "vendor_confirmed": None,
                "fields": fields_template,
                "boxes": boxes_template,
            }
        else:
            full_template = {"fields": fields_template}

        header_section = ""
        if header_fields:
            header_list = "\n".join(f"  - {f}" for f in header_fields)
            header_section = f"""
<header_fields>
{header_list}
</header_fields>"""

        line_section = ""
        if line_item_fields:
            line_list = "\n".join(f"  - {f}" for f in line_item_fields)
            line_section = f"""
<line_item_columns>
{line_list}
</line_item_columns>"""

        return f"""Extract the header fields AND all visible line item rows from this purchase order page (page {page_num} of {total_pages}).

If any field is empty or not visible, return null.
{header_section}
{line_section}

Return JSON in exactly this shape:
{json.dumps(full_template, indent=2)}

<rules>
- Empty or missing cells → null.
- Extract every visible line item row.
</rules>

STRICTLY return ONLY valid JSON matching EXACTLY the structure above."""

    else:
        # ── Auto Extract mode (no bbox support) ──
        return f"""Extract ALL data from this invoice/purchase order document (page {page_num} of {total_pages}).

<critical>
Count the number of rows in the line items table FIRST, then extract that exact number of items.
</critical>

Return JSON with:
- Header fields: extract all visible header fields (po_number, order_date, vendor, bill_to, ship_to, etc.)
- Line items: extract all visible line item rows with all their columns

<accuracy>
- Before extraction: Count total rows in the table visually.
- After extraction: Verify your line_items array has that many items.
- Extract ONLY what is explicitly visible in the document image.
- Never guess or fabricate values.
</accuracy>

STRICTLY return ONLY valid JSON. No markdown fences, no explanation, no extra text."""


# ── Prompt Hash & Cache ─────────────────────────────────────────────

def compute_prompt_hash(
    header_fields: list[str],
    line_item_fields: list[str],
    instructions: str | None,
    rules: list[str],
    format_type: str = "single_page",
    gold_examples: list[dict] | None = None,
) -> str:
    payload = json.dumps({
        "header_fields": sorted(header_fields),
        "line_item_fields": sorted(line_item_fields),
        "instructions": instructions or "",
        "rules": sorted(rules),
        "format_type": format_type,
        "prompt_version": PROMPT_VERSION,
        "gold_examples": _safe_gold_correction_hints(gold_examples),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


async def get_or_build_system_prompt(
    pool, vendor_id: str,
    header_fields: list[str], line_item_fields: list[str],
    instructions: str | None, rules: list[str], format_type: str,
) -> tuple[str, str]:
    """Returns (system_prompt, prompt_hash). Cache: DB → build.

    Includes value-redacted correction hints from past human review.
    """

    # Fetch one consolidated latest correction per field for this vendor.
    gold_examples = await db_mod.get_gold_examples(pool, vendor_id)

    prompt_hash = compute_prompt_hash(
        header_fields, line_item_fields, instructions, rules, format_type,
        gold_examples=gold_examples,
    )

    # 1. DB
    tmpl = await db_mod.get_template(pool, vendor_id)
    if tmpl and tmpl.get("prompt_hash") == prompt_hash and tmpl.get("system_prompt"):
        logger.info("Prompt cache HIT (DB) vendor=%s hash=%s gold=%d", vendor_id, prompt_hash[:12], len(gold_examples))
        return tmpl["system_prompt"], prompt_hash

    # 2. Build fresh (includes value-redacted correction hints)
    logger.info("Prompt cache MISS — building vendor=%s hash=%s gold=%d", vendor_id, prompt_hash[:12], len(gold_examples))
    system_prompt = build_system_prompt(
        header_fields, line_item_fields, instructions, rules, format_type,
        gold_examples=gold_examples,
    )

    await db_mod.upsert_template(
        pool, vendor_id, format_type,
        header_fields, line_item_fields,
        instructions, rules, system_prompt, prompt_hash,
    )

    return system_prompt, prompt_hash


# ── LLM Call ─────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?", re.MULTILINE)
_FENCE_END_RE = re.compile(r"\n?```\s*$", re.MULTILINE)


def _usage_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def call_llm(
    image_b64: str, system_prompt: str, user_message: str,
    llm_url: str, model: str, mime_type: str = "image/jpeg",
    page_num: int = 0, total_pages: int = 0,
    pipeline_context: dict | None = None,
    cancel_event: asyncio.Event | None = None,
    pool: Any | None = None,
    billing_user_id: str | None = None,  # when set, usage is billed to this user not vendor owner
) -> dict:
    """POST to LLM, strip markdown fences, return parsed JSON dict.

    If cancel_event is set while the HTTP request is in-flight, the
    request is aborted immediately (saves GPU cycles on llama-server).
    """

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
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
        "presence_penalty": LLM_PRESENCE_PENALTY,
        "max_tokens": LLM_MAX_TOKENS_FIELDS,
    }

    start = time.perf_counter()
    with trace_llm_call(model, messages, temperature=LLM_TEMPERATURE, page_num=page_num, total_pages=total_pages) as trace_ctx:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            # If a cancel_event is provided, race the HTTP request against it
            if cancel_event:
                async def _do_post():
                    return await client.post(llm_url, json=payload)

                async def _wait_cancel():
                    await cancel_event.wait()
                    raise asyncio.CancelledError("Extraction cancelled by user")

                done, pending = await asyncio.wait(
                    [asyncio.create_task(_do_post()), asyncio.create_task(_wait_cancel())],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                resp = done.pop().result()  # raises CancelledError if cancel won
            else:
                resp = await client.post(llm_url, json=payload)

            if resp.status_code != 200:
                body = resp.text[:1000]
                logger.error("LLM HTTP %d from %s — body: %s", resp.status_code, llm_url, body)
            resp.raise_for_status()

        resp_json = resp.json()
        _c0 = (resp_json.get("choices") or [{}])[0]
        _content = (_c0.get("message", {}).get("content", "") if isinstance(_c0, dict) else "")
        trace_ctx["response"] = _content
        usage = resp_json.get("usage") or {}
        prompt_tokens = _usage_int(usage.get("prompt_tokens"))
        completion_tokens = _usage_int(usage.get("completion_tokens"))
        total_tokens = _usage_int(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)
        duration_ms = (time.perf_counter() - start) * 1000
        trace_ctx["usage"] = usage
        logger.info(
            "LLM tokens page=%d/%d prompt=%d completion=%d total=%d elapsed=%.0fms",
            page_num, total_pages, prompt_tokens, completion_tokens, total_tokens, duration_ms,
        )
        if pool is not None:
            context = pipeline_context or {}
            try:
                await db_mod.record_llm_usage(
                    pool,
                    doc_id=context.get("doc_id") or context.get("document_id"),
                    extraction_id=context.get("extraction_id"),
                    vendor_id=context.get("vendor_id"),
                    page_num=page_num,
                    total_pages=total_pages,
                    call_type="extraction",
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    duration_ms=duration_ms,
                    llm_url=llm_url,
                    request_id=context.get("request_id") or context.get("job_id"),
                    # Override billing to admin when admin is the uploader
                    billing_user_id=billing_user_id or context.get("billing_user_id"),
                )
            except Exception as exc:
                logger.warning("Failed to record LLM usage for extraction page %s: %s", page_num, exc)

    _choices = resp_json.get("choices") or []
    if not _choices or not isinstance(_choices[0], dict) or "message" not in _choices[0]:
        logger.error(
            "LLM response missing choices[0].message — raw: %s",
            str(resp_json)[:500],
        )
        return {}
    raw: str = (_choices[0].get("message") or {}).get("content", "").strip()
    raw = _JSON_FENCE_RE.sub("", raw)
    raw = _FENCE_END_RE.sub("", raw)
    raw = raw.strip()

    # Attempt JSON parse with progressive fallback
    try:
        parsed = json.loads(raw)
        if pipeline_context is not None:
            fields = parsed.get("fields", parsed) if isinstance(parsed, dict) else {}
            fc = len([k for k in fields.keys() if k != "line_items"]) if isinstance(fields, dict) else 0
            li = len(fields.get("line_items") or []) if isinstance(fields, dict) else 0
            logger.info("Parsed page %d/%d: %d fields, %d line items", page_num, total_pages, fc, li)
        return _strip_newlines(parsed)
    except json.JSONDecodeError:
        # Fallback 1: fix leading-zero numbers (e.g. 0070 -> "0070")
        raw_fixed = re.sub(r'([\[:,]\s*)(-?0[0-9]+)(\s*[\]},])', r'\1"\2"\3', raw)
        try:
            parsed = json.loads(raw_fixed)
            logger.warning("LLM JSON recovered via leading-zero fix (page %d)", page_num)
            return _strip_newlines(parsed)
        except json.JSONDecodeError:
            pass

        # Fallback 2: try json_repair if available
        try:
            from json_repair import repair_json
            repaired = repair_json(raw, return_objects=True)
            if isinstance(repaired, dict):
                logger.warning("LLM JSON recovered via json_repair (page %d)", page_num)
                return _strip_newlines(repaired)
        except ImportError:
            pass
        except Exception:
            pass

        # All recovery attempts failed
        logger.error("LLM JSON parse failed after all fallbacks. Raw response:\n%s", raw[:500])
        if pipeline_context is not None:
            logger.error("LLM JSON parse failed on page %d — raw: %s", page_num, raw[:200])
        raise ValueError(f"LLM returned invalid JSON: {raw[:200]}")


# ── Extraction Orchestration ─────────────────────────────────────────

async def extract_document(
    pages: list[dict],
    header_fields: list[str],
    line_item_fields: list[str],
    system_prompt: str,
    format_type: str,
    llm_url: str,
    model: str,
    on_page_done: Callable[[int, int, dict | None], Awaitable[None]] | None = None,
    cancel_event: asyncio.Event | None = None,
    start_from_page: int = 1,
    existing_page_results: list[dict] | None = None,
    pipeline_context: dict | None = None,
    pool: Any | None = None,
    system_prompt_page1: str | None = None,
) -> dict:
    """
    Process pages in parallel batches of 2 (matches --parallel 2 on llama-server).
    
    Ordering guarantees:
    - asyncio.gather returns results in INPUT order (page 1 before page 2)
    - Pages already successful in existing_page_results are skipped on retry
    - If any page in a batch fails, processing stops (no further batches)
    - merge_results always receives pages in page_number order
    
    Returns {"result": ..., "page_results": [...], "cancelled": bool, "last_completed_page": int}.
    """
    PARALLEL_BATCH = 1  # Sequential: process page 1 before page 2

    total = len(pages)
    page_results: list[dict] = list(existing_page_results or [])
    cancelled = False
    batch_had_failure = False

    if total == 0:
        return {"result": None, "page_results": []}

    # Build set of already-successful page numbers (from previous runs)
    # so we skip them on retry instead of re-processing
    already_done: set[int] = {
        pr.get("_page") for pr in page_results
        if "_error" not in pr and pr.get("_page") is not None
    }

    # Remove any ERROR results from existing_page_results — they'll be retried
    page_results = [pr for pr in page_results if "_error" not in pr]

    # Pages that still need processing (not yet successful)
    pending_pages = [
        p for p in pages
        if p["page_number"] >= start_from_page and p["page_number"] not in already_done
    ]

    logger.info("Parallel extraction: %d pages pending, %d already done, batch_size=%d",
                len(pending_pages), len(already_done), PARALLEL_BATCH)

    # Helper — runs inside asyncio.gather, never raises
    async def _process_page(page: dict) -> dict:
        page_num = page["page_number"]
        # Page 1 uses a different prompt that also requests bounding boxes
        is_page1 = page_num == 1
        effective_prompt = system_prompt_page1 if (is_page1 and system_prompt_page1) else system_prompt

        with trace_page_extraction(page_num, total) as page_ctx:
            # Build user message with tracing
            with trace_build_user_message(page_num, total) as msg_ctx:
                user_msg = build_user_message(
                    header_fields, line_item_fields, page_num, total,
                    include_boxes=is_page1,
                )
                msg_ctx["user_message"] = user_msg

            page_ctx["user_message"] = user_msg

            try:
                result = await call_llm(
                    page["image_b64"], effective_prompt, user_msg, llm_url, model,
                    mime_type=page.get("mime_type", "image/jpeg"),
                    page_num=page_num, total_pages=total,
                    pipeline_context=pipeline_context,
                    cancel_event=cancel_event,
                    pool=pool,
                )
                result["_page"] = page_num
                result["_total_pages"] = total
                # Count line items for logging (handle both v3 and legacy format)
                _fields = result.get("fields", result)
                _li = (_fields.get("line_items") or []) if isinstance(_fields, dict) else []
                _li_count = len(_li)
                logger.info("Page %d/%d done — %d line item(s)",
                            page_num, total, _li_count)
                if pipeline_context is not None:
                    _header_out = {
                        k: v for k, v in _fields.items()
                        if k not in ("line_items", "boxes", "vendor_confirmed")
                        and not k.startswith("_")
                    } if isinstance(_fields, dict) else {}
                    logger.info("Page %d/%d extracted: %d line items | %s",
                                page_num, total, _li_count,
                                ", ".join(f"{k}={v}" for k, v in _header_out.items()) if _header_out else "(no fields)")
                page_ctx["result"] = result
                return result
            except asyncio.CancelledError:
                logger.info("Page %d LLM call cancelled by user", page_num)
                page_ctx["error"] = "cancelled"
                return {"_page": page_num, "_total_pages": total, "_error": "cancelled"}
            except (ValueError, httpx.HTTPError) as exc:
                logger.error("Page %d extraction failed: %s", page_num, exc)
                page_ctx["error"] = str(exc)
                return {"_page": page_num, "_total_pages": total, "_error": str(exc)}

    # Process in batches of PARALLEL_BATCH
    for batch_start in range(0, len(pending_pages), PARALLEL_BATCH):
        batch = pending_pages[batch_start : batch_start + PARALLEL_BATCH]

        # Check cancellation before each batch
        if cancel_event and cancel_event.is_set():
            cancelled = True
            logger.info("Extraction cancelled before batch starting page %d", batch[0]["page_number"])
            break

        # Fire all pages in this batch concurrently — results come back in INPUT order
        batch_results = await asyncio.gather(*[_process_page(p) for p in batch])

        # Collect results and notify frontend (always in page order)
        for page_result in batch_results:
            page_results.append(page_result)
            if on_page_done:
                try:
                    await on_page_done(page_result["_page"], total, page_result)
                except Exception as exc:
                    logger.error("on_page_done callback failed page %s: %s", page_result.get("_page"), exc)

        # If ANY page in the batch failed, stop processing further batches
        if any("_error" in r for r in batch_results):
            batch_had_failure = True
            logger.warning("Batch had failures — stopping extraction. Failed pages: %s",
                           [r["_page"] for r in batch_results if "_error" in r])
            break

    # Sort all page_results by page number for correct merge order
    page_results.sort(key=lambda pr: pr.get("_page", 0))

    # Compute last_completed_page = highest page with all pages before it also successful
    # e.g., pages [1✓, 2✗, 3✓] → last_completed_page = 1 (not 3)
    last_completed_page = 0
    for pn in range(1, total + 1):
        pr = next((r for r in page_results if r.get("_page") == pn), None)
        if pr and "_error" not in pr:
            last_completed_page = pn
        else:
            break  # Gap found — stop counting

    # ── Build final result based on format ──
    if format_type == "po_per_page":
        final = [
            pr.get("fields") if pr.get("fields") is not None
            else {k: v for k, v in pr.items() if not k.startswith("_")}
            for pr in page_results if "_error" not in pr
        ]
    elif format_type == "single_page" and len(page_results) == 1:
        if "_error" in page_results[0]:
            final = None
        else:
            pr0 = page_results[0]
            final = pr0.get("fields") if pr0.get("fields") is not None \
                else {k: v for k, v in pr0.items() if not k.startswith("_")}
    else:
        # single_po_multipage — merge header from page 1 + line_items from all
        # merge_results already filters out _error pages internally
        with trace_merge_results(total, format_type) as merge_ctx:
            final = merge_results(page_results, header_fields, line_item_fields)
            if isinstance(final, dict):
                # v3: line_items under fields, legacy: at top level
                _f = final.get("fields", final)
                merge_ctx["merged_line_items"] = len(_f.get("line_items", []))
                merge_ctx["merged_fields"] = len([
                    k for k in (final.get("fields", final)).keys()
                    if k not in ("line_items", "_format", "boxes")
                ])

    final = normalize_header_values(final)

    return {
        "result": final,
        "page_results": page_results,
        "cancelled": cancelled or batch_had_failure,
        "last_completed_page": last_completed_page,
    }


# ── Result Merger ────────────────────────────────────────────────────

def _value_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        lines: list[str] = []
        for nested_value in value.values():
            lines.extend(_value_lines(nested_value))
        return lines
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            lines.extend(_value_lines(item))
        return lines

    text = str(value).strip()
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _combine_header_value(value: Any) -> Any:
    if not isinstance(value, (dict, list)):
        return value

    lines = _value_lines(value)
    deduped: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return "\n".join(deduped) if deduped else None


def normalize_header_values(result: Any) -> Any:
    """Flatten nested header field values while leaving line_items unchanged."""
    if isinstance(result, list):
        return [normalize_header_values(record) for record in result]
    if not isinstance(result, dict):
        return result

    normalized: dict[str, Any] = {}
    for key, value in result.items():
        normalized[key] = value if key == "line_items" else _combine_header_value(value)
    return normalized


def merge_results(
    page_results: list[dict],
    header_fields: list[str],
    line_item_fields: list[str],
) -> dict:
    """
    Merge multi-page results.

    Supports both v3 format ({fields, boxes}) and legacy flat format.
    - Header values from page 1
    - Line items concatenated from all pages
    - Boxes merged per-page with page association
    """
    valid_pages = [pr for pr in page_results if "_error" not in pr]
    if not valid_pages:
        return {}

    _meta_keys = {"_page", "_total_pages", "_error", "line_items", "fields", "boxes"}

    first_page = valid_pages[0]
    is_v3 = "fields" in first_page and isinstance(first_page.get("fields"), dict)

    if is_v3:
        # ── v3 format: return JUST the merged fields ──
        # Boxes and formats are now handled exclusively via page_results
        merged_fields: dict[str, Any] = {}

        # Header from page 1
        first_fields = first_page.get("fields", {})
        if header_fields:
            for f in header_fields:
                merged_fields[f] = first_fields.get(f)
        else:
            for key, val in first_fields.items():
                if key != "line_items":
                    merged_fields[key] = val

        # Line items from all pages
        all_items: list[dict] = []
        for pr in valid_pages:
            pr_fields = pr.get("fields", {})
            items = pr_fields.get("line_items")
            page_num = pr.get("_page")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        item_copy = dict(item)
                        if page_num is not None:
                            item_copy["_page"] = page_num
                        all_items.append(item_copy)
                    else:
                        all_items.append(item)
        merged_fields["line_items"] = all_items

        return normalize_header_values(merged_fields)
    else:
        # ── Legacy flat format ──
        merged: dict[str, Any] = {}
        if header_fields:
            for f in header_fields:
                merged[f] = first_page.get(f)
        else:
            for key, val in first_page.items():
                if key not in _meta_keys:
                    merged[key] = val

        all_items = []
        for pr in valid_pages:
            items = pr.get("line_items")
            page_num = pr.get("_page")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        item_copy = dict(item)
                        if page_num is not None:
                            item_copy["_page"] = page_num
                        all_items.append(item_copy)
                    else:
                        all_items.append(item)
        merged["line_items"] = all_items
        return normalize_header_values(merged)
