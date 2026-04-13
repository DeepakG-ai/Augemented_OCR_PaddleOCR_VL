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
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

try:
    from . import cache as cache_mod
    from . import db as db_mod
    from .phoenix_tracing import (
        trace_llm_call,
        trace_page_extraction,
        trace_build_user_message,
        trace_merge_results,
    )
except ImportError:
    import cache as cache_mod
    import db as db_mod
    from phoenix_tracing import (
        trace_llm_call,
        trace_page_extraction,
        trace_build_user_message,
        trace_merge_results,
    )

logger = logging.getLogger("extractor")

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
            json.dumps(ex["corrected_result"], indent=2, ensure_ascii=False)
            for ex in gold_examples[:2]  # max 2 to save context window
        )
        gold_section = f"""
<verified_examples>
The following are human-verified correct extractions for this vendor's documents.
Use them as reference for field formatting, value style, and expected output structure:

{examples_json}
</verified_examples>"""

    return f"""You are a highly accurate document data extraction assistant.
Extract ONLY what is explicitly visible in the document image.
Never guess or fabricate data. If a field is not visible, set it to null.
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
- Return ONLY valid JSON. No markdown fences, no explanation, no extra text.
- Use null for missing fields, never omit them.
- For line_items, return an array even if only one item exists.
- Numbers should be numeric (not strings) when possible.
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
        template: dict[str, Any] = {}
        for f in header_fields:
            template[f] = None
        if line_item_fields:
            template["line_items"] = [{col: "" for col in line_item_fields}]

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
If any field is empty or not visible, return null in the JSON object.
{header_section}
{line_section}

<json_template>
{json.dumps(template, indent=2)}
</json_template>

<rules>
- Empty or missing cells → null.
- Numbers (qty, unit_cost, amount, unit_price, etc.) must be numbers, not strings.
- Extract every visible line item row.
</rules>

Return ONLY valid JSON matching EXACTLY the structure above."""

    else:
        # ── Auto Extract mode ──
        return f"""Extract ALL data from this invoice/purchase order document (page {page_num} of {total_pages}).

<critical>
Count the number of rows in the line items table FIRST, then extract that exact number of items.
</critical>

<output_format>
Return JSON with:
- Header fields: extract all visible header fields (po_number, order_date, vendor, bill_to, ship_to, etc.)
- Line items: extract all visible line item rows with all their columns
</output_format>

<accuracy>
- Before extraction: Count total rows in the table visually.
- After extraction: Verify your line_items array has that many items.
- Double-check you didn't skip rows at page breaks or table headers.
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
    pool, redis_client, vendor_id: str,
    header_fields: list[str], line_item_fields: list[str],
    instructions: str | None, rules: list[str], format_type: str,
) -> tuple[str, str]:
    """Returns (system_prompt, prompt_hash). Cache: Redis → DB → build.

    Includes gold examples from past human corrections in the prompt.
    """

    # Fetch gold examples for this vendor (max 2)
    gold_examples = await db_mod.get_gold_examples(pool, vendor_id, limit=2)

    prompt_hash = compute_prompt_hash(
        header_fields, line_item_fields, instructions, rules, format_type,
        gold_examples=gold_examples,
    )

    # 1. Redis
    cached = await cache_mod.get_cached_prompt(redis_client, vendor_id, prompt_hash)
    if cached:
        logger.info("Prompt cache HIT (Redis) vendor=%s hash=%s gold=%d", vendor_id, prompt_hash[:12], len(gold_examples))
        return cached, prompt_hash

    # 2. DB
    tmpl = await db_mod.get_template(pool, vendor_id)
    if tmpl and tmpl.get("prompt_hash") == prompt_hash and tmpl.get("system_prompt"):
        logger.info("Prompt cache HIT (DB) vendor=%s hash=%s gold=%d", vendor_id, prompt_hash[:12], len(gold_examples))
        await cache_mod.set_cached_prompt(redis_client, vendor_id, prompt_hash, tmpl["system_prompt"])
        return tmpl["system_prompt"], prompt_hash

    # 3. Build fresh (includes gold examples)
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
    await cache_mod.set_cached_prompt(redis_client, vendor_id, prompt_hash, system_prompt)

    return system_prompt, prompt_hash


# ── LLM Call ─────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?", re.MULTILINE)
_FENCE_END_RE = re.compile(r"\n?```\s*$", re.MULTILINE)


async def call_llm(
    image_b64: str, system_prompt: str, user_message: str,
    llm_url: str, model: str, mime_type: str = "image/jpeg",
    page_num: int = 0, total_pages: int = 0,
) -> dict:
    """POST to LLM, strip markdown fences, return parsed JSON dict."""

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
        "temperature": 0.7,
        "top_p": 0.8,
        "presence_penalty": 1.5,
        "max_tokens": 6000,
    }

    with trace_llm_call(model, messages, temperature=0.7, page_num=page_num, total_pages=total_pages) as trace_ctx:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(llm_url, json=payload)
            if resp.status_code != 200:
                body = resp.text[:1000]
                logger.error("LLM HTTP %d from %s — body: %s", resp.status_code, llm_url, body)
            resp.raise_for_status()

        resp_json = resp.json()
        trace_ctx["response"] = resp_json["choices"][0]["message"]["content"]
        trace_ctx["usage"] = resp_json.get("usage", {})

    raw: str = resp_json["choices"][0]["message"]["content"].strip()
    raw = _JSON_FENCE_RE.sub("", raw)
    raw = _FENCE_END_RE.sub("", raw)
    raw = raw.strip()

    # Fix invalid JSON: numbers with leading zeros (e.g. 0070 -> "0070")
    raw = re.sub(r'([\[:,]\s*)(-?0[0-9]+)(\s*[\]},])', r'\1"\2"\3', raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("LLM JSON parse failed. Raw response:\n%s", raw[:500])
        raise ValueError(f"LLM returned invalid JSON: {raw[:200]}") from exc


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
    PARALLEL_BATCH = 2  # Match llama-server --parallel value

    total = len(pages)
    page_results: list[dict] = list(existing_page_results or [])
    cancelled = False
    batch_had_failure = False

    if total == 0:
        return {"result": None, "page_results": []}

    # Build set of already-successful page numbers (from previous runs)
    # so we skip them on retry instead of re-processing
    already_done: set[int] = {
        pr["_page"] for pr in page_results if "_error" not in pr
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
                )
                result["_page"] = page_num
                result["_total_pages"] = total
                logger.info("Page %d/%d done — %d line item(s)",
                            page_num, total, len(result.get("line_items") or []))
                page_ctx["result"] = result
                return result
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
                await on_page_done(page_result["_page"], total, page_result)

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
        final = page_results
    elif format_type == "single_page" and len(page_results) == 1:
        final = page_results[0] if "_error" not in page_results[0] else None
    else:
        # single_po_multipage — merge header from page 1 + line_items from all
        # merge_results already filters out _error pages internally
        with trace_merge_results(total, format_type) as merge_ctx:
            final = merge_results(page_results, header_fields, line_item_fields)
            if isinstance(final, dict):
                merge_ctx["merged_line_items"] = len(final.get("line_items", []))
                merge_ctx["merged_fields"] = len([
                    k for k in final.keys() if k != "line_items"
                ])

    return {
        "result": final,
        "page_results": page_results,
        "cancelled": cancelled or batch_had_failure,
        "last_completed_page": last_completed_page,
    }


# ── Result Merger ────────────────────────────────────────────────────

def merge_results(
    page_results: list[dict],
    header_fields: list[str],
    line_item_fields: list[str],
) -> dict:
    """
    Header from page 1. Line items concatenated from all pages.
    Works for both auto-extract and extract-fields mode.
    """
    valid_pages = [pr for pr in page_results if "_error" not in pr]
    if not valid_pages:
        return {}

    merged: dict[str, Any] = {}
    _meta_keys = {"_page", "_total_pages", "_error", "line_items"}

    # ── Header: from page 1 only ──
    first_page = valid_pages[0]
    if header_fields:
        for f in header_fields:
            merged[f] = first_page.get(f)
    else:
        # Auto-extract: take all non-metadata, non-list keys from page 1
        for key, val in first_page.items():
            if key not in _meta_keys:
                merged[key] = val

    # ── Line items: concat from all pages, deduplicate ──
    all_items: list[dict] = []

    for pr in valid_pages:
        items = pr.get("line_items")
        if not isinstance(items, list):
            continue
        for item in items:
            all_items.append(item)

    merged["line_items"] = all_items
    return merged
