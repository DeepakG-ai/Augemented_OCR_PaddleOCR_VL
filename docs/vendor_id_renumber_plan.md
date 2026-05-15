# Vendor ID Renumber Plan (REVIEW ONLY — NOT APPLIED)

Status: **draft for review.** No SQL in here has been run. Nothing touches the
DB until you explicitly approve and a safe window is chosen (demo-week rule).

## Goal

Physically rewrite `vendors.id` for **existing** vendors (currently `RS001`,
random base36, etc.) to clean numeric strings, so every vendor — old and new —
has a sequential id. **No row is deleted.** Only id *values* change, plus every
place the old id is stored as a value.

> Reminder: the already-shipped `client_seq` design means users **already** see
> old vendors as `Vendor #1, #2…`. This renumber changes the *internal* id only.
> It buys consistency, not visible behaviour. Accept the risk below knowingly.

---

## Everything that holds a vendor id (full inventory)

### A. Foreign-key columns (Postgres enforces these)

| Table | Column | Constraint today |
|---|---|---|
| `templates` | `vendor_id` | `REFERENCES vendors(id) ON DELETE CASCADE` |
| `documents` | `vendor_id` | `REFERENCES vendors(id)` |
| `extractions` | `vendor_id` | `REFERENCES vendors(id)` |
| `vendor_aliases` | `vendor_id` | `REFERENCES vendors(id) ON DELETE CASCADE` |
| `spatial_memory` | `vendor_id` | `REFERENCES vendors(id) ON DELETE CASCADE` |
| `qwen_layout_boxes` | `vendor_id` | `REFERENCES vendors(id) ON DELETE CASCADE` |
| `vendors` | `id` (PK) | parent |

**None have `ON UPDATE CASCADE`** — confirmed in [db.py](../backend/db.py). A bare
`UPDATE vendors SET id=…` is rejected. We add `ON UPDATE CASCADE` so a single
parent update propagates atomically.

### B. Vendor id stored as a value, NOT a foreign key (the silent risks)

| Location | Form | Auto-updates with cascade? |
|---|---|---|
| `llm_usage.vendor_id` | plain TEXT — FK was dropped in an earlier migration | **No** — handled manually |
| `spatial_memory.layout_key` | TEXT literal `"<vendor_id>:<template_id>"` ([layout_key.py:39](../backend/layout_key.py#L39)) | **No** — handled manually, in lockstep |
| `extractions.field_locations` | JSONB bbox map | Not believed to embed vendor_id; **verify before run** (query below) |
| Frontend `localStorage['extractVendor']` | client-side string | No — harmless, self-heals on next vendor pick |
| MLflow traces / tags, exported Excel/CSV in object store | historical artifacts | No — **accepted as stale**, not rewritten |

The `spatial_memory.layout_key` rewrite is the dangerous one. If the id changes
but `layout_key` does not, spatial memory **silently stops matching** — no error,
just degraded extraction. It must change in the same transaction.

---

## Preconditions (all required)

1. **Full DB backup taken and verified restorable.** (`pg_dump` of the `augocr` DB.)
2. **All five workers stopped** and **no extraction in flight** — a vendor id
   changing mid-pipeline corrupts the job chain. Verify:
   ```sql
   SELECT count(*) FROM jobs WHERE status IN ('queued','running');  -- expect 0
   ```
3. The additive migration (sequence + `client_seq` + name-unique index) already
   applied successfully.
4. Pre-check `field_locations` does not embed a vendor id (expect 0):
   ```sql
   SELECT count(*) FROM extractions e
   JOIN vendors v ON v.id <> '' 
   WHERE e.field_locations::text LIKE '%' || e.vendor_id || '%';
   ```
   (Heuristic — eyeball a few rows if non-zero before proceeding.)

---

## The migration (single transaction, two-phase rename)

Two-phase is required to get a clean `1..N` without transient PK collisions
(e.g. mapping old `5 → 1` while a vendor `1` still exists mid-statement).

```sql
BEGIN;

-- 0. Belt-and-braces: block concurrent writes for the duration.
LOCK TABLE vendors IN EXCLUSIVE MODE;

-- 1. Add ON UPDATE CASCADE to every FK (drop + re-add). Idempotent-safe to
--    run once; these become permanent and are desirable going forward.
ALTER TABLE templates        DROP CONSTRAINT templates_vendor_id_fkey,
  ADD CONSTRAINT templates_vendor_id_fkey
  FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE documents        DROP CONSTRAINT documents_vendor_id_fkey,
  ADD CONSTRAINT documents_vendor_id_fkey
  FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON UPDATE CASCADE;
ALTER TABLE extractions      DROP CONSTRAINT extractions_vendor_id_fkey,
  ADD CONSTRAINT extractions_vendor_id_fkey
  FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON UPDATE CASCADE;
ALTER TABLE vendor_aliases   DROP CONSTRAINT vendor_aliases_vendor_id_fkey,
  ADD CONSTRAINT vendor_aliases_vendor_id_fkey
  FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE spatial_memory   DROP CONSTRAINT spatial_memory_vendor_id_fkey,
  ADD CONSTRAINT spatial_memory_vendor_id_fkey
  FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE qwen_layout_boxes DROP CONSTRAINT qwen_layout_boxes_vendor_id_fkey,
  ADD CONSTRAINT qwen_layout_boxes_vendor_id_fkey
  FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE CASCADE ON UPDATE CASCADE;
-- NOTE: exact constraint names must be confirmed first:
--   SELECT conname, conrelid::regclass FROM pg_constraint
--   WHERE confrelid = 'vendors'::regclass;

-- 2. Build the old -> new id map (stable order = creation order).
CREATE TEMP TABLE vendor_id_map ON COMMIT DROP AS
SELECT id AS old_id,
       row_number() OVER (ORDER BY created_at, id)::text AS new_id
FROM vendors;

-- 3. PHASE 1: move every id into a collision-proof temporary namespace.
--    Prefix guarantees no overlap with any final numeric value.
UPDATE vendors v
   SET id = 'tmp__' || m.old_id
  FROM vendor_id_map m
 WHERE v.id = m.old_id;          -- FK children + nothing else cascade here

-- 3b. Rewrite the non-cascading value columns to the temp namespace too,
--     so they stay consistent through phase 2.
UPDATE llm_usage lu
   SET vendor_id = 'tmp__' || m.old_id
  FROM vendor_id_map m
 WHERE lu.vendor_id = m.old_id;

UPDATE spatial_memory sm
   SET layout_key = 'tmp__' || m.old_id || substring(sm.layout_key from position(':' in sm.layout_key))
  FROM vendor_id_map m
 WHERE split_part(sm.layout_key, ':', 1) = m.old_id;

-- 4. PHASE 2: move from temp namespace to the final numeric id.
UPDATE vendors v
   SET id = m.new_id
  FROM vendor_id_map m
 WHERE v.id = 'tmp__' || m.old_id;        -- FK children cascade automatically

UPDATE llm_usage lu
   SET vendor_id = m.new_id
  FROM vendor_id_map m
 WHERE lu.vendor_id = 'tmp__' || m.old_id;

UPDATE spatial_memory sm
   SET layout_key = m.new_id || substring(sm.layout_key from position(':' in sm.layout_key))
  FROM vendor_id_map m
 WHERE split_part(sm.layout_key, ':', 1) = 'tmp__' || m.old_id;

-- 5. Realign the global sequence so future inserts don't collide.
SELECT setval('vendors_global_id_seq',
               (SELECT MAX(id::bigint) FROM vendors WHERE id ~ '^[0-9]+$') + 1,
               false);

-- 6. VERIFY inside the transaction — rollback if anything is off.
--    a. No orphaned children:
SELECT 'templates' t, count(*) FROM templates  WHERE vendor_id NOT IN (SELECT id FROM vendors)
UNION ALL SELECT 'documents',  count(*) FROM documents   WHERE vendor_id NOT IN (SELECT id FROM vendors)
UNION ALL SELECT 'extractions',count(*) FROM extractions WHERE vendor_id NOT IN (SELECT id FROM vendors)
UNION ALL SELECT 'aliases',    count(*) FROM vendor_aliases WHERE vendor_id NOT IN (SELECT id FROM vendors)
UNION ALL SELECT 'spatial',    count(*) FROM spatial_memory WHERE vendor_id NOT IN (SELECT id FROM vendors)
UNION ALL SELECT 'qwen_boxes', count(*) FROM qwen_layout_boxes WHERE vendor_id NOT IN (SELECT id FROM vendors);
--    Every count MUST be 0.

--    b. No layout_key still in temp namespace or pointing at a dead vendor:
SELECT count(*) FROM spatial_memory
 WHERE layout_key LIKE 'tmp__%'
    OR split_part(layout_key, ':', 1) NOT IN (SELECT id FROM vendors);
--    MUST be 0.

-- If all verifications pass:
COMMIT;
-- else:
-- ROLLBACK;
```

---

## Post-run checks (after COMMIT)

- `SELECT id, name, client_seq FROM vendors ORDER BY id::bigint;` — ids are `1..N`.
- Open a vendor whose spatial memory you know exists, re-run an extraction,
  confirm a remembered field still snaps to its saved box (proves the
  `layout_key` rewrite worked — this is the silent-failure canary).
- Restart workers.

## Accepted stale (NOT rewritten — documented, not fixed)

- MLflow run tags / traces referencing old ids — historical only.
- Previously exported Excel/CSV in object storage — historical only.
- `localStorage['extractVendor']` in any open browser — self-heals on next pick.

## Rollback

Single transaction: any failed verification → `ROLLBACK` leaves the DB
**exactly** as before (the `ON UPDATE CASCADE` constraint changes also roll
back). If COMMIT already happened and a problem surfaces later, restore from
the Precondition-1 backup. There is no in-place "un-renumber" once committed.

## Residual risks even when done perfectly

1. Anything outside Postgres that cached an old id (external integrations,
   the outbound stage's already-generated files) keeps the old value.
2. If a worker was *not* actually idle, its in-flight job references a now-dead
   id → that one extraction fails and must be re-run. (Mitigated by precondition 2.)
3. Constraint names in step 1 are assumed to be Postgres defaults; they MUST be
   confirmed with the `pg_constraint` query first or the ALTERs fail.
