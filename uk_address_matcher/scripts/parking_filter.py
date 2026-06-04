"""Narrow parking-space canonical filter for uk_address_matcher.

Implements the issue's proposed first step: exclude *obvious* parking-space canonical
records (``CAR PARK SPACE`` / ``CAR PARKING SPACE``) from the matching pool, so they
can't win against real dwellings. Deliberately narrow — broader ``GARAGE`` filtering is
left out because it would wrongly drop real homes like ``FLAT OVER GARAGE``.

Use as a filter expression with AddressMatcher (raw canonical relation)::

    from parking_filter import parking_filter_sql
    AddressMatcher(..., canonical_address_filter=parking_filter_sql("address_concat"))

For a *prepared canonical folder*, the filter runs against the prepared columns, so
pass the prepared text column name instead::

    canonical_address_filter=parking_filter_sql("original_address_concat")

Run directly for a demo on tiny data:

    uv run python uk_address_matcher/scripts/parking_filter.py
"""

from __future__ import annotations

import duckdb

# Matches "CAR PARK SPACE" and "CAR PARKING SPACE", case-insensitively.
_PARKING_REGEX = "CAR PARK(ING)? SPACE"


def parking_filter_sql(column: str = "address_concat") -> str:
    """Return a DuckDB boolean SQL expression that is TRUE for non-parking rows.

    Suitable for ``AddressMatcher(canonical_address_filter=...)`` or any
    ``relation.filter(...)`` call. ``column`` is the address-text column to test
    (``address_concat`` for raw input, ``original_address_concat`` for prepared
    canonical folders).
    """
    return f"NOT regexp_matches(upper({column}), '{_PARKING_REGEX}')"


def is_parking_sql(column: str = "address_concat") -> str:
    """Return the positive form: TRUE for obvious parking-space rows."""
    return f"regexp_matches(upper({column}), '{_PARKING_REGEX}')"


def count_parking_records(
    relation: duckdb.DuckDBPyRelation, column: str = "address_concat"
) -> int:
    """Count rows in ``relation`` that look like obvious parking-space records."""
    return relation.filter(is_parking_sql(column)).aggregate("count(*) AS n").fetchone()[0]


_DEMO_CANONICAL = """
SELECT * FROM (VALUES
  ('c_flat5b',  'FLAT 5 BASEMENT 447 EXAMPLE ROAD LONDON'),
  ('c_carpark', 'CAR PARK SPACE 5 EXAMPLE COURT 447 EXAMPLE ROAD LONDON'),
  ('c_carpark2','CAR PARKING SPACE 12 EXAMPLE COURT LONDON'),
  ('c_garage1', 'FLAT OVER GARAGE 8 EXAMPLE ROAD LONDON'),
  ('c_garage2', 'FIRST FLAT 1 EXAMPLE GARAGE YARD SUMMER LANE LONDON')
) t(unique_id, address_concat)
"""


def _demo() -> None:
    con = duckdb.connect(":memory:")
    canon = con.sql(_DEMO_CANONICAL)

    print("Filter expression:")
    print("  ", parking_filter_sql())
    print()

    n_parking = count_parking_records(canon)
    print(f"parking-style rows: {n_parking} of {canon.aggregate('count(*) n').fetchone()[0]}")

    print("\nremoved (obvious parking):")
    for uid, addr in canon.filter(is_parking_sql()).select("unique_id, address_concat").fetchall():
        print(f"  - {uid}: {addr}")

    print("\nkept (incl. real 'garage' dwellings — proves the rule is narrow):")
    for uid, addr in canon.filter(parking_filter_sql()).select("unique_id, address_concat").fetchall():
        print(f"  + {uid}: {addr}")

    print(
        "\nOn real OS AddressBase/NGD this rule reportedly matches ~16,659 canonical rows"
        " nationally (52 in Hackney) — see the GitHub issue."
    )


if __name__ == "__main__":
    _demo()
