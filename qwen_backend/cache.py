"""
cache.py — Redis async caching layer for prompts and extraction results.
Uses redis.asyncio (pip install redis).
"""
from __future__ import annotations

import json
import os

import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")


async def get_redis() -> aioredis.Redis:
    """Create and return an async Redis client."""
    return aioredis.from_url(REDIS_URL, decode_responses=True)


# ── Prompt cache (no TTL — pinned until hash changes) ────────────────

async def get_cached_prompt(r: aioredis.Redis, vendor_id: str, prompt_hash: str) -> str | None:
    """Return cached system_prompt or None on miss."""
    key = f"prompt:{vendor_id}:{prompt_hash}"
    return await r.get(key)


async def set_cached_prompt(r: aioredis.Redis, vendor_id: str, prompt_hash: str, system_prompt: str) -> None:
    """Pin system_prompt in Redis — no expiry."""
    key = f"prompt:{vendor_id}:{prompt_hash}"
    await r.set(key, system_prompt)


async def invalidate_vendor_cache(r: aioredis.Redis, vendor_id: str) -> None:
    """Delete all prompt keys for a vendor when template is updated."""
    pattern = f"prompt:{vendor_id}:*"
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            await r.delete(*keys)
        if cursor == 0:
            break


# ── Extraction result cache (TTL 3600s) ─────────────────────────────

async def get_cached_extraction(r: aioredis.Redis, extraction_id: int) -> dict | None:
    """Return cached extraction result or None on miss."""
    key = f"extraction:{extraction_id}"
    data = await r.get(key)
    if data is None:
        return None
    return json.loads(data)


async def set_cached_extraction(r: aioredis.Redis, extraction_id: int, data: dict) -> None:
    """Cache extraction result with 1-hour TTL."""
    key = f"extraction:{extraction_id}"
    await r.set(key, json.dumps(data), ex=3600)
