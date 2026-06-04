# Extending uk_address_matcher to New Canonical Sources

The matcher ships tuned for Ordnance Survey data, but the gazetteer you match against is
swappable. This doc works out, from first principles, how to point it at other datasets:
**OpenStreetMap, Overture Maps, Companies House, and UK schools (GIAS)**. Read
`ARCHITECTURE.md` first for the internals; this builds on §7 (invariants) and §8 (seams).

The thesis in one line: **the machinery is portable, the data and rules are UK-specific.**
A new source is an ingestion shim, not a fork.

---

## 1. The canonical-source contract

### 1a. The minimal hard interface
A canonical source is any DuckDB relation with three columns
(`address_matcher.py:47-53`):

| Column | Rule |
|---|---|
| `unique_id` | VARCHAR/BIGINT, non-NULL, **stable across rebuilds**. Becomes `resolved_canonical_id`. |
| `address_concat` | VARCHAR free text. Ideally exclude the postcode (cleaning strips it anyway). |
| `postcode` | optional; supplied as-is and preferred over text extraction, else NULL is added. |

That is the entire hard contract. Everything else (flat parsing, term frequencies,
blocking keys) is derived. Satisfy this, call `prepare_canonical_folder`, done.

### 1b. The implicit assumptions (the part that bites)
"Just three columns" is true and misleading. The pipeline bakes in UK assumptions
(ARCHITECTURE §7). For a new source, each one either degrades gracefully or hurts:

| Assumption | Non-UK / new-source effect | Verdict |
|---|---|---|
| UK postcode regex | a non-UK postcode is **not** auto-extracted from text. But if you pass `postcode` explicitly it's used as an opaque string for comparison. | supply `postcode` and it works; rely on extraction and it won't |
| UK token frequencies + `5e-5` floor | out-of-corpus tokens all collapse to the floor, flattening discrimination | re-derive TFs when vocabulary diverges (§4) |
| UK abbreviations / locality peel | harmless no-op for irrelevant vocab; just absent for new vocab | acceptable; extend the JSON if needed |
| UK flat/business-unit parser | mostly produces NULLs for non-dwellings (graceful); can mis-fire on words like "UNIT" in a company name | acceptable, watch for noise |

**Separation of concerns** (the whole reason this is feasible):

| Dataset-agnostic machinery | UK / OS-specific data + rules |
|---|---|
| SQL-pipeline framework, chunking | postcode regex |
| inverted-index blocking | token + numeric term frequencies |
| stage runner + Splink scoring | abbreviations JSON |
| prepare/load canonical folder | locality peel lists |
| `canonical_address_filter` | flat/business-unit parser |

All four sources below are UK-domiciled, so the postcode regex and peel lists apply. The
main per-source decision is **whether to re-derive term frequencies.**

---

## 2. The adapter pattern

A new source needs no library change for the happy path. It's a three-function shim that
lands in this playground (`adapters/`):

```
fetch_<source>()   ->   to_canonical()        ->   prepare_canonical_folder()   ->  folder/
download/extract       project to the              the library's own, UNCHANGED        |
native rows            (unique_id,                                                       v
                        address_concat,                          AddressMatcher(canonical_addresses="folder/",
                        postcode); mint id                                       addresses_to_match=messy, con=con)
```

- `fetch_<source>()` — get the raw rows into DuckDB (CSV read, parquet read, API pull).
- `to_canonical(raw, con)` — project native fields onto the contract; mint the stable
  `unique_id`; build `address_concat` in number→street→locality order with postcode held
  separately.
- `build(raw, folder, con)` — `prepare_canonical_folder(to_canonical(raw, con), folder, con=con)`.

**Free functions, not a base class.** The contract is a relation shape, not behaviour to
subclass, and `prepare_canonical_folder` already orchestrates. A `SourceAdapter` ABC would
be ceremony around one real implementation. Revisit only if several adapters grow shared
logic.

**Validate your own output.** `prepare_canonical_folder` does not re-check the required
columns (ARCHITECTURE §4.11); a bad projection fails late inside the pipeline. A one-line
assert in `to_canonical` that `unique_id` and `address_concat` are present and non-NULL
saves a confusing stack trace.

See `adapters/gias.py` for a working, tested implementation, and `adapters/osm.py`,
`adapters/overture.py`, `adapters/companies_house.py` for design-stub projections.

---

## 3. Per-source designs

### 3a. UK schools — GIAS (the worked, tested example)
- **Source / licence:** Get Information About Schools establishment CSV. OGL v3.0.
- **id:** `URN` (official, stable).
- **address_concat:** `concat_ws(' ', EstablishmentName, Street, Locality, Town)`;
  `postcode` direct.
- **Quality:** small (~50k), clean, real UK postcodes. **No TF retraining needed** — the
  pre-baked UK frequencies apply well.
- **Status:** implemented and verified in `adapters/gias.py` (see §5).

### 3b. OpenStreetMap
- **Source / licence:** Overpass API or a Geofabrik/planet extract. **ODbL 1.0**
  (share-alike + attribution) — the strictest here; anything you derive and publish
  inherits it.
- **id:** `osm_type/osm_id` (`node/123`, `way/456`). Stable per object, but objects churn
  with edits/deletions; record the extract date.
- **address_concat:** `concat_ws(' ', housenumber, unit, street, city)` from `addr:*` tags.
- **Gaps:** sparse, inconsistent tagging; **many objects lack a postcode**, so postcode
  blocking weakens and the matcher leans on the inverted index and
  `SplinkStage(include_outside_postcode_block=True)`. POI names skew vocabulary →
  **re-derive TFs**.

### 3c. Overture Maps
- **Source / licence:** `addresses` (and optionally `places`) themes, GeoParquet on cloud.
  **CDLA-Permissive 2.0** for most themes (some upstream ODbL); attribution.
- **id:** **GERS id** — globally unique, designed to be stable across releases. Best
  id-stability of the four; lean on it.
- **address_concat:** from the addresses theme number/street/locality fields; postcode
  from the postal field (names vary by release).
- **Gaps:** global schema → extract the UK subset first (bbox or country = GB). The
  `places` theme mixes POIs (non-dwelling) → see §4 and the parking filter. Large corpus →
  **re-derive TFs** from the UK subset.

### 3d. Companies House
- **Source / licence:** Free Company Data monthly snapshot (registered-office addresses).
  **OGL**.
- **id:** `CompanyNumber` (very stable).
- **Canonical vs messy — decide the role:**
  - *As messy (recommended):* you have CH addresses and geocode them to AddressBase/OSM.
    Natural — CH is the dirty side.
  - *As canonical:* it's a registered-office gazetteer, not dwellings. Many companies share
    one accountant's address → heavy many-to-one collisions, which the matcher correctly
    reports as low `distinguishability`. Good for "is this a registered office?" lookups,
    not for resolving to a unique premises.
- **Gaps:** org-name vocabulary (LTD, HOLDINGS, &) → **re-derive TFs** if used as canonical.
  Non-dwelling → §4 applies.

---

## 4. Cross-cutting concerns

**Re-derive term frequencies, or reuse pre-baked UK?**
- *Reuse* when the source is UK, dwelling-like, vocabulary overlaps AddressBase, and it's
  small (GIAS).
- *Re-derive* when vocabulary diverges (OSM POIs, Overture places, CH org names) or the
  corpus is large. Use the public path: `derive_term_frequencies_table(rel, con)` then pass
  `term_frequency_lookup=...`. `prepare_canonical_folder` already derives token TFs from
  the data you give it.
- **Limitation:** the **numeric** TF table is always pre-baked UK and can't be overridden
  via the folder path (ARCHITECTURE §3, `pipelines.py:303-340`). Document, don't fight it.

**Id strategy & provenance.** Keep source-native stable ids. The prepared-folder manifest
already records ukam/duckdb versions + hashes. When **mixing sources**, prefix ids by
source (`os_…`, `osm_…`, `gers_…`) to keep provenance, union the adapter outputs into one
relation **before** prepare, and derive TFs over the combined corpus. Cross-source
collisions on the same physical address are expected and fine.

**Dwelling vs non-dwelling (the car-park generalisation).** The matcher has **no built-in
content filter** (`address_matcher.py:172-175`). Non-dwelling records (car parks, POIs,
registered offices) can out-rank real dwellings. Reuse the narrow filter pattern in
`../scripts/parking_filter.py`, mindful of the column caveat: `address_concat` for a raw
relation, `original_address_concat` for a prepared folder. This is exactly notebook 03
generalised to new sources that carry POIs.

**Evaluation.** Put a labelled `ukam_label` column on your messy input and use
`MatchResult.accuracy_analysis()` to measure top-1 precision/recall for the new source.
Don't trust a new canonical source until you've measured it on labelled data.

**Licensing summary** (confirm current terms before redistributing):

| Source | Licence |
|---|---|
| OS AddressBase / NGD | PSGA / commercial |
| OpenStreetMap | ODbL 1.0 |
| Overture Maps | CDLA-Permissive 2.0 (some themes ODbL) |
| Companies House | OGL |
| GIAS | OGL |

---

## 5. Worked minimal viable extension (GIAS)

Implemented and **verified** in `adapters/gias.py` against `adapters/fixtures/gias_sample.csv`.
The core is just the contract projection + the library's own folder builder:

```python
import duckdb
from uk_address_matcher import AddressMatcher, prepare_canonical_folder

con = duckdb.connect()
raw = con.read_csv("gias_establishments.csv")          # ~140 native columns

canonical = con.sql("""
    SELECT CAST(URN AS VARCHAR)                                      AS unique_id,
           concat_ws(' ', EstablishmentName, Street, Locality, Town) AS address_concat,
           Postcode                                                  AS postcode
    FROM raw
    WHERE URN IS NOT NULL
""")

prepare_canonical_folder(canonical, "prepared_gias", con=con, overwrite=True)   # one-time

matcher = AddressMatcher(canonical_addresses="prepared_gias",
                         addresses_to_match=messy, con=con)
result = matcher.match()
```

Verified run on the fixture: a messy query
`St Marys Roman Catholic Primary, Church Lane, Springfield` (SP1 3CD) resolves to
**URN 100002** (St Mary's RC Primary) at match_weight 26.33. Run it:

```powershell
uv run python uk_address_matcher/docs/adapters/gias.py
```

To do Overture or CH, copy this shape, swap the `SELECT`, and decide TF retraining per §4.

---

## 6. Upstream candidates (library changes, if pursued)

Things that work as playground adapter code today but would need a library change to serve
non-UK or POI-heavy sources cleanly. File as issues on the upstream repo if you pursue them.

1. **Configurable postcode regex.** Hard-coded UK pattern in `normalisation.py:56`. A
   pluggable regex (or a "trust the supplied postcode, skip extraction" flag) would unlock
   non-UK sources.
2. **Configurable locality peel lists.** UK cities/counties are baked into
   `common_uk_end_tokens.json` / `peeled.py`. Make the list injectable for other regions.
3. **Numeric-TF override.** Currently always pre-baked UK (`pipelines.py:303-340`); no
   public way to supply your own. A `numeric_term_frequency_lookup` parameter would let a
   large non-UK corpus tune it.
4. **Optional column validation in `prepare_canonical_folder`.** It doesn't re-check the
   required columns (ARCHITECTURE §4.11), so bad input fails late. A cheap up-front check
   would give adapter authors a clear error.

A thin `SourceAdapter` protocol could also be upstreamed, but since
`prepare_canonical_folder` is already the seam, adapters are better proven as example code
first. Don't upstream an abstraction before there are three real users of it.
