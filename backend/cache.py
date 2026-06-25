"""
cache.py -- optional Redis read-through cache with a safe no-op fallback.

The database is remote, so every query is a cross-region round trip. This
module caches hot, read-mostly rows (auth lookups, vendor/template/alias/
mapping definitions) so the API and the pipeline workers can share the same
warm keys. Invalidation is explicit on the matching mutation; the TTLs are a
safety net.

Safety rules baked in here:
  * If Redis is disabled (CACHE_ENABLED=false) or unreachable, every call
    degrades to a no-op (NullCache) and callers fall through to Postgres. A
    Redis outage must never take the API down.
  * Every Redis call is wrapped so a transient error returns a miss, not an
    exception.
  * Values are pickled. This is safe ONLY because Redis is private
    (localhost / authenticated) and we only ever store our own DB rows --
    never untrusted input. If Redis is ever exposed, switch to JSON encoding.
  * get_or_set caches positive results only -- a None loader result (e.g. "no
    template") is never stored, so misses re-check the DB.

Access pattern: a single process-wide cache is held here. main.py (API) and
worker.py (pipeline) each call `set_active_cache(await create_cache())` at
startup; db.py reads it via `get_cache()`.
"""
from __future__ import annotations

import logging
import pickle
from typing import Any, Awaitable, Callable

from .config import CACHE_ENABLED, CACHE_PREFIX, REDIS_URL

logger = logging.getLogger(__name__)


class NullCache:
    """No-op cache used when Redis is disabled or unavailable."""

    enabled = False

    async def get(self, key: str) -> Any | None:
        return None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        return None

    async def delete(self, *keys: str) -> None:
        return None

    async def delete_pattern(self, pattern: str) -> None:
        return None

    async def get_or_set(
        self, key: str, ttl: int | None, loader: Callable[[], Awaitable[Any]]
    ) -> Any:
        return await loader()

    async def close(self) -> None:
        return None


class RedisCache:
    """Async Redis-backed cache. All operations fail soft to a miss/no-op."""

    enabled = True

    def __init__(self, client: Any, prefix: str) -> None:
        self._r = client
        self._prefix = prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._r.get(self._k(key))
            return pickle.loads(raw) if raw is not None else None
        except Exception as exc:  # noqa: BLE001 - fail soft to a cache miss
            logger.warning("cache get failed key=%s: %s", key, exc)
            return None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        try:
            data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            if ttl and ttl > 0:
                await self._r.set(self._k(key), data, ex=int(ttl))
            else:
                await self._r.set(self._k(key), data)
        except Exception as exc:  # noqa: BLE001 - caching is best-effort
            logger.warning("cache set failed key=%s: %s", key, exc)

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self._r.delete(*[self._k(k) for k in keys])
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache delete failed keys=%s: %s", keys, exc)

    async def delete_pattern(self, pattern: str) -> None:
        """Delete every key matching `pattern` (a glob, relative to the prefix).

        Uses SCAN (non-blocking) so it is safe for occasional invalidation.
        """
        try:
            match = self._k(pattern)
            keys = [k async for k in self._r.scan_iter(match=match, count=500)]
            if keys:
                await self._r.delete(*keys)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache delete_pattern failed pattern=%s: %s", pattern, exc)

    async def get_or_set(
        self, key: str, ttl: int | None, loader: Callable[[], Awaitable[Any]]
    ) -> Any:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await loader()
        if value is not None:  # positive-only caching
            await self.set(key, value, ttl)
        return value

    async def close(self) -> None:
        try:
            await self._r.aclose()
        except Exception:  # noqa: BLE001
            pass


# -- Process-wide handle ----------------------------------------------------

_active_cache: NullCache | RedisCache = NullCache()


def get_cache() -> NullCache | RedisCache:
    """Return the process-wide cache (NullCache until set_active_cache runs)."""
    return _active_cache


def set_active_cache(cache: NullCache | RedisCache) -> None:
    global _active_cache
    _active_cache = cache


async def create_cache() -> NullCache | RedisCache:
    """Build the cache for this process. Falls back to NullCache on any problem."""
    if not CACHE_ENABLED:
        logger.info("Cache disabled (CACHE_ENABLED=false) -- using NullCache")
        return NullCache()
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            REDIS_URL, socket_connect_timeout=2, socket_timeout=2
        )
        await client.ping()
        logger.info("Redis cache connected: %s", REDIS_URL)
        return RedisCache(client, CACHE_PREFIX)
    except Exception as exc:  # noqa: BLE001 - never block startup on Redis
        logger.warning("Redis unavailable (%s) -- falling back to NullCache", exc)
        return NullCache()
