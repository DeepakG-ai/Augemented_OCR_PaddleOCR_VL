"""Tests for the db-layer read cache and its invalidation (Phase 4/5).

Each db read should serve a second call from cache, and the matching mutation
must invalidate it so the next read hits the DB again. A fake async pool counts
DB round trips; a fake Redis backs a real RedisCache so the get_or_set /
delete / delete_pattern paths are exercised for real.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import unittest
from datetime import timedelta, timezone
from uuid import uuid4

from backend import cache as cache_mod
from backend import db
from backend.cache import RedisCache


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int | None] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
            self.ttls.pop(k, None)

    async def scan_iter(self, match=None, count=None):
        for k in list(self.store.keys()):
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    async def aclose(self):
        pass


class _FakeConn:
    def __init__(self) -> None:
        self.fetchrow_result = None
        self.fetch_result: list = []
        self.fetchval_result = None
        self.execute_result = "UPDATE 1"
        self.counts: dict[str, int] = {"fetchrow": 0, "fetch": 0, "fetchval": 0, "execute": 0}

    async def fetchrow(self, query, *args):
        self.counts["fetchrow"] += 1
        return self.fetchrow_result

    async def fetch(self, query, *args):
        self.counts["fetch"] += 1
        return self.fetch_result

    async def fetchval(self, query, *args):
        self.counts["fetchval"] += 1
        return self.fetchval_result

    async def execute(self, query, *args):
        self.counts["execute"] += 1
        return self.execute_result

    def transaction(self):
        return _NoopTxn()


class _NoopTxn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _AcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireCtx(self.conn)


class _CacheTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fake_redis = _FakeRedis()
        self.cache = RedisCache(self.fake_redis, "augocr")
        original = cache_mod.get_cache()
        cache_mod.set_active_cache(self.cache)
        self.addCleanup(cache_mod.set_active_cache, original)
        self.conn = _FakeConn()
        self.pool = _FakePool(self.conn)


class UserCacheTests(_CacheTestBase):
    async def test_get_user_by_id_caches_then_invalidates(self):
        uid = str(uuid4())
        self.conn.fetchrow_result = {
            "id": uid, "email": "a@b.com", "role": "client",
            "is_active": True, "created_at": None, "subscription_limit": 0,
        }
        await db.get_user_by_id(self.pool, uid)
        await db.get_user_by_id(self.pool, uid)
        self.assertEqual(self.conn.counts["fetchrow"], 1)  # 2nd from cache

        await db.deactivate_user(self.pool, uid)  # must invalidate user:{uid}
        await db.get_user_by_id(self.pool, uid)
        self.assertEqual(self.conn.counts["fetchrow"], 2)  # re-fetched

    async def test_reset_password_invalidates_user(self):
        uid = str(uuid4())
        self.conn.fetchrow_result = {
            "id": uid, "email": "a@b.com", "role": "client",
            "is_active": True, "created_at": None, "subscription_limit": 0,
        }
        await db.get_user_by_id(self.pool, uid)
        await db.reset_user_password(self.pool, uid, "newhash")
        await db.get_user_by_id(self.pool, uid)
        self.assertEqual(self.conn.counts["fetchrow"], 2)

    async def test_subscription_limit_change_invalidates_user(self):
        # subscription_limit is part of the cached user row, so changing it must
        # bust the cache (otherwise a quota bump is stale for up to the TTL).
        uid = str(uuid4())
        self.conn.fetchrow_result = {
            "id": uid, "email": "a@b.com", "role": "client",
            "is_active": True, "created_at": None, "subscription_limit": 10,
        }
        await db.get_user_by_id(self.pool, uid)
        await db.update_user_subscription_limit(self.pool, uid, 50)
        await db.get_user_by_id(self.pool, uid)
        self.assertEqual(self.conn.counts["fetchrow"], 2)


class ApiKeyCacheTests(_CacheTestBase):
    async def test_positive_lookup_is_cached(self):
        self.conn.fetchrow_result = {
            "id": 1, "user_id": uuid4(), "is_active": True, "expires_at": None,
        }
        await db.verify_api_key_hash(self.pool, "hash1")
        await db.verify_api_key_hash(self.pool, "hash1")
        self.assertEqual(self.conn.counts["fetchrow"], 1)

    async def test_misses_are_not_cached(self):
        self.conn.fetchrow_result = None
        self.assertIsNone(await db.verify_api_key_hash(self.pool, "nope"))
        self.assertIsNone(await db.verify_api_key_hash(self.pool, "nope"))
        self.assertEqual(self.conn.counts["fetchrow"], 2)  # re-queried

    async def test_ttl_capped_to_expiry(self):
        soon = dt.datetime.now(timezone.utc) + timedelta(seconds=5)
        self.conn.fetchrow_result = {
            "id": 2, "user_id": uuid4(), "is_active": True, "expires_at": soon,
        }
        await db.verify_api_key_hash(self.pool, "h2")
        ttl = self.fake_redis.ttls["augocr:apikey:h2"]
        self.assertTrue(1 <= ttl <= 5)  # never longer than the key's own life

    async def test_already_expired_in_race_is_not_cached(self):
        past = dt.datetime.now(timezone.utc) - timedelta(seconds=1)
        self.conn.fetchrow_result = {
            "id": 3, "user_id": uuid4(), "is_active": True, "expires_at": past,
        }
        await db.verify_api_key_hash(self.pool, "h3")
        self.assertNotIn("augocr:apikey:h3", self.fake_redis.store)

    async def test_deactivate_invalidates_key_cache(self):
        self.conn.fetchrow_result = {
            "id": 4, "user_id": uuid4(), "is_active": True, "expires_at": None,
        }
        await db.verify_api_key_hash(self.pool, "h4")
        await db.deactivate_api_key(self.pool, 4)  # delete_pattern apikey:*
        await db.verify_api_key_hash(self.pool, "h4")
        self.assertEqual(self.conn.counts["fetchrow"], 2)

    async def test_naive_expires_at_does_not_crash(self):
        # Defensive: a naive datetime must be treated as UTC, not raise. Build it
        # from UTC-now so the cap is deterministic regardless of machine tz.
        naive = dt.datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=10)
        self.conn.fetchrow_result = {
            "id": 9, "user_id": uuid4(), "is_active": True, "expires_at": naive,
        }
        result = await db.verify_api_key_hash(self.pool, "h9")
        self.assertIsNotNone(result)
        ttl = self.fake_redis.ttls.get("augocr:apikey:h9")
        self.assertTrue(1 <= ttl <= 10)


class TouchApiKeyTests(_CacheTestBase):
    async def test_touch_is_throttled(self):
        await db.touch_api_key(self.pool, "h")
        await db.touch_api_key(self.pool, "h")
        await db.touch_api_key(self.pool, "h")
        self.assertEqual(self.conn.counts["execute"], 1)  # only first writes

    async def test_touch_different_keys_each_write_once(self):
        await db.touch_api_key(self.pool, "a")
        await db.touch_api_key(self.pool, "b")
        self.assertEqual(self.conn.counts["execute"], 2)


class VendorCacheTests(_CacheTestBase):
    async def test_list_vendors_caches_then_invalidates(self):
        self.conn.fetch_result = [{
            "id": "v1", "name": "ACME", "status": "idle",
            "user_id": None, "client_seq": 1, "created_at": None,
        }]
        await db.list_vendors(self.pool, None)
        await db.list_vendors(self.pool, None)
        self.assertEqual(self.conn.counts["fetch"], 1)

        await db.invalidate_vendor_cache("v1")
        await db.list_vendors(self.pool, None)
        self.assertEqual(self.conn.counts["fetch"], 2)

    async def test_get_vendor_caches(self):
        self.conn.fetchrow_result = {
            "id": "v1", "name": "ACME", "status": "idle",
            "user_id": None, "client_seq": 1, "created_at": None,
        }
        await db.get_vendor(self.pool, "v1")
        await db.get_vendor(self.pool, "v1")
        self.assertEqual(self.conn.counts["fetchrow"], 1)
        await db.invalidate_vendor_cache("v1")
        await db.get_vendor(self.pool, "v1")
        self.assertEqual(self.conn.counts["fetchrow"], 2)


class AliasCacheTests(_CacheTestBase):
    async def test_alias_list_caches_then_delete_invalidates(self):
        self.conn.fetch_result = [{
            "id": 1, "vendor_id": "v1", "vendor_name": "ACME",
            "pattern": "acme", "weight": 1, "source": "manual", "created_at": None,
        }]
        await db.list_vendor_aliases(self.pool, "v1")
        await db.list_vendor_aliases(self.pool, "v1")
        self.assertEqual(self.conn.counts["fetch"], 1)

        # delete_vendor_alias uses RETURNING vendor_id then invalidates.
        self.conn.fetchval_result = "v1"
        deleted = await db.delete_vendor_alias(self.pool, 1)
        self.assertTrue(deleted)
        await db.list_vendor_aliases(self.pool, "v1")
        self.assertEqual(self.conn.counts["fetch"], 2)

    async def test_delete_missing_alias_returns_false(self):
        self.conn.fetchval_result = None
        self.assertFalse(await db.delete_vendor_alias(self.pool, 999))

    async def test_detection_aliases_cache_and_invalidate(self):
        self.conn.fetch_result = [{
            "vendor_id": "v1", "vendor_name": "ACME",
            "pattern": "acme", "weight": 1, "source": "manual",
        }]
        await db.get_all_aliases_for_detection(self.pool, "u1")
        await db.get_all_aliases_for_detection(self.pool, "u1")
        self.assertEqual(self.conn.counts["fetch"], 1)
        await db.invalidate_alias_cache("v1")  # clears aliases:detect:*
        await db.get_all_aliases_for_detection(self.pool, "u1")
        self.assertEqual(self.conn.counts["fetch"], 2)


class TemplateCacheTests(_CacheTestBase):
    def _row(self):
        return {
            "id": 1, "vendor_id": "v1", "format_type": "single_po_multipage",
            "header_fields": ["po"], "line_item_fields": ["qty"],
            "prompt_instructions": None, "extraction_rules": [],
            "system_prompt": "x", "prompt_hash": "h",
            "created_at": None, "updated_at": None,
        }

    async def test_get_template_caches_then_upsert_invalidates(self):
        self.conn.fetchrow_result = self._row()
        await db.get_template(self.pool, "v1")
        await db.get_template(self.pool, "v1")
        self.assertEqual(self.conn.counts["fetchrow"], 1)  # 2nd cached

        # upsert_template itself does one fetchrow (INSERT ... RETURNING) and
        # then invalidates, so the following get re-queries.
        await db.upsert_template(
            self.pool, "v1", "single_po_multipage", ["po"], ["qty"], None, [], "sys", "hash",
        )
        await db.get_template(self.pool, "v1")
        self.assertEqual(self.conn.counts["fetchrow"], 3)


class MappingCacheTests(_CacheTestBase):
    def _row(self):
        return {
            "id": 1, "vendor_id": "v1", "template_id": None, "schema_id": None,
            "header_map": {}, "line_map": {}, "header_snapshot": [],
            "line_snapshot": [], "pending_notices": [],
            "created_at": None, "updated_at": None,
        }

    async def test_get_mapping_caches_then_upsert_invalidates(self):
        self.conn.fetchrow_result = self._row()
        await db.get_field_mapping(self.pool, "v1")
        await db.get_field_mapping(self.pool, "v1")
        self.assertEqual(self.conn.counts["fetchrow"], 1)

        await db.upsert_field_mapping(self.pool, "v1", None, {}, {}, [], [], [])
        await db.get_field_mapping(self.pool, "v1")
        self.assertEqual(self.conn.counts["fetchrow"], 3)

    async def test_set_notices_invalidates_mapping(self):
        self.conn.fetchrow_result = self._row()
        await db.get_field_mapping(self.pool, "v1")
        # set_field_mapping_notices uses execute (no fetchrow) and invalidates.
        await db.set_field_mapping_notices(self.pool, "v1", [])
        await db.get_field_mapping(self.pool, "v1")
        self.assertEqual(self.conn.counts["fetchrow"], 2)


class SubscriptionCacheTests(_CacheTestBase):
    """subscription_limit is a legacy mirror in the cached user row; explicit
    subscription mutations must bust it (the authoritative quota path reads the
    tables directly and is uncached)."""

    def _user_row(self, uid):
        return {
            "id": uid, "email": "a@b.com", "role": "client",
            "is_active": True, "created_at": None, "subscription_limit": 0,
        }

    async def test_create_subscription_invalidates_user(self):
        uid = str(uuid4())
        self.conn.fetchrow_result = self._user_row(uid)
        await db.get_user_by_id(self.pool, uid)  # warm (fetchrow 1)
        start = dt.datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = dt.datetime(2026, 2, 1, tzinfo=timezone.utc)
        await db.create_subscription(self.pool, uid, 100, start, end)  # fetchrow 2
        await db.get_user_by_id(self.pool, uid)  # busted -> fetchrow 3
        self.assertEqual(self.conn.counts["fetchrow"], 3)

    async def test_cancel_subscription_invalidates_user(self):
        uid = str(uuid4())
        self.conn.fetchrow_result = self._user_row(uid)
        self.conn.execute_result = "UPDATE 1"  # a row was actually cancelled
        await db.get_user_by_id(self.pool, uid)  # fetchrow 1
        await db.cancel_subscription(self.pool, uid, 1)  # execute only, invalidates
        await db.get_user_by_id(self.pool, uid)  # busted -> fetchrow 2
        self.assertEqual(self.conn.counts["fetchrow"], 2)


if __name__ == "__main__":
    unittest.main()
