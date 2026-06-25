"""Tests for backend/cache.py — the optional Redis read cache.

These never touch a live Redis: the RedisCache logic is exercised against a
small in-memory fake, and create_cache's fallback paths are verified by
monkeypatching. The point of the cache is to fail soft (never raise, never
block startup) and to preserve Python types (datetime/UUID) across the
pickle round trip.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from backend import cache as cache_mod
from backend.cache import NullCache, RedisCache, create_cache


class _FakeRedis:
    """Minimal async stand-in for redis.asyncio.Redis."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    async def scan_iter(self, match=None, count=None):
        for k in list(self.store.keys()):
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    async def aclose(self):
        pass


class _BrokenRedis:
    """A client whose every op raises — to prove the cache fails soft."""

    async def get(self, *a, **k):
        raise RuntimeError("redis down")

    async def set(self, *a, **k):
        raise RuntimeError("redis down")

    async def delete(self, *a, **k):
        raise RuntimeError("redis down")

    async def scan_iter(self, *a, **k):
        raise RuntimeError("redis down")
        yield  # pragma: no cover - makes this an async generator

    async def aclose(self):
        raise RuntimeError("redis down")


class NullCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_is_always_miss(self):
        c = NullCache()
        self.assertFalse(c.enabled)
        await c.set("k", {"v": 1}, ttl=60)
        self.assertIsNone(await c.get("k"))

    async def test_get_or_set_runs_loader_every_time(self):
        c = NullCache()
        calls = {"n": 0}

        async def loader():
            calls["n"] += 1
            return {"hello": "world"}

        self.assertEqual(await c.get_or_set("k", 60, loader), {"hello": "world"})
        self.assertEqual(await c.get_or_set("k", 60, loader), {"hello": "world"})
        self.assertEqual(calls["n"], 2)  # never cached

    async def test_delete_and_close_are_noops(self):
        c = NullCache()
        await c.delete("a", "b")
        await c.delete_pattern("a:*")
        await c.close()  # must not raise


class RedisCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_get_roundtrip_preserves_types(self):
        c = RedisCache(_FakeRedis(), "augocr")
        now = dt.datetime(2026, 6, 25, 12, 0, 0)
        uid = uuid4()
        row = {"id": uid, "created_at": now, "email": "a@b.com"}
        await c.set("user:1", row, ttl=60)
        got = await c.get("user:1")
        self.assertEqual(got, row)
        self.assertIsInstance(got["created_at"], dt.datetime)  # not a string
        self.assertEqual(got["id"], uid)

    async def test_key_is_prefixed(self):
        fake = _FakeRedis()
        c = RedisCache(fake, "augocr")
        await c.set("vendor:7", {"name": "ACME"})
        self.assertIn("augocr:vendor:7", fake.store)

    async def test_delete_removes_key(self):
        fake = _FakeRedis()
        c = RedisCache(fake, "augocr")
        await c.set("k", 1)
        await c.delete("k")
        self.assertIsNone(await c.get("k"))

    async def test_delete_pattern_removes_matching(self):
        fake = _FakeRedis()
        c = RedisCache(fake, "augocr")
        await c.set("aliases:v1", [1])
        await c.set("aliases:v2", [2])
        await c.set("vendor:v1", {"x": 1})
        await c.delete_pattern("aliases:*")
        self.assertIsNone(await c.get("aliases:v1"))
        self.assertIsNone(await c.get("aliases:v2"))
        self.assertIsNotNone(await c.get("vendor:v1"))  # untouched

    async def test_get_or_set_caches_positive_only(self):
        c = RedisCache(_FakeRedis(), "augocr")
        calls = {"n": 0}

        async def loader_value():
            calls["n"] += 1
            return {"v": 1}

        # First call loads + caches; second is served from cache.
        self.assertEqual(await c.get_or_set("k", 60, loader_value), {"v": 1})
        self.assertEqual(await c.get_or_set("k", 60, loader_value), {"v": 1})
        self.assertEqual(calls["n"], 1)

    async def test_get_or_set_does_not_cache_none(self):
        c = RedisCache(_FakeRedis(), "augocr")
        calls = {"n": 0}

        async def loader_none():
            calls["n"] += 1
            return None

        self.assertIsNone(await c.get_or_set("missing", 60, loader_none))
        self.assertIsNone(await c.get_or_set("missing", 60, loader_none))
        self.assertEqual(calls["n"], 2)  # None never cached → loader re-runs

    async def test_failures_are_swallowed(self):
        c = RedisCache(_BrokenRedis(), "augocr")
        # None of these may raise even though the client is broken.
        self.assertIsNone(await c.get("k"))
        await c.set("k", 1, ttl=60)
        await c.delete("k")
        await c.delete_pattern("k:*")
        await c.close()

    async def test_get_or_set_falls_through_on_broken_client(self):
        c = RedisCache(_BrokenRedis(), "augocr")

        async def loader():
            return {"v": 42}

        # get fails -> miss -> loader runs; set fails silently.
        self.assertEqual(await c.get_or_set("k", 60, loader), {"v": 42})


class CreateCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_returns_nullcache(self):
        with patch.object(cache_mod, "CACHE_ENABLED", False):
            c = await create_cache()
        self.assertIsInstance(c, NullCache)

    async def test_unreachable_redis_falls_back_to_nullcache(self):
        broken = AsyncMock()
        broken.ping = AsyncMock(side_effect=RuntimeError("no server"))
        with patch.object(cache_mod, "CACHE_ENABLED", True), \
             patch("redis.asyncio.from_url", return_value=broken):
            c = await create_cache()
        self.assertIsInstance(c, NullCache)  # never raises on a dead Redis

    async def test_reachable_redis_returns_rediscache(self):
        ok = AsyncMock()
        ok.ping = AsyncMock(return_value=True)
        with patch.object(cache_mod, "CACHE_ENABLED", True), \
             patch("redis.asyncio.from_url", return_value=ok):
            c = await create_cache()
        self.assertIsInstance(c, RedisCache)


class ActiveCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_active_cache_is_nullcache(self):
        # get_cache() must always return a usable cache, even before wiring.
        self.assertTrue(hasattr(cache_mod.get_cache(), "get_or_set"))

    async def test_set_active_cache_swaps_handle(self):
        original = cache_mod.get_cache()
        self.addCleanup(cache_mod.set_active_cache, original)
        marker = RedisCache(_FakeRedis(), "augocr")
        cache_mod.set_active_cache(marker)
        self.assertIs(cache_mod.get_cache(), marker)


if __name__ == "__main__":
    unittest.main()
