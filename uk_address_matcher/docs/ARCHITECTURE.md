# uk_address_matcher — Architecture & Technical Implementation

Reference for how the library actually works inside. For "run it and see," use the
notebooks (`../notebooks/`). Citations are `path:line` into the `uk_address_matcher`
package. Claims here were checked against the source at v1.1.2; a few external facts
(data-asset provenance, licence terms) are attributed, not asserted from code.

---

## 1. What it is

A **record-linkage geocoder**. Given a free-text "messy" address, it resolves it to a
stable `unique_id` in a "canonical" gazetteer (Ordnance Survey AddressBase/NGD, or any
dataset you supply). It scores candidate pairs probabilistically rather than testing for
string equality, because real addresses disagree on tokens: abbreviations (`RD`/`ROAD`),
dropped localities, flat notation, typos.

Two engines do the work:

- **DuckDB** runs every transform. Cleaning, blocking, and feature derivation are all SQL
  over `DuckDBPyRelation`s on a single connection (`address_matcher.py:149`). Vectorised,
  in-process, no external services.
- **Splink** (Fellegi-Sunter probabilistic linkage) scores candidate pairs using a
  pre-trained model shipped as `data/splink_model.json`.

The default pipeline is **deterministic-first, probabilistic-last**
(`address_matcher.py` default stages): skim the cheap exact matches, then spend Splink
only on the fuzzy remainder.

**Measured performance** (this repo, in-memory, cold): matching the bundled
`fictional_london` set, 2,000 messy against 10,000 canonical, end to end (canonical
clean + term-frequency derive + index build + exact + Splink) takes **~9s** on a laptop.
The project's headline claim is ~100k addresses in ~30s with a one-time ~10min UK prep
(README, vendor claim, not re-benchmarked here).

---

## 2. The public API

```python
from uk_address_matcher import AddressMatcher, ExactMatchStage, SplinkStage

matcher = AddressMatcher(
    canonical_addresses=canonical,        # DuckDBPyRelation, or a path to a prepared folder
    addresses_to_match=messy,             # relation, or list of AddressRecord / dicts
    con=con,
    canonical_address_filter=None,        # optional SQL filter applied to canonical (see §8)
    stages=[ExactMatchStage(), SplinkStage()],   # default; pluggable
    cleaning_num_chunks=10,
)
result = matcher.match()
result.matches()            # one row per messy id -> resolved_canonical_id + metadata
result.match_metrics()      # counts/percentages by match_reason
result._splink_predictions(limit=n)   # every candidate pair Splink scored
```

Constructor: `address_matcher.py:134`. The input contract is small and is validated at
`address_matcher.py:47-53`:

| Column | Type | Required | Purpose |
|---|---|---|---|
| `unique_id` | VARCHAR / BIGINT | yes | stable id; becomes `resolved_canonical_id` |
| `address_concat` | VARCHAR | yes | address text, ideally excluding postcode |
| `postcode` | VARCHAR | optional | added as NULL if absent; parsed from text otherwise |

Output columns of `.matches()`: `unique_id`, `resolved_canonical_id`,
`original_address_concat`, `original_address_concat_canonical`, `match_reason`,
`match_weight`, `distinguishability`.

> `match_weight` is a **log2 Bayes factor**: `match_probability = 2^mw / (1 + 2^mw)`
> (`post_linkage/match_result/result.py:123-128`). Higher = more confident.

---

## 3. End-to-end data flow

`[C]` = canonical only, `[M]` = messy only, `[Both]` = both sides.

```
              RAW CANONICAL [C]                              RAW MESSY [M]
     (unique_id, address_concat, [postcode])      (unique_id, address_concat, [postcode])
                    |                                            |
                    v                                            |
   derive_term_frequencies_table [C]                            |   (messy reuses the
   clean -> tokenise -> count tokens ->                         |    canonical TF table;
   rel_freq per token   (chunking_strategies.py:208)            |    it does NOT derive
                    |  tf_table                                  |    its own)
                    v                                            |
  ============= prepare_data_for_matching (chunking_strategies.py:468) =============
  |  hash-partitioned into chunks (>=10k rows/chunk floor, chunking_strategies.py:108)|
  |                                                                                   |
  |  PHASE A  clean  [Both]   QUEUE_CLEAN_FULL_ADDRESS (pipelines.py:74)              |
  |    preserve original -> extract+canonicalise UK postcode -> trim/upper ->         |
  |    first-pass regex clean -> single-token abbrev MAP expand ->                    |
  |    strip country suffix -> de-dup end tokens                                      |
  |  PHASE A' features [Both] QUEUE_DERIVE_NON_TF_FEATURES (pipelines.py:89)          |
  |    parse flat pos/letter/number -> parse business unit -> parse numbers ->        |
  |    tokenise address-without-numbers                                               |
  |                                                                                   |
  |  PHASE B  register TF tables (token TF = derived or pre-baked;                    |
  |           numeric TF = ALWAYS pre-baked)  (pipelines.py:297-342)                  |
  |                                                                                   |
  |  PHASE C  apply TF + blocking  QUEUE_POST_TF (pipelines.py:112)                   |
  |    attach token rel_freq + numeric tf -> move common END tokens aside ->          |
  |    first_unusual_token -> unusual-token bands -> histograms ->                    |
  |    exploding_unique_ids:                                                          |
  |       canonical -> [unique_id] (self)            (inverted_index.py:285)          |
  |       messy     -> lookup keys in inverted index (inverted_index.py:189)          |
  ===================================================================================
                    |                                            |
                    v                                            |
   derive_inverted_index [C]                                     |
   trigram + bigram keys -> unique_ids,                          |
   keep keys mapping to 1..max ids (default 20)                  |
   (chunking_strategies.py:334; inverted_index.py:29-61,176)     |
                    |  inverted_index --------------------------->+  (messy looks up here)
                    |                                             |
                    v  df_canonical_clean        df_messy_clean   v
  ===================== _run_matching (linking_model/matching/runner.py) =============
  |  results_table: 1 row per messy ukam_address_id, resolved_canonical_id NULL       |
  |  until matched. Stages run in order; each is fed ONLY still-unmatched rows;        |
  |  the runner short-circuits when 0 remain.                                          |
  |    ExactMatchStage   [Both]  exact / no-whitespace / flat-retraction               |
  |    (opt) PeeledAddressStage  strip UK locality suffix then exact                   |
  |    (opt) UniqueTrigramStage  trigram unique within postcode                        |
  |    SplinkStage               block -> predict -> distinguishing-token reweight ->  |
  |                              distinguishability -> threshold                       |
  ===================================================================================
                    |
                    v
   MatchResult: unique_id, resolved_canonical_id, match_reason, match_weight, distinguishability
```

Two ordering facts the diagram makes explicit, because they are easy to get wrong:

1. On the **canonical** side, term frequencies are derived **before** the main clean +
   index build (`address_matcher.py:222-267`). Messy reuses that TF table and the index;
   it derives neither (`address_matcher.py:269-282`).
2. **Numeric** term frequencies (how often "1" vs "7" appears as a house/flat number) are
   **always loaded pre-baked**, never derived from your data
   (`cleaning/pipelines.py:303-340`). Only the *token* TF table is data-derived-or-prebaked.

---

## 4. Component deep-dives

### 4.1 The SQL pipeline framework
Every cleaning step and deterministic matcher is "just a list of CTEs." A
`@pipeline_stage` decorator plus `CTEStep(name, sql)` compose a stage into a chain; the
runner stitches them with `{input}` placeholders (`sql_pipeline/`). This is the
load-bearing abstraction. The cleaning queues are plain Python lists you can read top to
bottom in `cleaning/pipelines.py:74-118` (`QUEUE_CLEAN_FULL_ADDRESS`,
`QUEUE_DERIVE_NON_TF_FEATURES`, `QUEUE_POST_TF`).

### 4.2 Cleaning & chunking
Data is hash-partitioned (`abs(hash(address_concat)) % chunks`) with a **minimum
10k-rows-per-chunk floor** (`chunking_strategies.py:108`), so `cleaning_num_chunks` is an
upper bound, not exact. Each chunk is cleaned, processed rows are deleted from the work
table to bound memory, and the table is asserted empty at the end. `ukam_address_id` is
an **internal** row id (`ROW_NUMBER()`-based, `chunking_strategies.py:49`), distinct from
your `unique_id`; Splink keys on it (`splink_model.py:147`).

### 4.3 Postcode handling (a hard UK assumption)
`_ensure_postcode_column` adds a NULL postcode if you didn't supply one
(`pipelines.py:47-71`). `_extract_postcode_from_address` uses a **UK postcode regex**,
prefers an explicit postcode over a text-extracted one, and removes the postcode from the
address text (`cleaning/steps/normalisation.py:42-79`). It is then canonicalised to a
single space between outward and inward code (`:134-150`). A non-UK postcode will not be
extracted; see EXTENDING for what degrades.

### 4.4 Abbreviation / normalisation (single-token)
A first-pass regex chain strips commas/periods/apostrophes and splits letter-number runs.
Then abbreviation expansion builds a DuckDB `MAP` from `data/address_abbreviations.json`
(146 entries) plus programmatically-generated compact-floor rows (e.g. `GFR -> GROUND
FLOOR`), and applies it **token by token**: split on spaces, replace each token if the map
has it, re-join (`cleaning/steps/normalisation.py:303-385`). Keys are single tokens;
**replacement values may be multi-word** (so `RC -> ROMAN CATHOLIC` turns `RC CHURCH` into
`ROMAN CATHOLIC CHURCH`). A multi-word *key* like `CAR PARK SPACE` can never match this
map. See `../notebooks/02_abbreviation_expansions_rc_hmp.ipynb`.

### 4.5 Token parsing (the dwelling/unit model)
Extracts `flat_positional` / `flat_letter` / `flat_number` (composite
`flat_identity`), a `business_unit` type/id for UNIT/SUITE/OFFICE/WORKSHOP/etc., and a
`numeric_tokens` array split into `numeric_token_1..3` (`cleaning/steps/token_parsing.py`,
`tokenisation.py`). These feed the Splink comparisons and the deterministic flat logic.
This vocabulary is UK dwelling/commercial notation.

### 4.6 Term frequency
Per record, each token gets its relative frequency attached; **unknown tokens default to
`5e-5`** (`cleaning/steps/term_frequencies.py:116`). High-frequency trailing tokens
(counties/cities with corpus count > 3000, e.g. LONDON) are moved into a separate
`common_end_tokens` field so a missing county isn't over-penalised (`:202-262`). Tokens
are then banded into unusual / very-unusual / extremely-unusual arrays, which the Splink
blocking rules key on.

### 4.7 Inverted-index blocking
This is how matching avoids comparing every messy row to every canonical row. An
`IndexingStrategy` produces array-valued keys; defaults are TRIGRAM (3 consecutive tokens)
and BIGRAM (2) (`cleaning/steps/inverted_index.py:8-61`). The build explodes keys, groups,
and **keeps only keys mapping to 1..max canonical ids** (default 20,
`chunking_strategies.py:337`) so ultra-common keys don't blow up the pool. A messy row
looks up all its keys, unions the hit lists, and dedupes into `exploding_unique_ids`
(`inverted_index.py:189-269`). Canonical rows set `exploding_unique_ids = [unique_id]`
(`:285-296`).

### 4.8 The Splink model
`data/splink_model.json`: `link_only`, `probability_two_random_records_match = 3e-08`,
nine comparisons (`clean_full_address`, `address_without_numbers`, `flat_identity`,
`numeric_token_1/2/3`, a token-rel-freq histogram, `common_end_tokens`, `postcode`).
Sixteen blocking rules combine numeric tokens + unusual tokens + postcode parts; the last
is an **exploding** rule over the candidate pool:
`"blocking_rule": "l.exploding_unique_ids = r.exploding_unique_ids"` with
`"arrays_to_explode": ["exploding_unique_ids"]` (`splink_model.json:78-83`) — Splink
unnests the array so each candidate id becomes a blocking key. That is the seam connecting
the inverted index to Splink.

> On the weights: the JSON stores `m_probability` / `u_probability` as **Bayes-factor-style
> weights, not values in [0,1]** (e.g. `15/1`, `8/1`, `1/2` at `splink_model.json:99,136,107`).
> Per comparison level, `match_weight += log2(m/u)`. Describe them as "m/u weights," not
> "probabilities." `unique_id_column_name` is overridden to `ukam_address_id`
> (`splink_model.py:147`); numeric-token TF tables are registered into the linker
> (`splink_model.py:218-228`); blocking-rule subsets are toggled by
> `include_full_postcode_block` / `include_outside_postcode_block`.

### 4.9 The stage framework
`MatchingStage` is an ABC (`linking_model/matching/stages/base_stage.py`). `run()` writes
matches into a shared `results_table` keyed by `ukam_address_id`, **only updating rows
where `resolved_canonical_id IS NULL`**. The runner feeds each stage only the
still-unmatched rows and **breaks when none remain**
(`linking_model/matching/runner.py`). `available_stages()` walks the subclass tree, so a
custom stage you define is auto-discovered. Deterministic stages first restrict canonical
to messy postcodes (another UK-postcode dependency).

### 4.10 Splink stage internals & distinguishability
SplinkStage: build linker -> `predict(threshold)` -> reweight using distinguishing tokens
-> compute distinguishability -> threshold on `match_weight` (default 12 in the example)
and `distinguishability`. **Distinguishability** = `match_weight(best) -
match_weight(second best)` via a `LEAD()` window; NULL when there's only one candidate. It
is the "how ambiguous was this win" signal.

### 4.11 prepare_canonical (precompute & persist)
`prepare_canonical_folder(data, output_folder, *, con, num_of_chunks=10,
output_chunk_count=1, overwrite=False)` (`prepare_canonical.py:278`) writes
`ukam_canonical_addresses.parquet` (or a chunk dir), `ukam_term_frequencies.parquet`,
`ukam_inverted_index.parquet`, and a `ukam_manifest.json` (version, row counts, sha256).
`load_prepared_canonical_data` reads them back, supports remote URIs, checks the manifest
version, and applies `canonical_address_filter` post-load. Pay the cleaning cost once,
match many times.

> **Validation asymmetry to know about:** `prepare_canonical_folder` does **not**
> re-check that `unique_id` / `address_concat` exist (it goes straight into TF derivation,
> `prepare_canonical.py:329-357`). The required-column check lives in `AddressMatcher`
> (`address_matcher.py:47-53`). So a malformed adapter relation fails *late*, inside the
> pipeline, with a less obvious error. Validate your adapter output yourself.

---

## 5. Shipped data assets

Located in `uk_address_matcher/data/`. Provenance ("derived from UK address data") is per
the project; the training code is not in this repo, so treat provenance as attributed.

| File | What it is (verified) | Consumed by | UK-specific? |
|---|---|---|---|
| `address_abbreviations.json` | 146 token→expansion entries | `normalisation.py:315` | yes (UK street vocab) |
| `address_token_frequencies.parquet` | pre-baked token rel_freq | `pipelines.py:324` (fallback) | yes (UK corpus) |
| `numeric_token_frequencies.parquet` | numeric-token tf | `pipelines.py:335` (always) | yes |
| `common_end_tokens.csv` | high-freq locality tokens (LONDON, ESSEX…) | `term_frequencies.py:212` | yes |
| `common_uk_end_tokens.json` | locality suffixes to peel | `peeled.py` | yes |
| `splink_model.json` | trained Fellegi-Sunter model, 9 comparisons, 16 blocking rules | `splink_model.py:14` | yes (trained weights) |

---

## 6. Performance model

Where the speed comes from:
- **DuckDB** vectorised columnar execution of all SQL.
- **Blocking** collapses the O(N_messy × N_canonical) pair space: the inverted-index
  candidate pool + 16 Splink blocking rules + postcode restriction in deterministic stages.
- **Chunking** bounds memory and gives progress logging.
- **Deterministic short-circuit** removes easy cases before Splink runs.

Complexity: cleaning is ~O(N) per side; blocking turns matching from quadratic into
~O(candidate pairs). The dominant one-time cost is canonical prep (TF derive + clean +
index build), which `prepare_canonical_folder` persists so you pay it once. Measured
above: 2k × 10k end-to-end ~9s on a laptop.

---

## 7. Key invariants & assumptions

Machinery that is **dataset-agnostic**: the SQL-pipeline framework, chunking, the inverted
index, the stage runner, the Splink scoring mechanism, and prepare/load-folder.

Assumptions that are **UK / Ordnance-Survey-specific**:
- UK postcode regex for extraction, canonicalisation, and blocking
  (`normalisation.py:56,139`; deterministic-stage input filters).
- Token + numeric term frequencies derived from a UK corpus; unknown-token floor `5e-5`.
- Abbreviations are UK street vocabulary, single-token keys.
- Locality peel lists are UK cities/counties/boroughs.
- The flat/business-unit parser encodes UK dwelling/commercial notation.
- `unique_id` must be stable and identify the canonical entity; `ukam_address_id` is internal.

Those last six are exactly what you swap or accept when pointing the matcher at a new
dataset. See **EXTENDING-TO-NEW-CANONICAL-SOURCES.md**.

---

## 8. Where to intervene (extension seams)

- **Exclude canonical records** (e.g. car parks, POIs): `canonical_address_filter` SQL on
  the `AddressMatcher` (`address_matcher.py:172-175`) or prepared-folder load
  (`prepare_canonical.py`). Column is `address_concat` for a raw relation,
  `original_address_concat` for a prepared folder. See `../scripts/parking_filter.py` and
  `../notebooks/03_car_park_canonical_records.ipynb`.
- **New canonical dataset**: satisfy the contract and call `prepare_canonical_folder`. See
  the extension doc and `adapters/`.
- **Custom matching stage**: subclass `MatchingStage`; it's auto-discovered.
- **New abbreviations**: add to `data/address_abbreviations.json` (single-token keys).
