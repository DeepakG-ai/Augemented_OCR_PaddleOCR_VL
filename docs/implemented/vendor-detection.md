# Vendor Detection

This document covers how the system automatically identifies the vendor of an uploaded PDF using exact and fuzzy text matching on Page 1.

---

## What it is

Vendor detection is the entry point of the document understanding process. The system renders the first page of the uploaded PDF, extracts the words, and compares them against configured vendor names and aliases in the database. 

If a matching vendor is found, the system loads that vendor's extraction template and proceeds with the pipeline. If no vendor is found or the match is ambiguous, the upload fails immediately. The system **never** automatically creates a new vendor.

---

## How it works

Vendor detection executes during the initial document ingestion phase before any pipeline jobs are enqueued.

```
       [Render Page 1 of PDF]
                 │
                 ▼
       [Normalize Page 1 Text]
                 │
                 ▼
   [Load Active Aliases & Names]
                 │
                 ▼
       [Try Exact Matching] ───────── (Exact match found) ───────┐
                 │                                               │
           (No exact match)                                      │
                 │                                               │
                 ▼                                               │
       [Try Fuzzy Matching]                                      │
           (RapidFuzz)                                           │
                 │                                               │
                 ├─── (Fuzzy match found & margin safe) ─────────┤
                 │                                               │
                 ▼                                               ▼
          [No Match found]                             [Return VendorMatch]
                 │                                               │
                 ▼                                               ▼
          [HTTP 409 Error]                             [Proceed to Pipeline]
```

### Step-by-Step Match Flow

1. **Text Extraction**: The system extracts all words on Page 1 of the document (either from `pypdfium2` geometry for digital documents, or after a fast OCR run for scanned ones).
2. **Normalisation**: The raw words are joined and normalized by:
   - Converting to lowercase.
   - Replacing any non-alphanumeric character sequences with spaces.
   - Collapsing consecutive spaces and trimming.
3. **Alias Seeding**: Active candidates are loaded from the database:
   - Explicit patterns from the `vendor_aliases` table.
   - All vendor names from the `vendors` table (treated as aliases with a default weight of 5, classified under source `"vendor_name"`).
   - If a `user_id` is supplied, candidate loading is strictly restricted to that user's vendors (multi-tenant isolation).
4. **Exact Matching**:
   - The normalized pattern for each candidate is checked as a substring of the document's Page 1 normalized text.
   - Weights for all matching patterns are accumulated per vendor.
   - The vendor with the highest score is selected.
   - If the highest score is at least `MIN_SCORE = 1.0`, an exact match is returned, bypassing fuzzy matching.
5. **Fuzzy Matching**:
   - If exact matching yields nothing, the system initiates fuzzy matching using `rapidfuzz`.
   - Token-level sliding windows are extracted from the document text to match the word count of each candidate pattern.
   - `fuzz.token_sort_ratio()` scores the pattern against each window.
   - An effective score is calculated by adjusting the raw score for length and word boundaries (e.g. subtracting 3 points for one-word pattern mismatches, or adding up to 4 points for multi-word patterns).
   - If the best vendor's score is at least `FUZZY_MIN_SCORE = 88.0`, it is selected.
   - **Ambiguity Check**: If the second-best vendor's score is within `FUZZY_MIN_MARGIN = 5.0` of the top vendor, the match is rejected as ambiguous.
6. **Result**:
   - If a unique match is resolved, a `VendorMatch` object is returned detailing the `vendor_id`, `vendor_name`, `score`, and matching `match_type` (`"exact"` or `"fuzzy"`).
   - Otherwise, `None` is returned, and the system raises a `409 Conflict`.

---

## Rules & Hard Constraints

- **Multi-Tenant Isolation**: The `user_id` must be passed to `detect_vendor()` on all client uploads to ensure aliases and names are loaded only for vendors owned by that user. Non-admin users can never match another tenant's vendors.
- **Exact Matches First**: Fuzzy matching is slow and prone to noise. The exact matching loop must run first and short-circuit immediately on success.
- **Fuzzy Safety Guards**:
   - Tiny patterns (fewer than 5 characters after removing spaces) or numeric-only patterns are excluded from fuzzy matching to prevent matching invoice numbers or dates.
   - A single-word pattern matched fuzzy must match exactly or faces a 3-point score penalty.
- **Ambiguity Margin**: If the fuzzy score difference between the top two candidates is less than 5.0 points, the detection fails. This prevents mismatching close variants like "Acme Corp V1" and "Acme Corp V2" when the document text is unclear.
- **No Auto-Creation**: If no vendor matches, the server returns an HTTP 409 error. The system never creates vendors automatically during upload.

---

## All Scenarios in Plain English

### Scenario 1 — Exact match hits
- A PDF invoice contains the text `"Invoice from Canada Metal Co"`.
- The database contains a vendor alias `"Canada Metal"` with weight 3.
- Exact match checks Canada Metal, finds it in the document text, and assigns a score of 3.0.
- Since 3.0 >= `MIN_SCORE` (1.0), the system returns an exact match for Canada Metal. Fuzzy matching is skipped.

### Scenario 2 — Fuzzy match hits on versioned name
- A PDF contains the text `"Canada Metal FA595213 APV 184468"`.
- No exact alias matches.
- The system loads the vendor name fallback `"CANADA METAL V1"`.
- Slide window matching calculates `fuzz.token_sort_ratio` between `"canada metal v1"` and the document text. The best window scores above 88.0.
- The system returns a fuzzy match.

### Scenario 3 — Ambiguous fuzzy match rejected
- A PDF contains the text `"Canada Metal"`.
- The database has two vendors: `"CANADA METAL V1"` and `"CANADA METAL V2"`.
- Slide windows return fuzzy scores of 92.0 and 92.0 respectively.
- The score margin (0.0) is less than the `FUZZY_MIN_MARGIN` (5.0).
- The system rejects the match and returns `None` (resulting in a 409 Conflict).

### Scenario 4 — Short pattern excluded from fuzzy matching
- A vendor has an alias `"A1"`.
- The document contains `"invoice number: A1-9901"`.
- The exact match check fails (e.g. word boundaries or formatting mismatch).
- The fuzzy match loop runs. It sees `"A1"` has length 2 (< 5), so it is excluded from fuzzy matching to prevent false matches on layout text.

### Scenario 5 — Tenant isolation enforced
- Client A uploads an invoice containing `"Acme Corp"`.
- Client B has a vendor named `"Acme Corp"`. Client A does not.
- The system loads aliases using Client A's `user_id`. No vendors or aliases match.
- The detection returns `None` and Client A receives a 409 error, even though Client B has a matching vendor.

---

## Error Responses

| Situation | HTTP Code | Error Message |
|---|---|---|
| Vendor not detected | 409 | `"Could not detect vendor from page 1 text."` |
| Ambiguous vendor match | 409 | `"Could not detect vendor from page 1 text."` (logged as warning internally) |

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_vendor_detector.py`](../../tests/test_vendor_detector.py) | `test_exact_alias_match_runs_before_fuzzy_match` | Exact match loop runs and short-circuits before fuzzy logic. |
| | `test_fuzzy_match_detects_versioned_vendor_name` | Fuzzy sliding windows match slightly varied vendor names. |
| | `test_close_fuzzy_matches_are_rejected_as_ambiguous` | Ambiguity margin check rejects matches with score differences < 5.0. |
| | `test_weak_single_word_alias_is_rejected` | Single-word exact weight 1 matches are accepted (verifies threshold constraint). |
| [`test_admin_client_scoping.py`](../../tests/test_admin_client_scoping.py) | `test_detect_vendor_isolation` | Verifies that `detect_vendor` queries never cross-leak vendor records between users. |
| | `test_detect_vendor_passes_user_id_to_db_aliases` | Verifies `detect_vendor` forwards `user_id` to database alias queries. |

---

## Quick Reference

| Operation / Check | Method | Source / Table | Notes |
|---|---|---|---|
| Load explicit aliases | `get_all_aliases_for_detection` | `vendor_aliases` | Scoped by `user_id` |
| Load name fallback | `list_vendors` | `vendors` | Treated as alias with weight 5 |
| Exact match score | Sum of weights | n/a | Short-circuits if >= 1.0 |
| Fuzzy score library | `fuzz.token_sort_ratio` | n/a | Threshold >= 88.0, margin >= 5.0 |
