"""
extractor.py -- Core prompt builder, LLM calls, result merger, prompt caching.

Fields are fully dynamic: users define header_fields and line_item_fields
freely in the UI. No hardcoded field registry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

import cache as cache_mod
import db as db_mod

logger = logging.getLogger("extractor")

# ── Prompt Builder ───────────────────────────────────────────────────

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


def build_system_prompt(
    header_fields: list[str],
    line_item_fields: list[str],
    instructions: str | None,
    rules: list[str],
    format_type: str,
) -> str:
    """
    Assemble the full reusable system prompt. Stored in DB and Redis.
    The user message (field list + JSON template) is injected per-call, NOT here.
    """
    parts: list[str] = []

    # 1. Role declaration
    parts.append(
        "You are a highly accurate document data extraction assistant. "
        "Extract ONLY what is explicitly visible in the document image. "
        "Never guess or fabricate data. If a field is not visible, set it to null."
    )

    # 2. Document context (user-supplied layout hints)
    if instructions and instructions.strip():
        parts.append(f"\nDOCUMENT CONTEXT:\n{instructions.strip()}")

    # 3. Extraction rules
    if rules:
        numbered = "\n".join(f"  {i}. {rule}" for i, rule in enumerate(rules, 1))
        parts.append(f"\nEXTRACTION RULES:\n{numbered}")

    # 4. Format description
    fmt_desc = _FORMAT_DESCRIPTIONS.get(format_type, _FORMAT_DESCRIPTIONS["single_page"])
    parts.append(f"\nDOCUMENT FORMAT:\n{fmt_desc}")

    # 5. Output rules
    parts.append(
        "\nOUTPUT RULES:\n"
        "  - Return ONLY valid JSON. No markdown fences, no explanation, no extra text.\n"
        "  - Use null for missing fields, never omit them.\n"
        "  - For line_items, return an array even if only one item exists.\n"
        "  - Numbers should be numeric (not strings) when possible.\n"
        "  - Dates should be in the format they appear in the document."
    )

    return "\n".join(parts)


def _build_json_template(
    header_fields: list[str],
    line_item_fields: list[str],
    page_num: int,
    total_pages: int,
    include_header: bool,
) -> dict[str, Any]:
    """Build the JSON template dict that the model should fill."""
    template: dict[str, Any] = {}

    if include_header:
        for f in header_fields:
            template[f] = None

    if line_item_fields:
        template["line_items"] = [{col: "" for col in line_item_fields}]

    template["_page"] = page_num
    template["_total_pages"] = total_pages
    return template


def build_user_message(
    header_fields: list[str],
    line_item_fields: list[str],
    page_num: int,
    total_pages: int,
    mode: str,
) -> str:
    """
    Per-call user message injected at runtime. NOT stored in DB.
    mode: 'header_and_items' | 'items_only' | 'full'
    """
    parts: list[str] = []

    # Page context
    if total_pages == 1:
        parts.append("This is a single-page document.")
    elif mode == "items_only":
        parts.append(
            f"You are processing page {page_num} of {total_pages} "
            f"(continuation page -- header already extracted from page 1)."
        )
    else:
        parts.append(f"You are processing page {page_num} of {total_pages}.")

    # Mode hint
    if mode == "header_and_items":
        parts.append("Extract all header fields AND line items from this page.")
    elif mode == "items_only":
        parts.append(
            "Extract ONLY the line_items table rows from this page. "
            "Do NOT repeat header fields."
        )
    else:
        parts.append("Extract all requested fields from this page.")

    # Field lists
    include_header = mode != "items_only"

    if include_header and header_fields:
        parts.append("\nHeader fields to extract:")
        for f in header_fields:
            parts.append(f"  - {f}")

    if line_item_fields:
        parts.append("\nLine item columns to extract (each row):")
        for f in line_item_fields:
            parts.append(f"  - {f}")

    # JSON template
    template = _build_json_template(
        header_fields, line_item_fields, page_num, total_pages, include_header
    )
    parts.append(f"\nReturn JSON matching this structure:\n{json.dumps(template, indent=2)}")

    return "\n".join(parts)


# ── Prompt Hash ──────────────────────────────────────────────────────

def compute_prompt_hash(
    header_fields: list[str],
    line_item_fields: list[str],
    instructions: str | None,
    rules: list[str],
) -> str:
    """SHA256 of all config inputs. Used for cache invalidation."""
    payload = json.dumps({
        "header_fields": sorted(header_fields),
        "line_item_fields": sorted(line_item_fields),
        "instructions": instructions or "",
        "rules": sorted(rules),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ── Prompt Cache Cascade: Redis -> DB -> Build ───────────────────────

async def get_or_build_system_prompt(
    pool,
    redis_client,
    vendor_id: str,
    header_fields: list[str],
    line_item_fields: list[str],
    instructions: str | None,
    rules: list[str],
    format_type: str,
) -> tuple[str, str]:
    """
    Returns (system_prompt, prompt_hash).
    Cache lookup: Redis -> DB -> build -> cache in both.
    """
    prompt_hash = compute_prompt_hash(header_fields, line_item_fields, instructions, rules)

    # 1. Redis check
    cached = await cache_mod.get_cached_prompt(redis_client, vendor_id, prompt_hash)
    if cached:
        logger.info("Prompt cache HIT (Redis) vendor=%s hash=%s", vendor_id, prompt_hash[:12])
        return cached, prompt_hash

    # 2. DB check
    tmpl = await db_mod.get_template(pool, vendor_id)
    if tmpl and tmpl.get("prompt_hash") == prompt_hash and tmpl.get("system_prompt"):
        logger.info("Prompt cache HIT (DB) vendor=%s hash=%s", vendor_id, prompt_hash[:12])
        await cache_mod.set_cached_prompt(redis_client, vendor_id, prompt_hash, tmpl["system_prompt"])
        return tmpl["system_prompt"], prompt_hash

    # 3. Build fresh
    logger.info("Prompt cache MISS -- building vendor=%s hash=%s", vendor_id, prompt_hash[:12])
    system_prompt = build_system_prompt(
        header_fields, line_item_fields, instructions, rules, format_type
    )

    # Upsert to DB
    await db_mod.upsert_template(
        pool, vendor_id, format_type,
        header_fields, line_item_fields,
        instructions, rules, system_prompt, prompt_hash,
    )

    # Set in Redis
    await cache_mod.set_cached_prompt(redis_client, vendor_id, prompt_hash, system_prompt)

    return system_prompt, prompt_hash


# ── LLM Call ─────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?", re.MULTILINE)
_FENCE_END_RE = re.compile(r"\n?```\s*$", re.MULTILINE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


async def call_llm(
    image_b64: str,
    system_prompt: str,
    user_message: str,
    llm_url: str,
    model: str,
) -> dict:
    """
    POST to LLM with OpenAI-compatible payload (vision).
    Strips markdown fences and think blocks from response.
    Returns parsed dict or raises ValueError on parse failure.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                    {"type": "text", "text": user_message},
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": 4096,
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(llm_url, json=payload)
        resp.raise_for_status()

    raw: str = resp.json()["choices"][0]["message"]["content"].strip()

    # Strip <think>...</think> blocks (Qwen reasoning)
    raw = _THINK_RE.sub("", raw).strip()

    # Strip markdown fences
    raw = _JSON_FENCE_RE.sub("", raw)
    raw = _FENCE_END_RE.sub("", raw)
    raw = raw.strip()

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
    on_page_done: Callable[[int, int], Awaitable[None]] | None = None,
) -> dict:
    """
    Orchestrate extraction based on format_type.
    Calls are sequential -- local LLM cannot handle concurrent VRAM load.

    on_page_done(page_num, total_pages) is the SSE progress callback.
    Returns {"result": ..., "page_results": [...]}.
    """
    total = len(pages)
    page_results: list[dict] = []

    if total == 0:
        logger.warning("No pages found in document for extraction.")
        return {"result": None, "page_results": []}

    if format_type == "single_po_multipage":
        for i, page in enumerate(pages):
            page_num = page["page_number"]
            if i == 0:
                mode = "header_and_items"
            else:
                mode = "items_only"

            user_msg = build_user_message(
                header_fields, line_item_fields, page_num, total, mode
            )
            try:
                result = await call_llm(
                    page["image_b64"], system_prompt, user_msg, llm_url, model
                )
                result["_page"] = page_num
                result["_total_pages"] = total
                page_results.append(result)
            except (ValueError, httpx.HTTPError) as exc:
                logger.error("Page %d extraction failed: %s", page_num, exc)
                page_results.append({
                    "_page": page_num, "_total_pages": total, "_error": str(exc),
                })

            if on_page_done:
                await on_page_done(page_num, total)

        merged = merge_results(page_results, header_fields, line_item_fields)
        return {"result": merged, "page_results": page_results}

    elif format_type == "po_per_page":
        for page in pages:
            page_num = page["page_number"]
            user_msg = build_user_message(
                header_fields, line_item_fields, page_num, total, "full"
            )
            try:
                result = await call_llm(
                    page["image_b64"], system_prompt, user_msg, llm_url, model
                )
                result["_page"] = page_num
                result["_total_pages"] = total
                page_results.append(result)
            except (ValueError, httpx.HTTPError) as exc:
                logger.error("Page %d extraction failed: %s", page_num, exc)
                page_results.append({
                    "_page": page_num, "_total_pages": total, "_error": str(exc),
                })

            if on_page_done:
                await on_page_done(page_num, total)

        return {"result": page_results, "page_results": page_results}

    else:  # single_page
        page = pages[0]
        user_msg = build_user_message(
            header_fields, line_item_fields, 1, 1, "full"
        )
        try:
            result = await call_llm(
                page["image_b64"], system_prompt, user_msg, llm_url, model
            )
            result["_page"] = 1
            result["_total_pages"] = 1
            page_results.append(result)
        except (ValueError, httpx.HTTPError) as exc:
            logger.error("Page 1 extraction failed: %s", exc)
            page_results.append({
                "_page": 1, "_total_pages": 1, "_error": str(exc),
            })
            result = None

        if on_page_done:
            await on_page_done(1, 1)

        return {"result": result, "page_results": page_results}


# ── Result Merger ────────────────────────────────────────────────────

def merge_results(
    page_results: list[dict],
    header_fields: list[str],
    line_item_fields: list[str],
) -> dict:
    """
    Merge multi-page extraction results:
      - Header fields: first non-null, non-empty value wins.
      - line_items: extend across all pages, deduplicate by first 3 columns.
      - _page / _total_pages / _error metadata is excluded because those
        keys are never in the user's field lists.
    """
    merged: dict[str, Any] = {}

    # Header fields -- first non-null wins
    for f in header_fields:
        for pr in page_results:
            if "_error" in pr:
                continue
            val = pr.get(f)
            if val is not None and val != "" and val != []:
                merged[f] = val
                break
        else:
            merged[f] = None

    # Merge line_items with deduplication
    if line_item_fields:
        all_items: list[dict] = []
        seen_keys: set[tuple] = set()
        # Use the first 3 columns (or fewer) as dedup key
        dedup_cols = line_item_fields[:3]
        for pr in page_results:
            if "_error" in pr:
                continue
            items = pr.get("line_items")
            if not isinstance(items, list):
                continue
            for item in items:
                dedup_key = tuple(
                    str(item.get(col, "")).strip().lower() for col in dedup_cols
                )
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    all_items.append(item)
        merged["line_items"] = all_items

    return merged
