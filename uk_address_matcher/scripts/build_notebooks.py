"""Generate the three starter notebooks for the uk_address_matcher exploration.

Why a generator? Hand-authored .ipynb JSON is error-prone; building with
nbformat guarantees valid notebooks, and this file doubles as a readable,
diff-friendly source of the notebook content.

Run:
    uv run python uk_address_matcher/scripts/build_notebooks.py

Writes (next to this script's parent) into ../notebooks/:
    01_understand_uk_address_matcher.ipynb
    02_abbreviation_expansions_rc_hmp.ipynb
    03_car_park_canonical_records.ipynb
"""

from __future__ import annotations

from pathlib import Path

import nbformat as nbf

NOTEBOOK_DIR = Path(__file__).resolve().parent.parent / "notebooks"


def md(text: str) -> nbf.NotebookNode:
    return nbf.v4.new_markdown_cell(text.strip("\n"))


def code(text: str) -> nbf.NotebookNode:
    return nbf.v4.new_code_cell(text.strip("\n"))


def write_notebook(name: str, cells: list[nbf.NotebookNode]) -> Path:
    nb = nbf.v4.new_notebook()
    nb["cells"] = cells
    nb["metadata"] = {
        "kernelspec": {
            "display_name": "OSS Playground (splink+ukam)",
            "language": "python",
            "name": "oss-playground",
        },
        "language_info": {"name": "python"},
    }
    NOTEBOOK_DIR.mkdir(parents=True, exist_ok=True)
    path = NOTEBOOK_DIR / name
    nbf.write(nb, path)
    return path


# ---------------------------------------------------------------------------
# Notebook 01 — Understand what uk_address_matcher does
# ---------------------------------------------------------------------------
NB01 = [
    md(
        """
# 01 · Understand `uk_address_matcher` end-to-end

`uk_address_matcher` is a **geocoder / address matcher**: given a *messy* address it
finds the best-matching *canonical* address (typically Ordnance Survey AddressBase or
NGD). It runs on a laptop, is built on **DuckDB** (SQL engine) + **Splink**
(probabilistic record linkage), and ships a pre-trained Splink model.

This notebook answers: *what does it actually do, and what are the moving parts?*
Later notebooks dig into two concrete contribution opportunities:

- **02** — adding abbreviation expansions (`RC → ROMAN CATHOLIC`, `HMP → HIS MAJESTYS PRISON`).
- **03** — keeping car-park "addresses" out of the canonical candidate pool.

> First run downloads two small parquet datasets from GitHub (cached afterwards).
"""
    ),
    md(
        """
## Inputs

Both the messy and canonical tables need just two columns (a third is recommended):

| Column | Type | Required | Purpose |
|---|---|---|---|
| `unique_id` | BIGINT or VARCHAR | yes | stable per-row id |
| `address_concat` | VARCHAR | yes | the address text (ideally **excluding** postcode) |
| `postcode` | VARCHAR | recommended | used for blocking + matching; parsed out of `address_concat` if absent |
"""
    ),
    code(
        """
import duckdb
from uk_address_matcher import (
    AddressMatcher, ExactMatchStage, PeeledAddressStage, SplinkStage, ukam_datasets,
)

con = duckdb.connect(database=":memory:")

# Bundled fictional dataset (downloads on first use, then cached).
messy, canonical = ukam_datasets.fictional_london
print("messy columns   :", messy.columns)
print("canonical columns:", canonical.columns)
print("messy rows      :", messy.aggregate("count(*) AS n").fetchone()[0])
print("canonical rows  :", canonical.aggregate("count(*) AS n").fetchone()[0])
"""
    ),
    code(
        """
# Use .df() throughout — it renders as a clean table in Jupyter and avoids
# Windows-console encoding issues you'd hit with relation.show().
messy.limit(5).df()
"""
    ),
    code(
        """
canonical.limit(5).df()
"""
    ),
    md(
        """
## The matching pipeline = a list of **stages**

`AddressMatcher` runs an ordered list of stages. Earlier stages settle the easy cases
cheaply; the probabilistic stage handles the fuzzy remainder.

- `ExactMatchStage()` — deterministic / exact matches.
- `PeeledAddressStage()` — handles "peeling" leading sub-building tokens.
- `UniqueTrigramStage()` — match on a canonical-unique trigram.
- `SplinkStage(...)` — probabilistic matching (typos, reordering, missing tokens),
  using the pre-trained Splink model and **term-frequency** weighting.

Behind the scenes the canonical side is cleaned, term frequencies are computed, and an
**inverted index** (trigram/bigram keys → canonical ids) builds each messy record's
candidate pool (`exploding_unique_ids`) so Splink only scores plausible pairs.
"""
    ),
    code(
        """
print(AddressMatcher.available_stages())
"""
    ),
    code(
        """
matcher = AddressMatcher(
    canonical_addresses=canonical,
    addresses_to_match=messy,
    con=con,
    stages=[
        ExactMatchStage(),
        PeeledAddressStage(),
        SplinkStage(
            predict_threshold_match_weight=-20,
            final_match_weight_threshold=12,
            include_full_postcode_block=True,
            retain_intermediate_calculation_columns=True,
        ),
    ],
)
result = matcher.match()
result.matches().limit(10).df()
"""
    ),
    md(
        """
### Output columns

| Column | Meaning |
|---|---|
| `unique_id` | the messy record |
| `resolved_canonical_id` | the chosen canonical `unique_id` |
| `original_address_concat` / `..._canonical` | the two address texts |
| `match_reason` | which stage decided (e.g. `splink: probabilistic match`) |
| `match_weight` | Splink confidence (log2 Bayes factor; higher = more confident) |
| `distinguishability` | gap in weight to the next-best candidate (bigger = less ambiguous) |
"""
    ),
    code(
        """
# How many matches came from each stage / reason?
result.match_metrics().df()
"""
    ),
    md(
        """
## Peeking under the hood: the candidate pool

`._splink_predictions()` returns every *candidate pair* Splink scored — not just the
winner. This is the lens we use in notebook 03 to see bad candidates (car parks)
sitting in the pool next to the right answer.
"""
    ),
    code(
        """
sp = result._splink_predictions(limit=5)
# A few of the most useful columns:
cols = [c for c in ["match_weight", "match_probability",
                    "original_address_concat_l", "original_address_concat_r"]
        if c in sp.columns]
sp.select(", ".join(cols)).order("match_weight DESC").df()
"""
    ),
    md(
        """
## Takeaways & where it can be improved

- The matcher is a **staged pipeline** over DuckDB + a pre-trained Splink model.
- **Cleaning** (normalisation, abbreviation expansion) happens before matching and
  strongly affects which candidates look similar → **notebook 02**.
- The **canonical candidate pool** can contain records that aren't real dwellings
  (e.g. car-park spaces) which then compete with the right answer → **notebook 03**.
"""
    ),
]


# ---------------------------------------------------------------------------
# Notebook 02 — Abbreviation / expansion feature (RC, HMP)
# ---------------------------------------------------------------------------
NB02 = [
    md(
        """
# 02 · Abbreviation expansions — `RC → ROMAN CATHOLIC`, `HMP → HIS MAJESTYS PRISON`

**Feature request:** *"feat: further expansions/abbreviations"* — e.g.
`RC CHURCH → ROMAN CATHOLIC CHURCH` and `HMP → His Majesty's Prison`.

This notebook explains **where** expansions live, **how** they are applied, **why**
these cases matter, and the one architectural constraint a contributor must respect.
It uses only tiny in-memory data, so it runs offline.
"""
    ),
    md(
        """
## Where expansions are defined

A single JSON file, shipped inside the package:

`uk_address_matcher/data/address_abbreviations.json` — a list of
`{"token": <abbrev>, "replacement": <expansion>}` objects (e.g. `RD → ROAD`,
`FLT → FLAT`). Adding `RC` and `HMP` here is the entire contribution surface for data;
no code change is required.
"""
    ),
    code(
        """
import json
import importlib.resources as ir
import duckdb

raw = ir.files("uk_address_matcher.data").joinpath("address_abbreviations.json").read_text()
abbr = json.loads(raw)
tokens = {d["token"].upper() for d in abbr}

print(f"{len(abbr)} abbreviation entries currently shipped")
print("RC present? ", "RC" in tokens)
print("HMP present?", "HMP" in tokens)
print("\\nA few existing entries:")
for d in abbr[:8]:
    print(f"  {d['token']:>6}  ->  {d['replacement']}")
"""
    ),
    md(
        """
## How expansions are applied — **single-token MAP lookup** (the key constraint)

In `cleaning/steps/normalisation.py`, `_normalise_abbreviations_and_units()` builds a
DuckDB `MAP` from the JSON and applies it **token by token**:

```sql
array_to_string(
  list_transform(
    string_split(clean_full_address, ' '),     -- split on spaces
    x -> COALESCE(map_extract(abbr_map, x)[1], x)  -- replace token if found, else keep
  ), ' ')
```

Consequences you must design around:

- The **key** is matched against a *single whitespace token*. A multi-word key like
  `CAR PARK SPACE` can **never** match this map.
- The **replacement value may be multiple words** — `array_to_string` re-joins them.
  So `RC → ROMAN CATHOLIC` turns `RC CHURCH` into `ROMAN CATHOLIC CHURCH` for free.
- Matching is case-insensitive (keys are upper-cased) and runs **after** punctuation
  is stripped — so encode `His Majesty's` as `HIS MAJESTYS` (no apostrophe).
"""
    ),
    md(
        """
### See the *real* pipeline expand an existing abbreviation

`prepare_data_for_matching` runs the full cleaning pipeline and exposes the
`clean_full_address` column. Watch `FLT`/`GRD`/`RD` expand:
"""
    ),
    code(
        """
from uk_address_matcher import prepare_data_for_matching

con = duckdb.connect(":memory:")
demo = con.sql(\"\"\"
SELECT * FROM (VALUES
  ('a1', 'FLT 5 GRD FLOOR 10 HIGH RD', 'AB1 2CD'),
  ('a2', 'APT 2 SUMMER LN', 'AB1 2CD')
) t(unique_id, address_concat, postcode)
\"\"\")
prepare_data_for_matching(demo, con=con).select(
    "original_address_concat, clean_full_address"
).df()
"""
    ),
    md(
        """
## Simulating the proposed `RC` / `HMP` additions

We can't edit the installed package's JSON from here (the upstream clone must stay
pristine), so we **replicate the exact single-token MAP transform** in DuckDB with the
shipped abbreviations *plus* the two proposed entries. This is precisely what the
pipeline step would produce after the JSON change.
"""
    ),
    code(
        """
import pandas as pd

PROPOSED = [
    {"token": "RC",  "replacement": "ROMAN CATHOLIC"},
    {"token": "HMP", "replacement": "HIS MAJESTYS PRISON"},   # apostrophe already stripped upstream
]
abbr_df = pd.DataFrame(abbr + PROPOSED, columns=["token", "replacement"])
con.register("abbr", abbr_df)

# Cleaned text is upper-cased + punctuation-free by the time abbreviation expansion runs.
test_inputs = [
    "RC CHURCH HALL 5 EXAMPLE ROAD",
    "HMP LEEDS ARMLEY",
    "FLAT 2 RC PRESBYTERY 9 CHAPEL STREET",
]
inp_values = ", ".join(f"('{s}')" for s in test_inputs)

con.sql(f\"\"\"
WITH lookup AS (
    SELECT UPPER(TRIM(token)) AS token, TRIM(replacement) AS replacement
    FROM abbr WHERE token IS NOT NULL AND replacement IS NOT NULL
),
m AS (SELECT map(list(token), list(replacement)) AS abbr_map FROM lookup),
inp AS (SELECT * FROM (VALUES {inp_values}) t(clean_full_address))
SELECT
  inp.clean_full_address AS before,
  array_to_string(
    list_transform(string_split(inp.clean_full_address, ' '),
                   x -> COALESCE(map_extract(m.abbr_map, x)[1], x)),
    ' '
  ) AS after
FROM inp CROSS JOIN m
\"\"\").df()
"""
    ),
    md(
        """
## Why these cases — and the risk to weigh

**Why it helps:** matching is token/term-frequency driven. If a messy record says
`RC CHURCH` but the canonical says `ROMAN CATHOLIC CHURCH` (or vice-versa), the shared
signal is weak. Expanding both sides to a common form makes the tokens line up.

**The risk — over-expansion.** Because expansion is unconditional and single-token,
**every** standalone `RC` becomes `ROMAN CATHOLIC`, and **every** `HMP` becomes
`HIS MAJESTYS PRISON`, regardless of context. For `HMP` that is almost always safe (it
is an unusual token). For `RC` it is worth checking it doesn't routinely appear as
something else (initials, a road/block code, etc.). The single-token architecture means
you **cannot** scope it to "only when followed by CHURCH" without changing the mechanism.
"""
    ),
    md(
        """
## Proposed contribution

1. Add to `uk_address_matcher/data/address_abbreviations.json`:
   ```json
   { "token": "RC",  "replacement": "ROMAN CATHOLIC" },
   { "token": "HMP", "replacement": "HIS MAJESTYS PRISON" }
   ```
2. Add a test (the project tests cleaning output) asserting that
   `RC CHURCH → ROMAN CATHOLIC CHURCH` and `HMP LEEDS → HIS MAJESTYS PRISON LEEDS`
   after `prepare_data_for_matching`.
3. In the PR, note the over-expansion trade-off for `RC` and (if needed) propose a
   follow-up if multi-word *keys* are ever required — that would mean extending the
   normalisation step beyond a single-token MAP (e.g. an ordered phrase-replacement
   pass), which is a larger change than this data-only addition.
"""
    ),
]


# ---------------------------------------------------------------------------
# Notebook 03 — Car-park canonical records
# ---------------------------------------------------------------------------
NB03 = [
    md(
        """
# 03 · Car-park records in the canonical pool

**Issue:** obvious parking-space *canonical* records leak into the candidate pool and
sometimes **win** against real dwellings.

```
dwelling : FLAT 5 BASEMENT 447 EXAMPLE ROAD LONDON
parking  : CAR PARK SPACE 5 EXAMPLE COURT 447 EXAMPLE ROAD LONDON
```

A text check for `CAR PARK SPACE` / `CAR PARKING SPACE` reportedly matches roughly
**16,659** canonical rows nationally (52 in Hackney). The proposed first step:
**exclude obvious parking-space canonical records during matching** with a *narrow*
text rule, leaving broader garage filtering for later.

This notebook reproduces the failure conceptually on tiny data, shows the fix, and
checks the rule is safe (doesn't eat real "garage" dwellings). Runs offline.
"""
    ),
    md(
        """
## Why a parking record competes with a dwelling

Matching strength comes from **shared, rare tokens**. The parking and dwelling records
above share the building number `447`, the street `EXAMPLE ROAD`, the town `LONDON`
*and the same postcode* — so they land in the same candidate pool (blocking) and share
most of the high-value tokens. The only tokens that distinguish them (`CAR`, `PARK`,
`SPACE`, `BASEMENT`) are a small part of the signal. When the messy address is itself
sparse or ambiguous, the parking record can edge out the real dwelling.
"""
    ),
    code(
        """
import duckdb
from uk_address_matcher import AddressMatcher, SplinkStage

# Canonical pool for one building (same postcode) — a dwelling, a ground flat,
# a CAR PARK SPACE record, plus the user's "garage" examples to test safety later.
CANON_SQL = \"\"\"
SELECT * FROM (VALUES
  ('c_flat5b',  'FLAT 5 BASEMENT 447 EXAMPLE ROAD LONDON',                'E1 6AA'),
  ('c_gflat5',  'GROUND FLAT 5 447 EXAMPLE ROAD LONDON',                  'E1 6AA'),
  ('c_carpark', 'CAR PARK SPACE 5 EXAMPLE COURT 447 EXAMPLE ROAD LONDON', 'E1 6AA'),
  ('c_garage1', 'FLAT OVER GARAGE 8 EXAMPLE ROAD LONDON',                 'E1 6AA'),
  ('c_garage2', 'FIRST FLAT 1 EXAMPLE GARAGE YARD SUMMER LANE LONDON',    'E1 6AA')
) t(unique_id, address_concat, postcode)
\"\"\"

def run_match(messy_sql, canonical_filter=None):
    \"\"\"Fresh connection per run: Splink registers temp tables that clash on reuse.\"\"\"
    con = duckdb.connect(":memory:")
    matcher = AddressMatcher(
        canonical_addresses=con.sql(CANON_SQL),
        addresses_to_match=con.sql(messy_sql),
        con=con,
        canonical_address_filter=canonical_filter,
        stages=[SplinkStage(
            predict_threshold_match_weight=-30,
            final_match_weight_threshold=-30,   # keep everything so we can SEE the candidates
            include_full_postcode_block=True,
        )],
    )
    return matcher

con0 = duckdb.connect(":memory:")
con0.sql(CANON_SQL).df()
"""
    ),
    md(
        """
## The parking record is in the candidate pool

For a messy `FLAT 5 447 EXAMPLE ROAD LONDON`, list every candidate Splink scored.
The car-park record shows up as a genuine candidate.
"""
    ),
    code(
        """
m = run_match("SELECT * FROM (VALUES ('m_1','FLAT 5 447 EXAMPLE ROAD LONDON','E1 6AA')) t(unique_id,address_concat,postcode)")
res = m.match()
sp = res._splink_predictions(limit=50)
sp.select(
    "round(match_weight,2) AS match_weight, unique_id_l AS canonical_id, "
    "original_address_concat_l AS canonical_address"
).order("match_weight DESC").df()
"""
    ),
    md(
        """
## The failure mode: when the parking record **wins**

Make the messy address ambiguous — closer to the parking record's distinctive tokens
(`SPACE`, `EXAMPLE COURT`). Now the car-park record is the top match.
"""
    ),
    code(
        """
AMBIGUOUS = ("SELECT * FROM (VALUES "
             "('m_amb','SPACE 5 EXAMPLE COURT 447 EXAMPLE ROAD LONDON','E1 6AA')) "
             "t(unique_id,address_concat,postcode)")

without_filter = run_match(AMBIGUOUS).match().matches()
without_filter.select(
    "unique_id, resolved_canonical_id, original_address_concat_canonical, round(match_weight,2) AS mw"
).df()
"""
    ),
    md(
        """
## The fix — `canonical_address_filter`

`AddressMatcher` accepts `canonical_address_filter`: a DuckDB SQL boolean expression
applied to the canonical input **before** matching. We keep the rule **narrow** — only
`CAR PARK SPACE` / `CAR PARKING SPACE`:

```python
PARKING_FILTER = "NOT regexp_matches(upper(address_concat), 'CAR PARK(ING)? SPACE')"
```

> Note on the column name: for a **raw** canonical relation the column is
> `address_concat` (as here). For a **prepared canonical folder** the filter runs against
> the prepared columns — use `original_address_concat` there.
"""
    ),
    code(
        """
PARKING_FILTER = "NOT regexp_matches(upper(address_concat), 'CAR PARK(ING)? SPACE')"

with_filter = run_match(AMBIGUOUS, canonical_filter=PARKING_FILTER).match().matches()
with_filter.select(
    "unique_id, resolved_canonical_id, original_address_concat_canonical, round(match_weight,2) AS mw"
).df()
"""
    ),
    md(
        """
The winner flips from the car-park record to a real dwelling once parking records are
excluded from the pool. That is the whole point of the issue's "first step".
"""
    ),
    md(
        """
## Is the narrow rule safe? Check it doesn't eat real dwellings

The user flagged genuinely-residential addresses that merely contain the word
*garage*: `FLAT OVER GARAGE ...`, `... EXAMPLE GARAGE YARD ...`. A *broad* garage filter
would wrongly drop these. Our narrow `CAR PARK(ING)? SPACE` rule leaves them untouched —
let's confirm by listing which canonical rows the filter removes vs keeps.
"""
    ),
    code(
        """
con = duckdb.connect(":memory:")
canon = con.sql(CANON_SQL)
flagged = canon.select(
    "unique_id, address_concat, "
    "regexp_matches(upper(address_concat), 'CAR PARK(ING)? SPACE') AS removed_by_filter"
)
flagged.df()
"""
    ),
    code(
        """
removed = canon.filter("regexp_matches(upper(address_concat), 'CAR PARK(ING)? SPACE')")
kept    = canon.filter("NOT regexp_matches(upper(address_concat), 'CAR PARK(ING)? SPACE')")
print("removed:", removed.aggregate("count(*) n").fetchone()[0],
      "| kept:", kept.aggregate("count(*) n").fetchone()[0])
print("removed ids:", [r[0] for r in removed.select('unique_id').fetchall()])
"""
    ),
    md(
        """
## Scoping & where this belongs

- **Narrow first (recommended).** `CAR PARK SPACE` / `CAR PARKING SPACE` is high-precision:
  these are unambiguous non-dwellings. Broader terms (`GARAGE`, `PARKING`) risk false
  positives like *FLAT OVER GARAGE* — defer until proven safe with data.
- **Where to apply.** Today the cleanest lever is `canonical_address_filter` at
  `AddressMatcher` construction (raw relation) **or** when calling
  `prepare_canonical_folder` / `load_prepared_canonical_data` (prepared folders).
- **Longer term.** The issue frames this as a *raw canonical-data* hygiene problem.
  A reusable, well-tested `canonical` cleaning step (or a documented, shipped default
  filter expression) would beat every caller re-inventing the regex. That is the
  natural shape of the contribution: ship the narrow rule + tests, document the column
  caveat (raw vs prepared), and leave broader garage filtering as a follow-up.

See `scripts/parking_filter.py` for a reusable version of this filter.
"""
    ),
]


def main() -> None:
    for name, cells in [
        ("01_understand_uk_address_matcher.ipynb", NB01),
        ("02_abbreviation_expansions_rc_hmp.ipynb", NB02),
        ("03_car_park_canonical_records.ipynb", NB03),
    ]:
        path = write_notebook(name, cells)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
