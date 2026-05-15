"""READ-ONLY inspection for the vendor-id renumber. No writes whatsoever."""
import asyncio, os, asyncpg

DSN = os.getenv("DATABASE_URL", "postgresql://augocr:augocr@localhost:5432/augocr")


async def main():
    conn = await asyncpg.connect(DSN)
    try:
        print("=== FK constraint names referencing vendors ===")
        rows = await conn.fetch("""
            SELECT conname, conrelid::regclass::text AS tbl,
                   confupdtype, confdeltype
            FROM pg_constraint
            WHERE confrelid = 'vendors'::regclass
            ORDER BY tbl
        """)
        for r in rows:
            print(f"  {r['tbl']:<22} {r['conname']:<32} "
                  f"upd={r['confupdtype']} del={r['confdeltype']}")

        print("\n=== vendors (id, name, user_id, client_seq, created_at) ===")
        vs = await conn.fetch("""
            SELECT id, name, user_id, client_seq, created_at
            FROM vendors ORDER BY created_at, id
        """)
        print(f"  total vendors: {len(vs)}")
        for v in vs:
            print(f"  id={v['id']!r:<14} seq={v['client_seq']} "
                  f"user={str(v['user_id'])[:8]} name={v['name']!r}")

        print("\n=== jobs queued/running (must be 0 to renumber) ===")
        jq = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE status IN ('queued','running')")
        print(f"  active jobs: {jq}")

        print("\n=== spatial_memory layout_key samples ===")
        sm = await conn.fetch(
            "SELECT vendor_id, layout_key FROM spatial_memory LIMIT 20")
        print(f"  spatial_memory rows (sample {len(sm)}):")
        for s in sm:
            print(f"    vendor_id={s['vendor_id']!r} layout_key={s['layout_key']!r}")
        smc = await conn.fetchval("SELECT count(*) FROM spatial_memory")
        print(f"  spatial_memory total rows: {smc}")

        print("\n=== child-table row counts ===")
        for t in ("templates", "documents", "extractions", "vendor_aliases",
                  "spatial_memory", "qwen_layout_boxes", "llm_usage"):
            c = await conn.fetchval(f"SELECT count(*) FROM {t}")
            print(f"  {t:<20} {c}")

        print("\n=== duplicate (user_id, lower(name)) — blocks name-unique idx ===")
        dups = await conn.fetch("""
            SELECT user_id, lower(name) k, count(*) n, array_agg(id) ids
            FROM vendors WHERE user_id IS NOT NULL
            GROUP BY user_id, lower(name) HAVING count(*) > 1
        """)
        print(f"  duplicate groups: {len(dups)}")
        for d in dups:
            print(f"    {d['k']!r} -> {d['ids']}")
    finally:
        await conn.close()


asyncio.run(main())
