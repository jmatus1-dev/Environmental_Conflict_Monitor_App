"""
conflict_ids.py - Group articles that describe the same real-world conflict
under a single, stable conflict_id.

Why this exists
---------------
Several news outlets often cover the same event (e.g. one mining protest).
Without grouping, each article looks like a separate conflict, which inflates
the counts and makes it impossible to track one case over time. This script
reads the combined CSV, decides which articles belong together, and writes back
two new columns:

  * conflict_id    - a stable label shared by all articles about one conflict
                     (e.g. "CONFLICT_a3f8e201")
  * coverage_count - how many articles share that conflict_id; a rough
                     "how widely reported / how urgent" signal

How it decides two articles are "the same conflict"
---------------------------------------------------
Two articles are grouped together when ALL of these hold:
  1. same country, AND
  2. their titles are similar enough (fuzzy text match), AND
  3. (only if both have dates) they were reported within TIME_WINDOW_DAYS of
     each other.

This is intentionally simple: it uses only pandas + Python's standard library.
No paid APIs, no LLM, no extra installs. It won't be perfect, but it catches
the obvious duplicates and the two knobs at the top are easy to tune.

Run
---
    python conflict_ids.py

    # or point it at a different file:
    ENVCONFLICT_DATA=data/combined.csv python conflict_ids.py
"""

from __future__ import annotations

import os
import re
import hashlib
import difflib
import pandas as pd

DATA_PATH = os.environ.get("ENVCONFLICT_DATA", os.path.join("data", "combined.csv"))

# ---------------------------------------------------------------------------
# Tuning knobs - adjust these to make matching looser or stricter.
# ---------------------------------------------------------------------------
# Real headlines about the same event rarely share whole phrases, so we don't
# rely on raw text similarity alone. Two titles are judged the "same conflict"
# if ANY of the rules below pass (see title_match). Each knob controls one rule.

# Rule 1: overall character similarity of the two titles (0-1). Catches
# same-language rewordings. Higher = stricter.
SEQ_THRESHOLD = 0.60

# Rule 2: number of shared *distinctive* words (place names, companies, etc.)
# that on their own signal the same event, e.g. {"bambas", "apurimac"}.
STRONG_SHARED_KEYWORDS = 2

# Rule 3: a softer fallback - at least one distinctive shared word AND a decent
# proportion of overlapping keywords overall.
OVERLAP_THRESHOLD = 0.30

# Words shorter than this are ignored when building the keyword sets (drops
# tiny filler words that slip past the stopword list).
KEYWORD_MIN_LEN = 4
# A shared word is "distinctive" (likely a place/proper noun) if it's at least
# this long. Place names and company names are usually 5+ characters.
DISTINCTIVE_MIN_LEN = 5

# Articles more than this many days apart are treated as different conflicts,
# even if their titles match. Set to None to ignore dates entirely. 120 days is
# generous so a long-running conflict that flares up again stays grouped, while
# truly old/new events stay separate.
TIME_WINDOW_DAYS = 120

# Common "filler" words stripped before comparing titles, so that the
# comparison focuses on the meaningful words (places, actors, actions).
# Covers English, Spanish and Portuguese since the sources are Latin American.
STOPWORDS = {
    # English
    "the", "a", "an", "of", "in", "on", "to", "and", "for", "with", "at",
    "by", "from", "is", "are", "was", "were", "as", "that", "this", "it",
    "over", "after", "amid", "says", "say", "new", "news",
    # Spanish
    "el", "la", "los", "las", "de", "del", "en", "y", "con", "por", "para",
    "un", "una", "que", "se", "su", "sus", "sobre", "tras", "dice", "nuevo",
    "nueva",
    # Portuguese
    "o", "os", "as", "do", "da", "dos", "das", "em", "e", "com", "para",
    "uma", "apos", "diz", "novo", "nova",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Return a column as strings, or a column of empty strings if missing.
    Keeps the script from crashing on partially-built datasets."""
    if name in df.columns:
        return df[name].astype(str)
    return pd.Series([""] * len(df), index=df.index)


def normalize_title(title: str) -> str:
    """Lowercase, drop punctuation, and remove filler words so that two
    differently-worded headlines about the same event look more alike."""
    t = str(title).lower()
    # keep letters (incl. accented), digits and spaces; everything else -> space
    t = re.sub(r"[^a-z0-9áàâãéèêíïóôõöúüçñ\s]", " ", t)
    words = [w for w in t.split() if w and w not in STOPWORDS]
    return " ".join(words)


def keywords(norm_title: str) -> set:
    """The set of 'meaningful' words in a normalized title (length-filtered)."""
    return {w for w in norm_title.split() if len(w) >= KEYWORD_MIN_LEN}


def title_match(a: str, b: str) -> tuple:
    """Decide whether two normalized titles describe the same conflict.

    Returns (is_match, score). `is_match` is the yes/no decision used for
    grouping; `score` is only used to pick the *best* match when an article
    could join more than one existing conflict.

    Three ways to qualify as a match (any one is enough):
      1. high overall character similarity (same-language rewording), or
      2. two or more shared distinctive words (e.g. a place + a company), or
      3. one shared distinctive word plus solid overall keyword overlap.
    """
    if not a or not b:
        return (False, 0.0)

    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ka, kb = keywords(a), keywords(b)
    if not ka or not kb:
        return (seq >= SEQ_THRESHOLD, seq)

    shared = ka & kb
    distinctive = {w for w in shared if len(w) >= DISTINCTIVE_MIN_LEN}
    overlap = len(shared) / min(len(ka), len(kb))  # overlap coefficient

    is_match = (
        seq >= SEQ_THRESHOLD
        or len(distinctive) >= STRONG_SHARED_KEYWORDS
        or (len(distinctive) >= 1 and overlap >= OVERLAP_THRESHOLD)
    )
    # combined score for ranking candidate matches (higher = better fit)
    score = seq + overlap + 0.25 * len(distinctive)
    return (is_match, score)


def stable_conflict_id(country: str, rep_title: str) -> str:
    """A short, stable ID derived from the conflict's country + representative
    title. Because it's a hash of the content (not a running counter), the same
    conflict keeps the same ID across re-runs as long as its earliest article
    stays the same - which is what lets us track a case over time."""
    key = f"{str(country).strip().lower()}|{rep_title}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    return f"CONFLICT_{digest}"


# ---------------------------------------------------------------------------
# Core: cluster articles into conflicts
# ---------------------------------------------------------------------------

def assign_conflict_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Add `conflict_id` and `coverage_count` columns to `df` (returns a copy).

    Strategy: greedy clustering. Process articles oldest-first so the earliest
    article "seeds" each conflict. For every following article, compare it to
    the seed of each existing conflict; join the best match above the
    threshold, otherwise start a new conflict."""
    df = df.copy()

    dates = pd.to_datetime(_col(df, "date_published"), errors="coerce")
    norm_titles = _col(df, "article_title").map(normalize_title)
    countries = _col(df, "country").str.strip().str.lower()

    # oldest-first; undated rows go last
    order = dates.sort_values(na_position="last").index.tolist()

    clusters = []  # each: {country, rep_title, rep_date, members: [row_idx, ...]}

    for idx in order:
        c_country = countries.loc[idx]
        c_title = norm_titles.loc[idx]
        c_date = dates.loc[idx]

        best_cluster = None
        best_score = 0.0
        for cl in clusters:
            if cl["country"] != c_country:
                continue
            if (TIME_WINDOW_DAYS is not None
                    and pd.notna(c_date) and pd.notna(cl["rep_date"])
                    and abs((c_date - cl["rep_date"]).days) > TIME_WINDOW_DAYS):
                continue
            is_match, score = title_match(c_title, cl["rep_title"])
            if is_match and score > best_score:
                best_score = score
                best_cluster = cl

        if best_cluster is None:
            clusters.append({
                "country": c_country,
                "rep_title": c_title,
                "rep_date": c_date,
                "members": [idx],
            })
        else:
            best_cluster["members"].append(idx)

    # Turn clusters into the two output columns.
    id_by_row, count_by_row = {}, {}
    for cl in clusters:
        cid = stable_conflict_id(cl["country"], cl["rep_title"])
        n = len(cl["members"])
        for idx in cl["members"]:
            id_by_row[idx] = cid
            count_by_row[idx] = n

    df["conflict_id"] = df.index.map(id_by_row).fillna("")
    df["coverage_count"] = df.index.map(count_by_row).fillna(1).astype(int)
    return df


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.path.exists(DATA_PATH):
        raise SystemExit(
            f"No data file at '{DATA_PATH}'. Run `python run_all.py` first."
        )

    df = pd.read_csv(DATA_PATH, dtype=str, keep_default_na=False)
    n_articles = len(df)

    df = assign_conflict_ids(df)
    n_conflicts = df["conflict_id"].replace("", pd.NA).nunique()

    df.to_csv(DATA_PATH, index=False)

    multi = (df.groupby("conflict_id")["coverage_count"].first() > 1).sum()
    print(f"Read {n_articles} articles from {DATA_PATH}")
    print(f"Grouped into {n_conflicts} unique conflicts "
          f"({multi} of them reported by more than one article).")
    print(f"Wrote conflict_id + coverage_count back to {DATA_PATH}.")


if __name__ == "__main__":
    main()
