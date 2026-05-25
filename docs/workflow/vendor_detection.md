# Vendor Detection

> Source file: [backend/vendor_detector.py](../../backend/vendor_detector.py)
>
> Called from: `POST /detect-vendor` and the auto-detect path in `POST /ingest/ui`.
>
> Tenant isolation depends on this module. Read [isolation.md](isolation.md) first.

---

## What this module does

Given the words extracted from page 1 of a document, return the best-matching vendor — or `None` if nothing matches confidently.

```
page_words = [
    {"text": "ACME", "box": [...]},
    {"text": "Industries", "box": [...]},
    {"text": "Inc", "box": [...]},
    ...
]
        │
        ▼
detect_vendor(pool, page_words, user_id)
        │
        ├──> _load_detection_aliases(pool, user_id)  ← scoped DB query
        │      ├── vendor_aliases (manual UI patterns)
        │      └── vendors.name (implicit fallback alias)
        │
        ├──> _detect_exact(aliases, text_blob)        ← substring match
        │
        └──> _detect_fuzzy(aliases, text_blob)        ← rapidfuzz WRatio
        │
        ▼
VendorMatch | None
```

---

## Why two phases?

**Exact match first** — if the document literally contains the alias text, we want a deterministic match. Cheap (substring search), zero false-positives, fast.

**Fuzzy fallback** — handles OCR errors, hyphenation, typos. Uses `rapidfuzz.fuzz.WRatio`, a weighted combination of substring/token-based ratios.

The order matters: a fuzzy match that happens to score higher than an exact match would be wrong. So we always prefer exact when present.

---

## Tenant scoping (the critical bit)

Without scoping, this module would search all aliases across all tenants — a Client B's document with the word "ACME" might detect Client A's "ACME Industries" vendor and leak data across tenants.

Solution: pass `user_id` through the entire chain. Lines 234–263 in `vendor_detector.py`:

```python
async def detect_vendor(
    pool,
    page_words: list[dict],
    user_id: str | None = None,    # None = admin (no filter)
) -> VendorMatch | None:
    ...
    aliases = await _load_detection_aliases(pool, user_id=user_id)
```

```python
async def _load_detection_aliases(pool, user_id=None) -> list[dict]:
    aliases = await db_mod.get_all_aliases_for_detection(pool, user_id=user_id)
    ...
    vendors = await db_mod.list_vendors(pool, user_id=user_id)
```

Both queries filter by `user_id`. So:
- **Client request** → only their own vendors and aliases are considered → no cross-tenant detection possible.
- **Admin request** → `user_id=None` → all vendors and aliases visible → admin can debug across tenants.

The defence-in-depth: even after detection succeeds, the route handler in `main.py` runs `assert_vendor_access(pool, match.vendor_id, user)` to confirm. If the scoping query has any bug, the assertion catches it.

---

## Constants (lines 23–27)

```python
MIN_SCORE = 1                     # minimum exact-match weight
FUZZY_MIN_SCORE = 88.0            # minimum WRatio score (out of 100)
FUZZY_MIN_MARGIN = 5.0            # required gap between best and second-best
FUZZY_MIN_PATTERN_CHARS = 5       # don't fuzzy-match very short patterns
FUZZY_MAX_WINDOW_EXTRA = 2        # window size = pattern_words ± 2
```

These are tuned empirically. Tighten `FUZZY_MIN_SCORE` if you see false positives; loosen if real matches are getting rejected. The margin guard (line 207–217) protects against ambiguity — if two vendors both score ~89, neither wins.

---

## `VendorMatch` — the return type

```python
@dataclass
class VendorMatch:
    vendor_id: str
    vendor_name: str
    score: float
    matched_patterns: list[str]
    match_type: str  # 'exact' | 'fuzzy' | 'unknown'
```

The `matched_patterns` field is non-essential for callers but invaluable for debugging — `["fuzzy:robert scott~rbert scot:91.5"]` tells you exactly why the match fired.

---

## Step 1: Normalize the text (lines 39–41)

```python
def _normalize_text(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", value).strip()
```

Lowercase, replace non-alphanumeric with space, collapse whitespace. So `"ACME-Industries, Inc."` and `"acme  industries inc"` both become `"acme industries inc"`.

This is applied to:
1. The full page-1 text (joined word texts).
2. Every alias pattern.

Same normalization on both sides means equality and substring tests are stable across formatting variations.

---

## Step 2: Load aliases (lines 104–121)

```python
async def _load_detection_aliases(pool, user_id=None):
    aliases = await db_mod.get_all_aliases_for_detection(pool, user_id=user_id)
    if not aliases:
        logger.warning("No vendor aliases configured in DB - falling back to vendor names")

    vendors = await db_mod.list_vendors(pool, user_id=user_id)
    for v in vendors:
        aliases.append({
            "vendor_id": v["id"],
            "vendor_name": v["name"],
            "pattern": v["name"],
            "weight": 1,
        })
    return aliases
```

**Vendor names are always implicit aliases.** This means a freshly-created vendor with no aliases is still detectable (as long as the vendor name appears in the document). The `vendor_aliases` table is purely additive — for additional patterns the user wants to catch ("rd jet llc", "jetro", common abbreviations).

---

## Step 3: Exact match (lines 124–170)

```python
def _detect_exact(aliases, text_blob):
    scores = {}
    for alias in aliases:
        variants = _alias_variants(alias["pattern"])  # normalized variants
        if not variants:
            continue
        matched_pattern = next((p for p in variants if p in text_blob), None)
        if not matched_pattern:
            continue

        vid = alias["vendor_id"]
        if vid not in scores:
            scores[vid] = {"vendor_name": alias["vendor_name"], "score": 0.0, "matched_patterns": []}
        scores[vid]["score"] += float(alias.get("weight", 1))
        scores[vid]["matched_patterns"].append(matched_pattern)
```

For each alias, do a Python substring test (`pattern in text_blob`). Each match adds the alias's `weight` to that vendor's score. Multiple aliases per vendor stack.

**`MIN_SCORE = 1`**: a single weight-1 alias is enough to win, but a vendor with 5 matched aliases will outscore a vendor with 1.

If the best score is below `MIN_SCORE`, return None (lines 149–156). In practice this only fires if all matched aliases have weight 0.

---

## Step 4: Fuzzy match (lines 173–231)

If exact returns nothing, try fuzzy. This is where it gets interesting.

### `_text_windows` (lines 49–65)

```python
def _text_windows(text_blob, pattern_word_count):
    tokens = text_blob.split()
    min_size = max(1, pattern_word_count - FUZZY_MAX_WINDOW_EXTRA)
    max_size = min(len(tokens), pattern_word_count + FUZZY_MAX_WINDOW_EXTRA)
    windows = []
    seen = set()
    for size in range(min_size, max_size + 1):
        for start in range(0, len(tokens) - size + 1):
            window = " ".join(tokens[start : start + size])
            if window not in seen:
                windows.append(window)
                seen.add(window)
    return windows
```

For a 3-word alias `"acme industries inc"`, this produces every contiguous token window of length 1–5 (words ± 2). For a 1000-word page, that's a few thousand windows.

**Why windows?** `fuzz.WRatio("acme industries inc", "the entire page text concatenated")` would score low because the surrounding noise dilutes the match. Comparing against just the relevant local window gives a clean signal.

### `_pattern_is_safe_for_fuzzy` (lines 68–73)

```python
def _pattern_is_safe_for_fuzzy(pattern):
    compact = pattern.replace(" ", "")
    if len(compact) < FUZZY_MIN_PATTERN_CHARS:
        return False
    return any(ch.isalpha() for ch in compact)
```

Skip fuzzy on:
- Very short patterns (5+ chars required) — too easy to false-match.
- Numeric-only patterns — e.g. an alias like `"2024"` would match almost anything.

### `_best_window_score` (lines 76–90)

```python
def _best_window_score(pattern, text_blob):
    pattern_words = pattern.split()
    if not pattern_words or not _pattern_is_safe_for_fuzzy(pattern):
        return 0.0, None

    best_score = 0.0
    best_window = None
    for window in _text_windows(text_blob, len(pattern_words)):
        score = fuzz.WRatio(pattern, window)
        if score > best_score:
            best_score = float(score)
            best_window = window
            if best_score == 100.0:
                break        # perfect match — no need to keep searching
    return best_score, best_window
```

Iterate all windows, keep the best score. Early-exit on a perfect 100.0.

### `_effective_fuzzy_score` (lines 93–101)

```python
def _effective_fuzzy_score(pattern, window, raw_score):
    pattern_word_count = len(pattern.split())
    score = raw_score
    if pattern_word_count > 1:
        score += min(4.0, float((pattern_word_count - 1) * 2))   # bonus for multi-word
    elif pattern != window:
        score -= 3.0                                              # penalty for single-word fuzzy
    return max(0.0, min(100.0, score))
```

Multi-word patterns get a bonus (up to +4). They're harder to match by accident, so a 90 on `"robert scott"` is more trustworthy than a 90 on `"robert"`.

Single-word patterns that didn't match exactly get a small penalty (-3). They're high-risk for false positives.

### Ambiguity guard (lines 204–217)

```python
if len(ranked) > 1:
    second_vid, second = ranked[1]
    margin = best["score"] - second["score"]
    if margin < FUZZY_MIN_MARGIN:
        logger.warning("Ambiguous fuzzy vendor match: ...")
        return None
```

If two vendors both score ~91, the system bails out rather than guessing. The user gets a 409 from `/ingest/ui` and is asked to disambiguate.

---

## Failure case: `409 unknown_vendor`

When detection returns `None`, the ingest route raises:

```python
raise HTTPException(409, detail={"reason": "unknown_vendor", ...})
```

The frontend catches this in `extract.js`:

```javascript
const match = err.message.match(/HTTP 409:\s*(.+)/s);
if (match) {
    const parsed = JSON.parse(match[1]);
    if (parsed.detail?.reason === 'unknown_vendor') {
        errorMsg = 'Unknown Vendor - No vendor matched. Create a vendor with the correct name and aliases first, then retry.';
    }
}
```

The user sees a clear message and a button to navigate to the vendors page. Detection never silently invents a vendor.

---

## Two ways to skip detection

1. **Manual vendor selection on the extraction page**: if the user picks a vendor from the dropdown before uploading, `vendor_id` is sent in the form data and the backend uses it directly without running detection.

2. **`/detect-vendor` standalone endpoint**: explicit "what vendor is this?" endpoint. Used during dev/debugging. Returns the same `VendorMatch` shape as the auto-path. After detection it runs `assert_vendor_access` to confirm ownership.

---

## Tuning the matcher

If you see false positives:
- Raise `FUZZY_MIN_SCORE` (88.0 → 90.0).
- Raise `FUZZY_MIN_MARGIN` (5.0 → 8.0).
- Add specific anti-pattern aliases (i.e. don't add aliases that overlap with other vendors' aliases).

If you see false negatives:
- Lower `FUZZY_MIN_SCORE`.
- Lower `FUZZY_MAX_WINDOW_EXTRA` if too many windows are slowing you down (rare).
- Add more aliases via the template UI for the vendor.

For one-off problem documents:
- Use the manual vendor selector on the extraction page.

---

## What this module does NOT do

- **No machine learning.** No embeddings, no classifier. Pure substring + edit-distance.
- **No multi-page detection.** Only page 1 words are sent in. (Most documents have vendor branding on page 1; if they don't, the user picks manually.)
- **No language detection.** All matching is on lowercase ASCII alphanumeric. Non-Latin scripts work only via OCR transliteration into Latin.
- **No tracking of detection outcomes.** Successful detections aren't recorded anywhere; only the resulting `vendor_id` lands on the `extraction` row.
