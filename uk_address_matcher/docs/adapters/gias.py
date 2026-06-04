"""GIAS (Get Information About Schools) -> uk_address_matcher canonical-source adapter.

A WORKING, TESTED example of the canonical-source contract (see
../EXTENDING-TO-NEW-CANONICAL-SOURCES.md). It turns an external dataset into the
three columns the matcher needs -- (unique_id, address_concat, postcode) -- and builds
a prepared canonical folder via the library's own `prepare_canonical_folder`. No
library change required.

Why GIAS is the worked example: it's small (~50k establishments), clean, has stable
official ids (URN), real UK postcodes, and an open licence (OGL). The pre-baked UK
token frequencies apply well, so no TF retraining is needed.

Data: https://get-information-schools.service.gov.uk/Downloads  (OGL v3.0)
The establishment CSV has ~140 columns; we read 6.

Run the self-test on the bundled fixture:
    uv run python uk_address_matcher/docs/adapters/gias.py
"""

from __future__ import annotations

from pathlib import Path

import duckdb

# The native GIAS columns this adapter consumes. Kept explicit so a schema change
# upstream fails loudly here rather than silently producing blank addresses.
GIAS_SOURCE_COLUMNS = ["URN", "EstablishmentName", "Street", "Locality", "Town", "Postcode"]

FIXTURE = Path(__file__).parent / "fixtures" / "gias_sample.csv"


def to_canonical(raw: duckdb.DuckDBPyRelation, con: duckdb.DuckDBPyConnection):
    """Project native GIAS rows onto the canonical contract.

    - unique_id      <- URN (official, stable)
    - address_concat <- name + street + locality + town (postcode held separately)
    - postcode       <- Postcode

    concat_ws skips NULLs, so a missing Locality doesn't leave a double space.
    """
    return con.sql(
        f"""
        SELECT
            CAST(URN AS VARCHAR)                                           AS unique_id,
            concat_ws(' ', EstablishmentName, Street, Locality, Town)      AS address_concat,
            Postcode                                                       AS postcode
        FROM ({raw.sql_query()}) gias
        WHERE URN IS NOT NULL
        """
    )


def build(
    raw: duckdb.DuckDBPyRelation,
    output_folder: str | Path,
    con: duckdb.DuckDBPyConnection,
    *,
    overwrite: bool = True,
) -> Path:
    """Normalize GIAS -> canonical contract, then build the prepared canonical folder."""
    from uk_address_matcher import prepare_canonical_folder

    canonical = to_canonical(raw, con)
    prepare_canonical_folder(canonical, output_folder, con=con, overwrite=overwrite)
    return Path(output_folder)


def _demo() -> None:
    import tempfile

    from uk_address_matcher import AddressMatcher

    con = duckdb.connect(":memory:")
    raw = con.read_csv(str(FIXTURE))
    print(f"loaded {raw.aggregate('count(*) n').fetchone()[0]} GIAS rows from fixture")

    canonical = to_canonical(raw, con)
    print("\ncanonical contract (unique_id, address_concat, postcode):")
    for row in canonical.fetchall():
        print("  ", row)

    with tempfile.TemporaryDirectory() as tmp:
        folder = build(raw, Path(tmp) / "prepared_gias", con=con)

        # A messy query that should resolve to the RC primary (URN 100002).
        messy = con.sql(
            "SELECT * FROM (VALUES "
            "('q1','St Marys Roman Catholic Primary, Church Lane, Springfield','SP1 3CD')) "
            "t(unique_id, address_concat, postcode)"
        )
        result = AddressMatcher(
            canonical_addresses=str(folder), addresses_to_match=messy, con=con
        ).match()
        print("\nmatch result:")
        for row in result.matches().select(
            "unique_id, resolved_canonical_id, original_address_concat_canonical, round(match_weight,2) AS mw"
        ).fetchall():
            print("  ", row)


if __name__ == "__main__":
    _demo()
