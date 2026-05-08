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

if __package__:
    from . import db as db_mod
    from . import logging_config as plog
    from .logging_config import get_logger
    from .config import (
        LLM_TEMPERATURE,
        LLM_TOP_P,
        LLM_PRESENCE_PENALTY,
        LLM_MAX_TOKENS_FIELDS,
        LLM_TIMEOUT,
    )
    from .phoenix_tracing import (
        trace_llm_call,
        trace_page_extraction,
        trace_build_user_message,
        trace_merge_results,
    )
else:
    import db as db_mod  # type: ignore[no-redef]
    import logging_config as plog  # type: ignore[no-redef]
    from logging_config import get_logger  # type: ignore[no-redef]
    from config import (  # type: ignore[no-redef]
        LLM_TEMPERATURE,
        LLM_TOP_P,
        LLM_PRESENCE_PENALTY,
        LLM_MAX_TOKENS_FIELDS,
        LLM_TIMEOUT,
    )
    from phoenix_tracing import (  # type: ignore[no-redef]
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


# ── Format Descriptions ─────────────────────────────────────────────

_FORMAT_DESCRIPTIONS: dict[str, str] = {
    "single_po_multipage": (
        "This document is a single purchase order that spans multiple pages. "
        "Header fields appear on page 1; line items may continue across subsequent pages."
    ),
    "po_per_page": (
        "Each page of this document contains an independent purchase order. "
        "Extract each page as a separate, complete record."
    ),
    "single_page": (
        "This document is a single-page invoice or purchase order. "
        "All fields appear on this one page."
    ),
}


# v4.0 = two-agent split: Fields Agent returns {fields} only, no boxes
PROMPT_VERSION = "v4.0"

# ── System Prompt (built once, stored in DB + Redis) ─────────────────

def build_system_prompt(
    header_fields: list[str],
    line_item_fields: list[str],
    instructions: str | None,
    rules: list[str],
    format_type: str,
    gold_examples: list[dict] | None = None,
) -> str:
    """Build the reusable system prompt. Stored in DB and cached in Redis.

    Args:
        gold_examples: Optional list of human-verified correct extractions
            for this vendor. Injected as few-shot examples so the LLM learns
            from past corrections.
    """

    fmt_desc = _FORMAT_DESCRIPTIONS.get(format_type, _FORMAT_DESCRIPTIONS["single_page"])

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

    # Few-shot gold examples from human corrections
    gold_section = ""
    if gold_examples:
        examples_json = "\n---\n".join(
            json.dumps(ex["correction_diff"], indent=2, ensure_ascii=False)
            for ex in gold_examples[:2] if "correction_diff" in ex
        )
        if examples_json.strip():
            gold_section = f"""
<verified_examples>
The following are examples of human corrections tracking how raw outputs were fixed.
These represent "Diffs" in the form {{"field_name": "correct_value"}}.
Use these as hints for formatting issues or re-occurring mistakes:

{examples_json}
</verified_examples>"""

    return f"""You are a highly accurate document data extraction assistant.
This request is processed one page at a time.

Return one top-level key:
- `fields`: extracted values
{context_section}
{rules_section}
{gold_section}
<document_format>
{fmt_desc}
</document_format>

<critical>
Count the number of rows in the line items table FIRST, then extract that exact number of items.
</critical>

<output_rules>
- Extract ONLY what is explicitly visible in the document image.
- Never guess or fabricate data.
- Return ONLY valid JSON. No markdown fences, no explanation, no extra text.
- Use null for missing fields, never omit them.
- For line_items, return an array even if only one item exists.
- Numbers should be numeric (not strings) when possible.
- Identifiers (po_number, order_number, invoice_number, vendor_id, etc.) must ALWAYS be strings, even if they look numeric.
- Dates should be in the format they appear in the document.
</output_rules>"""


# ── User Message (per-call, same for every page) ────────────────────

def build_user_message(
    header_fields: list[str],
    line_item_fields: list[str],
    page_num: int,
    total_pages: int,
) -> str:
    """
    Build the user message for a single page.
    Same message for every page. Two modes based on whether fields are provided.
    """

    if header_fields or line_item_fields:
        # ── Extract Fields mode ──
        fields_template: dict[str, Any] = {}

        for f in header_fields:
            fields_template[f] = None

        if line_item_fields:
            fields_template["line_items"] = [{col: None for col in line_item_fields}]

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
- Numbers (qty, unit_cost, amount, unit_price) must be numbers, not strings.
- Identifiers (po_number, order_number, invoice_number, vendor_id, etc.) must ALWAYS be strings, even if they look numeric.
- Extract every visible line item row.
</rules>

Return ONLY valid JSON matching EXACTLY the structure above."""

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

Return ONLY valid JSON. No markdown fences, no explanation, no extra text."""


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
        "gold_examples": [
            {
                "corrected_result": ex.get("corrected_result"),
                "correction_diff": ex.get("correction_diff"),
            }
            for ex in (gold_examples or [])
        ],
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


async def get_or_build_system_prompt(
    pool, vendor_id: str,
    header_fields: list[str], line_item_fields: list[str],
    instructions: str | None, rules: list[str], format_type: str,
) -> tuple[str, str]:
    """Returns (system_prompt, prompt_hash). Cache: DB → build.

    Includes gold examples from past human corrections in the prompt.
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

    # 2. Build fresh (includes gold examples)
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
        trace_ctx["response"] = resp_json["choices"][0]["message"]["content"]
        usage = resp_json.get("usage") or {}
        prompt_tokens = _usage_int(usage.get("prompt_tokens"))
        completion_tokens = _usage_int(usage.get("completion_tokens"))
        total_tokens = _usage_int(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)
        duration_ms = (time.perf_counter() - start) * 1000
        trace_ctx["usage"] = usage
        if pipeline_context is not None:
            plog.event(
                "qwen_http_completed",
                stage="llm",
                duration_ms=duration_ms,
                page=page_num,
                total_pages=total_pages,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                usage=usage,
                response_chars=len(trace_ctx["response"]),
                **pipeline_context,
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
                )
            except Exception as exc:
                logger.warning("Failed to record LLM usage for extraction page %s: %s", page_num, exc)

    raw: str = resp_json["choices"][0]["message"]["content"].strip()
    raw = _JSON_FENCE_RE.sub("", raw)
    raw = _FENCE_END_RE.sub("", raw)
    raw = raw.strip()

    # Attempt JSON parse with progressive fallback
    try:
        parsed = json.loads(raw)
        if pipeline_context is not None:
            fields = parsed.get("fields", parsed) if isinstance(parsed, dict) else {}
            plog.event(
                "qwen_json_parsed",
                stage="llm",
                page=page_num,
                total_pages=total_pages,
                field_count=len([k for k in fields.keys() if k != "line_items"]) if isinstance(fields, dict) else 0,
                line_item_count=len(fields.get("line_items") or []) if isinstance(fields, dict) else 0,
                **pipeline_context,
            )
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
            plog.event(
                "qwen_json_parse_failed",
                stage="llm",
                status="error",
                page=page_num,
                total_pages=total_pages,
                raw_preview=raw[:500],
                **pipeline_context,
            )
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

        with trace_page_extraction(page_num, total) as page_ctx:
            # Build user message with tracing
            with trace_build_user_message(page_num, total) as msg_ctx:
                user_msg = build_user_message(header_fields, line_item_fields, page_num, total)
                msg_ctx["user_message"] = user_msg

            page_ctx["user_message"] = user_msg

            try:
                result = await call_llm(
                    page["image_b64"], system_prompt, user_msg, llm_url, model,
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
                _li_count = len(_fields.get("line_items") or [])
                logger.info("Page %d/%d done — %d line item(s)",
                            page_num, total, _li_count)
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
