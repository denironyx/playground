"""Overture Maps -> uk_address_matcher canonical-source adapter.

DESIGN NOTES, NOT VERIFIED END-TO-END. `fetch()` raises (the data lives as GeoParquet
on cloud storage). The `to_canonical` projection is real.

Source: Overture `addresses` theme (and optionally `places` for POIs), distributed as
        GeoParquet on AWS/Azure. Filter to the UK by bounding box or admin boundary.
Licence: CDLA-Permissive 2.0 for most themes; some upstream content is ODbL. Attribution
         required. Confirm the current per-theme licence before redistributing.

unique_id: GERS id -- globally unique and designed to be STABLE across releases. This is
           the best id-stability story of the four sources; lean on it.

Native (addresses theme): `number`, `street`, address levels (locality/region), `postcode`
  (field names vary by release -- check the schema for the release you pull).

Notes:
  - Global schema -> you must extract the UK subset first (bbox or country = 'GB').
  - The `places` theme mixes POIs (non-dwelling). If you ingest places, expect car-park /
    POI style records in the pool -> apply the parking/non-dwelling filter
    (see ../../scripts/parking_filter.py and EXTENDING §"dwelling vs non-dwelling").
  - Large corpus -> RE-DERIVE term frequencies from the UK subset.
"""

from __future__ import annotations

import duckdb


def to_canonical(raw: duckdb.DuckDBPyRelation, con: duckdb.DuckDBPyConnection):
    """Project Overture addresses-theme rows onto the canonical contract.

    Assumes `raw` has columns: gers_id, number, street, locality, postcode
    (rename from the release's actual field names when you flatten the parquet).
    """
    return con.sql(
        f"""
        SELECT
            CAST(gers_id AS VARCHAR)                          AS unique_id,
            concat_ws(' ', number, street, locality)         AS address_concat,
            postcode                                          AS postcode
        FROM ({raw.sql_query()}) ovt
        WHERE gers_id IS NOT NULL
        """
    )


def fetch(*_args, **_kwargs):
    raise NotImplementedError(
        "Overture data is GeoParquet on cloud storage. Pull the UK subset of the "
        "addresses theme (DuckDB can read remote parquet), flatten fields, then "
        "call to_canonical(). See module docstring."
    )
