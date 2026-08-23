//! vendor_detector.rs ← vendor_detector.py (alias-scored vendor detection).
//!
//! Detects the vendor from page-1 text: exact alias containment first, then a
//! RapidFuzz pass over token windows. Always uses the current document's text
//! and prefers explicit `vendor_aliases` rows, falling back to vendor names
//! when aliases have not been seeded.
//!
//! ## Scoring parity
//!
//! `rapidfuzz` 0.5 for Rust ships [`rapidfuzz::fuzz::ratio`] but not
//! `token_sort_ratio`, so [`token_sort_ratio`] is reconstructed here from its
//! definition — sort each side's whitespace tokens, then take the plain
//! ratio. Both languages' `ratio` is the same Indel-based similarity from the
//! shared upstream project, and inputs reach this module already normalised to
//! `[a-z0-9 ]`, so scores match the values the thresholds below were tuned
//! against.
//!
//! ## Why this is not a transliteration
//!
//! Python called `_text_windows` inside `_best_window_score`, i.e. once per
//! alias pattern, rebuilding and re-deduplicating the same window list for
//! every candidate vendor. Windows depend only on the *token count* of the
//! pattern, never on its content, so they are built once per distinct token
//! count here and shared. With a few hundred aliases that turns hundreds of
//! window builds into a handful. Scores are unchanged — only the redundant
//! work is gone.

use std::collections::hash_map::Entry;
use std::collections::{HashMap, HashSet};

use augocr_common::error::AppResult;
use rapidfuzz::fuzz;
use serde_json::Value;
use sqlx::PgPool;

use crate::page::Word;

/// Minimum summed weight for an exact match to count.
pub const MIN_SCORE: f64 = 1.0;
/// Minimum adjusted fuzzy score for a fuzzy match to count.
pub const FUZZY_MIN_SCORE: f64 = 88.0;
/// A fuzzy winner must beat the runner-up by at least this much.
pub const FUZZY_MIN_MARGIN: f64 = 5.0;
/// Patterns shorter than this (ignoring spaces) are never fuzzy-matched.
pub const FUZZY_MIN_PATTERN_CHARS: usize = 5;
/// Token windows are searched at the pattern length ± this many tokens.
pub const FUZZY_MAX_WINDOW_EXTRA: usize = 2;
/// Weight given to a vendor's own name as an implicit alias — meaningful, but
/// below an explicitly configured alias.
const VENDOR_NAME_WEIGHT: f64 = 5.0;

/// A detected vendor and the evidence for it.
#[derive(Debug, Clone, PartialEq)]
pub struct VendorMatch {
    pub vendor_id: String,
    pub vendor_name: String,
    pub score: f64,
    pub matched_patterns: Vec<String>,
    pub match_type: MatchType,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MatchType {
    Exact,
    Fuzzy,
}

impl MatchType {
    pub fn as_str(self) -> &'static str {
        match self {
            MatchType::Exact => "exact",
            MatchType::Fuzzy => "fuzzy",
        }
    }
}

/// One detection candidate: a pattern that, if found, votes for a vendor.
#[derive(Debug, Clone)]
pub struct Alias {
    pub vendor_id: String,
    pub vendor_name: String,
    pub pattern: String,
    pub weight: f64,
    pub source: String,
}

/// Collapse text to `[a-z0-9]` runs separated by single spaces.
///
/// Mirrors Python `_normalize_text`: lowercase, replace every run of
/// non-alphanumerics with one space, then trim.
pub fn normalize_text(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    let mut pending_space = false;
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() {
            if pending_space && !out.is_empty() {
                out.push(' ');
            }
            pending_space = false;
            out.push(ch.to_ascii_lowercase());
        } else if ch.is_alphanumeric() {
            // Python's `[^a-z0-9]` is applied *after* `.lower()`, so any
            // non-ASCII alphanumeric is a separator, not a character.
            pending_space = true;
        } else {
            pending_space = true;
        }
    }
    out
}

/// `" ".join(sorted(s.split()))` — the preprocessing half of token_sort_ratio.
///
/// Rust's `str` ordering is bytewise, which for UTF-8 is code-point order,
/// so this sorts identically to Python's `sorted()`.
fn sorted_tokens(value: &str) -> String {
    let mut tokens: Vec<&str> = value.split_whitespace().collect();
    tokens.sort_unstable();
    tokens.join(" ")
}

/// Rust's [`fuzz::ratio`] returns a 0.0–1.0 similarity; Python's returns
/// 0–100. Every threshold in this module is on the Python scale.
const PERCENT: f64 = 100.0;

/// `rapidfuzz.fuzz.token_sort_ratio` — ratio over whitespace-sorted tokens,
/// on Python's 0–100 scale.
pub fn token_sort_ratio(a: &str, b: &str) -> f64 {
    fuzz::ratio(sorted_tokens(a).chars(), sorted_tokens(b).chars()) * PERCENT
}

/// Avoid fuzzy-matching tiny ids or numeric-only patterns, which produce
/// confident nonsense against arbitrary invoice text.
fn pattern_is_safe_for_fuzzy(pattern: &str) -> bool {
    let compact_len = pattern.chars().filter(|c| *c != ' ').count();
    compact_len >= FUZZY_MIN_PATTERN_CHARS && pattern.chars().any(|c| c.is_alphabetic())
}

/// Token windows near a candidate length, pre-sorted for scoring.
///
/// Built once per distinct pattern token count and reused across every alias
/// of that length — see the module note on why Python rebuilt these per alias.
struct WindowSet {
    /// `(window, window_with_sorted_tokens)`.
    windows: Vec<(String, String)>,
}

/// Lazily builds and caches [`WindowSet`]s keyed by pattern token count.
struct WindowCache<'a> {
    tokens: Vec<&'a str>,
    by_word_count: HashMap<usize, WindowSet>,
}

impl<'a> WindowCache<'a> {
    fn new(text_blob: &'a str) -> Self {
        Self {
            tokens: text_blob.split_whitespace().collect(),
            by_word_count: HashMap::new(),
        }
    }

    /// Windows sized `[n - 2, n + 2]`, in Python's order (ascending size, then
    /// ascending start), first occurrence of each distinct window kept.
    fn get(&mut self, pattern_word_count: usize) -> &WindowSet {
        let tokens = &self.tokens;
        self.by_word_count
            .entry(pattern_word_count)
            .or_insert_with(|| {
                let mut windows = Vec::new();
                if tokens.is_empty() {
                    return WindowSet { windows };
                }
                let min_size = pattern_word_count.saturating_sub(FUZZY_MAX_WINDOW_EXTRA).max(1);
                let max_size = (pattern_word_count + FUZZY_MAX_WINDOW_EXTRA).min(tokens.len());
                let mut seen: HashSet<String> = HashSet::new();
                for size in min_size..=max_size {
                    for start in 0..=(tokens.len() - size) {
                        let window = tokens[start..start + size].join(" ");
                        if seen.insert(window.clone()) {
                            let sorted = sorted_tokens(&window);
                            windows.push((window, sorted));
                        }
                    }
                }
                WindowSet { windows }
            })
    }
}

/// Best window score for one pattern: `(raw_score, matched_window)`.
///
/// Returns `None` when the pattern is unusable or no window scores above zero,
/// matching Python's `(0.0, None)`.
fn best_window_score(
    pattern: &str,
    pattern_word_count: usize,
    windows: &WindowSet,
) -> Option<(f64, String)> {
    if pattern_word_count == 0 || !pattern_is_safe_for_fuzzy(pattern) {
        return None;
    }

    // Comparing one pattern against many windows: build the pattern's block
    // pattern once instead of on every comparison.
    let scorer = fuzz::RatioBatchComparator::new(sorted_tokens(pattern).chars());

    // Tracked on rapidfuzz's native 0.0–1.0 scale (the cutoff must be too),
    // and converted to Python's 0–100 on the way out.
    let mut best_unit = 0.0_f64;
    let mut best_window: Option<&str> = None;
    for (window, sorted_window) in &windows.windows {
        // Feeding the running best as a cutoff lets rapidfuzz abandon
        // hopeless comparisons early. Python kept only strictly-greater
        // scores, so discarding anything at or below the running best is
        // score-identical.
        let args = fuzz::Args::default().score_cutoff(best_unit);
        if let Some(score) = scorer.similarity_with_args(sorted_window.chars(), &args) {
            if score > best_unit {
                best_unit = score;
                best_window = Some(window);
                if best_unit >= 1.0 {
                    break;
                }
            }
        }
    }
    best_window.map(|w| (best_unit * PERCENT, w.to_string()))
}

/// Adjust fuzzy confidence by how distinctive the pattern is: multi-word
/// patterns earn a small bonus, a single word that did not match exactly takes
/// a penalty.
fn effective_fuzzy_score(pattern_word_count: usize, exact_window: bool, raw_score: f64) -> f64 {
    let mut score = raw_score;
    if pattern_word_count > 1 {
        score += (((pattern_word_count - 1) * 2) as f64).min(4.0);
    } else if !exact_window {
        score -= 3.0;
    }
    score.clamp(0.0, 100.0)
}

// ── Alias loading ────────────────────────────────────────────────────────────

fn alias_from_row(row: &Value) -> Option<Alias> {
    Some(Alias {
        vendor_id: row.get("vendor_id")?.as_str()?.to_string(),
        vendor_name: row
            .get("vendor_name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        pattern: row.get("pattern")?.as_str()?.to_string(),
        weight: row
            .get("weight")
            .and_then(|v| v.as_f64().or_else(|| v.as_str()?.parse().ok()))
            .unwrap_or(1.0),
        source: row
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
    })
}

/// Explicit aliases plus every vendor's own name as an implicit alias.
///
/// Vendor names are always valid candidates at [`VENDOR_NAME_WEIGHT`].
/// Explicit `vendor_aliases` rows are additive and are never generated from
/// document text.
async fn load_detection_aliases(pool: &PgPool, user_id: Option<&str>) -> AppResult<Vec<Alias>> {
    let alias_rows = augocr_common::db::get_all_aliases_for_detection(pool, user_id).await?;
    let vendor_rows = augocr_common::db::list_vendors(pool, user_id).await?;

    let mut aliases: Vec<Alias> = alias_rows.iter().filter_map(alias_from_row).collect();
    let alias_count = aliases.len();

    aliases.extend(vendor_rows.iter().filter_map(|v| {
        let id = v.get("id")?.as_str()?;
        let name = v.get("name")?.as_str()?;
        Some(Alias {
            vendor_id: id.to_string(),
            vendor_name: name.to_string(),
            pattern: name.to_string(),
            weight: VENDOR_NAME_WEIGHT,
            source: "vendor_name".to_string(),
        })
    }));

    let scope = user_id.unwrap_or("all");
    if aliases.is_empty() {
        tracing::warn!("No vendors or aliases configured in DB for user_id={scope}");
    } else {
        tracing::info!(
            "Detection aliases loaded: {alias_count} explicit alias(es) + {} vendor name(s) for user_id={scope}",
            aliases.len() - alias_count
        );
    }
    Ok(aliases)
}

// ── Matching ─────────────────────────────────────────────────────────────────

struct Candidate {
    vendor_name: String,
    score: f64,
    matched_patterns: Vec<String>,
}

/// Sum alias weights for every vendor whose normalised pattern appears
/// verbatim in the page text.
fn detect_exact(aliases: &[Alias], text_blob: &str) -> Option<VendorMatch> {
    // Insertion order matters: ties resolve to the first vendor seen, which is
    // what Python's `max()` over a dict does.
    let mut order: Vec<String> = Vec::new();
    let mut scores: HashMap<String, Candidate> = HashMap::new();

    for alias in aliases {
        let normalized = normalize_text(&alias.pattern);
        if normalized.is_empty() || !text_blob.contains(&normalized) {
            continue;
        }
        match scores.entry(alias.vendor_id.clone()) {
            Entry::Occupied(mut e) => {
                let c = e.get_mut();
                c.score += alias.weight;
                c.matched_patterns.push(normalized);
            }
            Entry::Vacant(e) => {
                order.push(alias.vendor_id.clone());
                e.insert(Candidate {
                    vendor_name: alias.vendor_name.clone(),
                    score: alias.weight,
                    matched_patterns: vec![normalized],
                });
            }
        }
    }

    let best_vid = order
        .into_iter()
        .max_by(|a, b| {
            let sa = scores.get(a).map_or(0.0, |c| c.score);
            let sb = scores.get(b).map_or(0.0, |c| c.score);
            // `partial_cmp` cannot fail here: weights come from the DB as
            // finite numbers. `Less` on a tie keeps the earlier entry.
            sa.partial_cmp(&sb).unwrap_or(std::cmp::Ordering::Less)
        })?;
    let best = scores.remove(&best_vid)?;

    if best.score < MIN_SCORE {
        tracing::info!(
            "Best exact vendor match '{best_vid}' scored {:.1} (below threshold {MIN_SCORE})",
            best.score
        );
        return None;
    }

    tracing::info!(
        "Vendor detected by exact match: {best_vid} (score={:.1}, patterns={:?})",
        best.score,
        best.matched_patterns
    );
    Some(VendorMatch {
        vendor_id: best_vid,
        vendor_name: best.vendor_name,
        score: best.score,
        matched_patterns: best.matched_patterns,
        match_type: MatchType::Exact,
    })
}

/// Score every alias against token windows and take the clear winner.
fn detect_fuzzy(aliases: &[Alias], text_blob: &str) -> Option<VendorMatch> {
    let mut cache = WindowCache::new(text_blob);
    let mut best_by_vendor: HashMap<String, Candidate> = HashMap::new();
    let mut order: Vec<String> = Vec::new();

    for alias in aliases {
        let pattern = normalize_text(&alias.pattern);
        if pattern.is_empty() {
            continue;
        }
        let word_count = pattern.split_whitespace().count();
        // Skip the window build entirely for patterns that can never match.
        if word_count == 0 || !pattern_is_safe_for_fuzzy(&pattern) {
            continue;
        }
        let Some((raw_score, window)) =
            best_window_score(&pattern, word_count, cache.get(word_count))
        else {
            continue;
        };
        let score = effective_fuzzy_score(word_count, pattern == window, raw_score);

        let entry = best_by_vendor.entry(alias.vendor_id.clone());
        match entry {
            Entry::Occupied(mut e) if score > e.get().score => {
                e.insert(Candidate {
                    vendor_name: alias.vendor_name.clone(),
                    score,
                    matched_patterns: vec![format!("fuzzy:{pattern}~{window}:{raw_score:.1}")],
                });
            }
            Entry::Occupied(_) => {}
            Entry::Vacant(e) => {
                order.push(alias.vendor_id.clone());
                e.insert(Candidate {
                    vendor_name: alias.vendor_name.clone(),
                    score,
                    matched_patterns: vec![format!("fuzzy:{pattern}~{window}:{raw_score:.1}")],
                });
            }
        }
    }

    if best_by_vendor.is_empty() {
        return None;
    }

    // Rank descending by score; Python's `sorted(..., reverse=True)` is stable,
    // so equal scores keep insertion order.
    let mut ranked: Vec<String> = order;
    ranked.sort_by(|a, b| {
        let sa = best_by_vendor.get(a).map_or(0.0, |c| c.score);
        let sb = best_by_vendor.get(b).map_or(0.0, |c| c.score);
        sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
    });

    let best_vid = ranked.first()?.clone();
    let best_score = best_by_vendor.get(&best_vid).map_or(0.0, |c| c.score);

    if best_score < FUZZY_MIN_SCORE {
        tracing::info!(
            "Best fuzzy vendor match '{best_vid}' scored {best_score:.1} (below threshold {FUZZY_MIN_SCORE:.1})"
        );
        return None;
    }

    if let Some(second_vid) = ranked.get(1) {
        let second_score = best_by_vendor.get(second_vid).map_or(0.0, |c| c.score);
        let margin = best_score - second_score;
        if margin < FUZZY_MIN_MARGIN {
            tracing::warn!(
                "Ambiguous fuzzy vendor match: {best_vid}={best_score:.1}, \
                 {second_vid}={second_score:.1} (margin {margin:.1} < {FUZZY_MIN_MARGIN:.1})"
            );
            return None;
        }
    }

    let best = best_by_vendor.remove(&best_vid)?;
    tracing::info!(
        "Vendor detected by fuzzy match: {best_vid} (score={:.1}, patterns={:?})",
        best.score,
        best.matched_patterns
    );
    Some(VendorMatch {
        vendor_id: best_vid,
        vendor_name: best.vendor_name,
        score: best.score,
        matched_patterns: best.matched_patterns,
        match_type: MatchType::Fuzzy,
    })
}

/// Detect the vendor from page-1 words: exact alias containment, then fuzzy.
///
/// Pass `user_id` to restrict matching to that tenant's vendors; `None`
/// (admin) matches across all vendors.
pub async fn detect_vendor(
    pool: &PgPool,
    page_words: &[Word],
    user_id: Option<&str>,
) -> AppResult<Option<VendorMatch>> {
    if page_words.is_empty() {
        tracing::warn!("[VendorDetect] No words provided for vendor detection");
        return Ok(None);
    }

    let raw_word_count = page_words.iter().filter(|w| !w.text.trim().is_empty()).count();
    let joined = page_words
        .iter()
        .filter(|w| !w.text.trim().is_empty())
        .map(|w| w.text.as_str())
        .collect::<Vec<_>>()
        .join(" ");
    let text_blob = normalize_text(&joined);
    if text_blob.is_empty() {
        tracing::warn!(
            "[VendorDetect] Empty text blob after normalisation (page_words={})",
            page_words.len()
        );
        return Ok(None);
    }

    tracing::info!(
        "[VendorDetect] Page-1 text ({raw_word_count} words, {} chars): {}{}",
        text_blob.len(),
        text_blob.chars().take(300).collect::<String>(),
        if text_blob.chars().count() > 300 { " ..." } else { "" }
    );

    let aliases = load_detection_aliases(pool, user_id).await?;
    if aliases.is_empty() {
        tracing::warn!(
            "[VendorDetect] No vendors found for user_id={}",
            user_id.unwrap_or("all")
        );
        return Ok(None);
    }

    if let Some(m) = detect_exact(&aliases, &text_blob) {
        tracing::info!(
            "[VendorDetect] MATCHED (exact)  vendor_id={}  name={:?}  score={:.1}  patterns={:?}",
            m.vendor_id,
            m.vendor_name,
            m.score,
            m.matched_patterns
        );
        return Ok(Some(m));
    }

    tracing::info!(
        "[VendorDetect] No exact match found in page-1 text ({} chars) — trying fuzzy",
        text_blob.len()
    );
    let fuzzy = detect_fuzzy(&aliases, &text_blob);
    match &fuzzy {
        Some(m) => tracing::info!(
            "[VendorDetect] MATCHED (fuzzy)  vendor_id={}  name={:?}  score={:.1}  patterns={:?}",
            m.vendor_id,
            m.vendor_name,
            m.score,
            m.matched_patterns
        ),
        None => tracing::warn!(
            "[VendorDetect] NO MATCH — neither exact nor fuzzy found a vendor for user_id={}. \
             Text snippet: {:?}",
            user_id.unwrap_or("all"),
            text_blob.chars().take(200).collect::<String>()
        ),
    }
    Ok(fuzzy)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn alias(vendor_id: &str, name: &str, pattern: &str, weight: f64) -> Alias {
        Alias {
            vendor_id: vendor_id.to_string(),
            vendor_name: name.to_string(),
            pattern: pattern.to_string(),
            weight,
            source: "alias".to_string(),
        }
    }

    #[test]
    fn normalize_collapses_punctuation_and_lowercases() {
        assert_eq!(normalize_text("FRESH PRODUCTS, INC."), "fresh products inc");
        assert_eq!(normalize_text("  A&B   Co.  "), "a b co");
        assert_eq!(normalize_text("---"), "");
        assert_eq!(normalize_text(""), "");
        assert_eq!(normalize_text("PO#12345"), "po 12345");
        // Non-ASCII alphanumerics are separators, as in Python's `[^a-z0-9]`
        // applied after lowercasing.
        assert_eq!(normalize_text("Café Ltd"), "caf ltd");
    }

    #[test]
    fn token_sort_ratio_is_order_insensitive_and_matches_ratio() {
        assert_eq!(token_sort_ratio("acme corp", "corp acme"), 100.0);
        assert_eq!(token_sort_ratio("acme corp", "acme corp"), 100.0);
        // Unrelated strings score low; identical single tokens score 100.
        assert!(token_sort_ratio("acme", "zzzzzz") < 50.0);
        assert_eq!(token_sort_ratio("acme", "acme"), 100.0);
    }

    #[test]
    fn fuzzy_safety_gate_rejects_short_and_numeric_patterns() {
        assert!(!pattern_is_safe_for_fuzzy("ab"));
        assert!(!pattern_is_safe_for_fuzzy("12345678"), "numeric-only");
        // "acme" is 4 compact chars — one short of the 5-char gate, so even a
        // real vendor name this short is exact-match only.
        assert!(!pattern_is_safe_for_fuzzy("acme"));
        assert!(!pattern_is_safe_for_fuzzy("a b c"), "3 compact chars");
        assert!(pattern_is_safe_for_fuzzy("acmex"));
        assert!(pattern_is_safe_for_fuzzy("ac me x"), "5 compact chars, has letters");
    }

    #[test]
    fn window_cache_reproduces_python_window_list() {
        let blob = "alpha beta gamma delta";
        let mut cache = WindowCache::new(blob);
        // pattern_word_count = 1 → sizes 1..=3 (min_size floors at 1).
        let ws = cache.get(1);
        let names: Vec<&str> = ws.windows.iter().map(|(w, _)| w.as_str()).collect();
        assert_eq!(
            names,
            vec![
                "alpha", "beta", "gamma", "delta",
                "alpha beta", "beta gamma", "gamma delta",
                "alpha beta gamma", "beta gamma delta",
            ]
        );
    }

    #[test]
    fn window_cache_is_reused_across_patterns_of_equal_length() {
        let mut cache = WindowCache::new("alpha beta gamma");
        let first = cache.get(2).windows.len();
        let second = cache.get(2).windows.len();
        assert_eq!(first, second);
        assert_eq!(cache.by_word_count.len(), 1, "one build for one token count");
    }

    #[test]
    fn window_cache_handles_empty_and_short_text() {
        let mut empty = WindowCache::new("");
        assert!(empty.get(3).windows.is_empty());

        // A pattern far longer than the text yields no windows at all:
        // min_size (9-2=7) exceeds max_size (clamped to the 1 available
        // token), so the size range is empty. Python's `range(7, 2)` is
        // likewise empty — a long alias simply cannot fuzzy-match short text.
        let mut short = WindowCache::new("acme");
        assert!(short.get(9).windows.is_empty());

        // Within reach, the whole text is a candidate window.
        assert_eq!(short.get(1).windows.len(), 1);
        assert_eq!(short.get(1).windows[0].0, "acme");
    }

    #[test]
    fn exact_match_sums_weights_across_aliases() {
        let aliases = vec![
            alias("v1", "Acme", "Acme Corp", 10.0),
            alias("v1", "Acme", "acme", 3.0),
            alias("v2", "Other", "Other Inc", 10.0),
        ];
        let blob = normalize_text("Invoice from ACME CORP, 123 Road");
        let m = detect_exact(&aliases, &blob).expect("match");
        assert_eq!(m.vendor_id, "v1");
        assert_eq!(m.score, 13.0);
        assert_eq!(m.match_type, MatchType::Exact);
        assert_eq!(m.matched_patterns, vec!["acme corp", "acme"]);
    }

    #[test]
    fn exact_match_returns_none_when_no_pattern_is_present() {
        let aliases = vec![alias("v1", "Acme", "Acme Corp", 10.0)];
        let blob = normalize_text("Completely unrelated document text");
        assert!(detect_exact(&aliases, &blob).is_none());
    }

    #[test]
    fn exact_match_ignores_blank_patterns() {
        // A pattern that normalises to "" would otherwise match everything.
        let aliases = vec![alias("v1", "Acme", "---", 10.0)];
        let blob = normalize_text("anything at all");
        assert!(detect_exact(&aliases, &blob).is_none());
    }

    #[test]
    fn fuzzy_match_tolerates_ocr_noise() {
        // "acrne" is the classic OCR misread of "acme" (m → rn).
        let aliases = vec![alias("v1", "Acme Trading", "Acme Trading Company", 5.0)];
        let blob = normalize_text("INVOICE Acrne Trading Company 500 Main St");
        let m = detect_fuzzy(&aliases, &blob).expect("fuzzy match");
        assert_eq!(m.vendor_id, "v1");
        assert_eq!(m.match_type, MatchType::Fuzzy);
        assert!(m.score >= FUZZY_MIN_SCORE, "score was {}", m.score);
        assert!(m.matched_patterns[0].starts_with("fuzzy:acme trading company~"));
    }

    #[test]
    fn fuzzy_match_threshold_bites_on_heavy_noise() {
        // Two misreads in one name drops the raw score to ~81.8; even the
        // +4 multi-word bonus leaves it under the 88 threshold, so the
        // detector declines rather than guessing.
        let aliases = vec![alias("v1", "Acme Trading", "Acme Trading Company", 5.0)];
        let blob = normalize_text("INVOICE Acrne Trading Cornpany 500 Main St");
        assert!(detect_fuzzy(&aliases, &blob).is_none());
    }

    #[test]
    fn fuzzy_match_rejects_ambiguous_pairs() {
        // Neither pattern is present verbatim and both sit one character from
        // the same window, so their scores tie and no winner is safe.
        let aliases = vec![
            alias("v1", "Acme Trading Co", "Acme Trading Co", 5.0),
            alias("v2", "Acme Trading Cp", "Acme Trading Cp", 5.0),
        ];
        let blob = normalize_text("Acme Trading Cx invoice");
        assert!(
            detect_fuzzy(&aliases, &blob).is_none(),
            "margin below {FUZZY_MIN_MARGIN} must be treated as ambiguous"
        );
    }

    #[test]
    fn fuzzy_match_accepts_a_clear_winner_over_a_rival() {
        // The same shape as the ambiguous case, but one alias matches the
        // text verbatim — a 9-point margin is decisive.
        let aliases = vec![
            alias("v1", "Acme Trading Co", "Acme Trading Co", 5.0),
            alias("v2", "Acme Trading Ltd", "Acme Trading Ld", 5.0),
        ];
        let blob = normalize_text("Acme Trading Co invoice");
        let m = detect_fuzzy(&aliases, &blob).expect("clear winner");
        assert_eq!(m.vendor_id, "v1");
        assert_eq!(m.score, 100.0);
    }

    #[test]
    fn fuzzy_match_rejects_scores_below_threshold() {
        let aliases = vec![alias("v1", "Acme Trading", "Acme Trading Company", 5.0)];
        let blob = normalize_text("Totally different supplier name here");
        assert!(detect_fuzzy(&aliases, &blob).is_none());
    }

    #[test]
    fn effective_score_bonuses_and_penalties() {
        // Multi-word patterns earn +2 per extra word, capped at +4.
        assert_eq!(effective_fuzzy_score(2, false, 90.0), 92.0);
        assert_eq!(effective_fuzzy_score(3, false, 90.0), 94.0);
        assert_eq!(effective_fuzzy_score(9, false, 90.0), 94.0, "bonus caps at 4");
        // A single word that did not match its window exactly loses 3.
        assert_eq!(effective_fuzzy_score(1, false, 90.0), 87.0);
        assert_eq!(effective_fuzzy_score(1, true, 90.0), 90.0);
        // Results stay inside [0, 100].
        assert_eq!(effective_fuzzy_score(5, false, 99.0), 100.0);
        assert_eq!(effective_fuzzy_score(1, false, 1.0), 0.0);
    }

    #[test]
    fn best_window_score_skips_unsafe_patterns() {
        let mut cache = WindowCache::new("some invoice text here");
        assert!(best_window_score("ab", 1, cache.get(1)).is_none());
        assert!(best_window_score("", 0, cache.get(1)).is_none());
    }

    #[test]
    fn early_exit_cutoff_does_not_change_the_winner() {
        // A perfect window appears after weaker ones; the running cutoff must
        // not hide it.
        let mut cache = WindowCache::new("zzz qqq acme trading company");
        let (score, window) =
            best_window_score("acme trading company", 3, cache.get(3)).expect("scored");
        assert_eq!(score, 100.0);
        assert_eq!(window, "acme trading company");
    }
}

