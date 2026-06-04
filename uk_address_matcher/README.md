# uk_address_matcher — contribution playground

Notebooks and scripts for understanding [`uk_address_matcher`](https://github.com/moj-analytical-services/uk_address_matcher)
and prototyping two contributions. Everything imports the **local editable clone** at
`../../uk_address_matcher`, so edits there are picked up live.

## Docs

| File | What it covers |
|---|---|
| `docs/ARCHITECTURE.md` | How the matcher works inside: data flow, cleaning pipeline, term frequency, inverted-index blocking, the Splink model, stage framework, `prepare_canonical_folder`. Cited to `path:line`. |
| `docs/EXTENDING-TO-NEW-CANONICAL-SOURCES.md` | First-principles design for pointing the matcher at OSM, Overture, Companies House, and UK schools (GIAS): the canonical-source contract, the adapter pattern, per-source designs, and upstream candidates. |
| `docs/adapters/gias.py` | Working, tested GIAS adapter (the worked example). Run: `uv run python uk_address_matcher/docs/adapters/gias.py`. |
| `docs/adapters/{osm,overture,companies_house}.py` | Design-stub adapters: real `to_canonical` projections, `fetch()` raises (needs a network extract). |

## Run it

```powershell
cd C:\Users\Dee\arche\oss\playground
uv run jupyter lab          # open notebooks/*.ipynb, kernel: "OSS Playground (splink+ukam)"
```

The notebooks are already executed with outputs saved, so you can read them without
running. To regenerate them from source, edit `scripts/build_notebooks.py` and run
`uv run python uk_address_matcher/scripts/build_notebooks.py`.

> Tip (Windows): in plain scripts, DuckDB's `relation.show()` can crash the console with
> a `cp1252` encoding error on box-drawing chars. Use `relation.df()` (as the notebooks
> do) or set `PYTHONUTF8=1`. Notebooks are unaffected.

## Notebooks

| File | What it covers |
|---|---|
| `notebooks/01_understand_uk_address_matcher.ipynb` | End-to-end: inputs, the staged pipeline (`ExactMatch → Peeled → Splink`), output columns, and how to inspect the candidate pool via `_splink_predictions()`. Uses the bundled `fictional_london` dataset (downloads on first run). |
| `notebooks/02_abbreviation_expansions_rc_hmp.ipynb` | The *"further expansions/abbreviations"* feature. Where expansions live, the **single-token MAP** mechanism, a faithful simulation of adding `RC`/`HMP`, and the over-expansion risk. Offline. |
| `notebooks/03_car_park_canonical_records.ipynb` | Reproduces parking-space canonical records winning against dwellings, fixes it with `canonical_address_filter`, and verifies the narrow rule doesn't eat real "garage" homes. Offline. |

## Scripts

| File | What it is |
|---|---|
| `scripts/build_notebooks.py` | nbformat generator — the source of truth for the three notebooks. |
| `scripts/parking_filter.py` | Reusable, importable + runnable version of the narrow `CAR PARK SPACE` / `CAR PARKING SPACE` filter (`parking_filter_sql`, `count_parking_records`). |

## Key findings (verified against the library)

**Abbreviation expansions** — defined in
`uk_address_matcher/data/address_abbreviations.json` (146 entries today; no `RC`/`HMP`)
and applied in `cleaning/steps/normalisation.py::_normalise_abbreviations_and_units()`.

- The transform is **single-token**: it splits on spaces and replaces each token via a
  DuckDB `MAP`. So `RC → ROMAN CATHOLIC` and `HMP → HIS MAJESTYS PRISON` work as
  **single-token entries** (the *replacement value* may be multiple words; `RC CHURCH`
  becomes `ROMAN CATHOLIC CHURCH` for free).
- A multi-word **key** like `CAR PARK SPACE` can never match this map.
- Expansion runs after punctuation is stripped → encode `His Majesty's` as `HIS MAJESTYS`.
- **Contribution:** add the two JSON entries + a cleaning test. Flag the unconditional
  over-expansion of `RC` in the PR.

**Car-park canonical records** — there is no built-in content filter on canonical rows.

- `AddressMatcher(canonical_address_filter=<SQL>)` filters the canonical input before
  matching. Narrow rule:
  `NOT regexp_matches(upper(address_concat), 'CAR PARK(ING)? SPACE')`.
- Column caveat: use `address_concat` for a **raw** relation; `original_address_concat`
  for a **prepared canonical folder** (`load_prepared_canonical_data`).
- Keep it narrow — a broad `GARAGE` rule would wrongly drop real homes like
  `FLAT OVER GARAGE`. Notebook 03 demonstrates the narrow rule leaves those alone.
- **Contribution:** ship the narrow filter (+ tests + the raw/prepared column note);
  leave broader garage filtering as a data-backed follow-up.
