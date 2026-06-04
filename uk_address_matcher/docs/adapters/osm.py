"""OpenStreetMap -> uk_address_matcher canonical-source adapter.

DESIGN NOTES, NOT VERIFIED END-TO-END. Unlike gias.py (which runs against a bundled
fixture), fetching OSM requires a network extract, so `fetch()` raises. The
`to_canonical` projection is real and reusable once you have OSM rows in DuckDB.

Source: Overpass API or a Geofabrik/planet extract, filtered to objects with addr:* tags.
Licence: ODbL 1.0 (share-alike + attribution) -- the strictest of the four sources here.
         Anything you derive and publish inherits ODbL obligations. Flag prominently.

Native tags: addr:housenumber, addr:unit, addr:street, addr:city, addr:postcode.
unique_id: OSM type+id ("node/123456", "way/789"). Stable per object, but objects are
           edited/deleted by the community -> ids churn. Record the extract date.

Coverage gaps (why this is harder than GIAS):
  - Sparse, inconsistent tagging. Many objects have a street but no housenumber/postcode.
  - Missing postcodes weaken postcode blocking; the matcher leans on the inverted index
    and the outside-postcode Splink block instead (SplinkStage(include_outside_postcode_block=True)).
  - POI names mixed in -> token vocabulary differs from AddressBase. RE-DERIVE term
    frequencies from the OSM-UK corpus (pass term_frequency_lookup), don't reuse pre-baked.
"""

from __future__ import annotations

import duckdb


def to_canonical(raw: duckdb.DuckDBPyRelation, con: duckdb.DuckDBPyConnection):
    """Project OSM addr:* rows onto the canonical contract.

    Assumes `raw` has columns: osm_type, osm_id, housenumber, unit, street, city, postcode
    (i.e. you've already flattened the addr:* tags). concat_ws drops NULLs, which is the
    whole point given OSM's patchy tagging.
    """
    return con.sql(
        f"""
        SELECT
            osm_type || '/' || CAST(osm_id AS VARCHAR)                   AS unique_id,
            concat_ws(' ', housenumber, unit, street, city)             AS address_concat,
            postcode                                                     AS postcode
        FROM ({raw.sql_query()}) osm
        WHERE street IS NOT NULL          -- skip objects with no usable address text
        """
    )


def fetch(*_args, **_kwargs):
    raise NotImplementedError(
        "OSM fetch is environment-specific (Overpass query or planet/Geofabrik extract). "
        "Flatten addr:* tags into columns, then call to_canonical(). See module docstring."
    )
