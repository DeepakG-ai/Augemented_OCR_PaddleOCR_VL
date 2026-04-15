"""Debug script to analyze extraction 85 text matching."""
import asyncio, json, sys
sys.path.insert(0, '/app')
from qwen_backend import db as db_mod

async def main():
    pool = await db_mod.get_pool()
    ext = await db_mod.get_extraction(pool, 85)
    result = ext.get('result', {})
    page_results = ext.get('page_results', [])
    field_locs = ext.get('field_locations', {})
    ocr_data = ext.get('ocr_data', [])

    print("=== RESULT KEYS:", list(result.keys()) if isinstance(result, dict) else type(result))
    li = result.get('line_items', []) if isinstance(result, dict) else []
    print("=== LINE ITEMS COUNT:", len(li))
    if li:
        print("=== FIRST ITEM:", json.dumps(li[0], indent=2))
        print("=== LAST ITEM:", json.dumps(li[-1], indent=2))

    print("=== PAGE_RESULTS COUNT:", len(page_results) if page_results else 0)
    if page_results:
        for i, pr in enumerate(page_results):
            pr_items = pr.get('line_items', []) if isinstance(pr, dict) else []
            pg = pr.get('_page', '?')
            print(f"  page_result[{i}]: _page={pg} items={len(pr_items)}")

    print("=== OCR PAGES:", len(ocr_data))
    for p in ocr_data:
        print(f"  page {p['page_number']}: {len(p.get('words',[]))} words")

    print("=== FIELD_LOCATIONS:", len(field_locs), "entries")
    li_locs = {k:v for k,v in field_locs.items() if k.startswith('line_item_')}
    header_locs = {k:v for k,v in field_locs.items() if not k.startswith('line_item_')}
    print(f"  header locs: {len(header_locs)}")
    print(f"  line_item locs: {len(li_locs)}")

    rows_with_locs = set()
    for k in li_locs:
        parts = k.replace('line_item_', '').split('_', 1)
        rows_with_locs.add(int(parts[0]))
    print(f"  rows with at least one loc: {sorted(rows_with_locs)}")
    print(f"  total line item rows: {len(li)}")

    # Show which rows are MISSING locations entirely
    missing_rows = [i for i in range(len(li)) if i not in rows_with_locs]
    print(f"  rows with ZERO locs: {missing_rows}")

    # Show per-row detail for first few missing
    for row_idx in missing_rows[:5]:
        row = li[row_idx]
        print(f"\n  --- Missing row {row_idx} ---")
        for col, val in row.items():
            print(f"    {col}: {repr(val)}")

    # Show page distribution of matched line item locs
    page_dist = {}
    for k, v in li_locs.items():
        pg = v.get('page', '?')
        page_dist[pg] = page_dist.get(pg, 0) + 1
    print(f"\n=== LINE ITEM LOC PAGE DISTRIBUTION: {page_dist}")

    await pool.close()

asyncio.run(main())
