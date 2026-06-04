"""Companies House -> uk_address_matcher adapter.

DESIGN NOTES, NOT VERIFIED END-TO-END. `fetch()` raises (you download the monthly
snapshot). The `to_canonical` projection is real.

Source: Companies House "Free Company Data Product" (monthly CSV snapshot of all
        registered companies), or the streaming/REST API.
Licence: Open Government Licence (OGL). Attribution required.

unique_id: CompanyNumber -- very stable.

Native (snapshot CSV): RegAddress.AddressLine1, RegAddress.AddressLine2,
  RegAddress.PostTown, RegAddress.County, RegAddress.PostCode, CompanyName.

CANONICAL vs MESSY -- read this before using it:
  - As MESSY (recommended primary role): you have CH registered-office addresses and
    want to geocode them to AddressBase/OSM. Natural fit -- CH is the dirty side.
  - As CANONICAL: it's a registered-office gazetteer, not a dwelling gazetteer. Many
    companies share one accountant's/formation-agent's address, so you get heavy
    many-to-one collisions. The matcher will (correctly) report low distinguishability
    on those. Useful for "is this address a registered office?" lookups, not for
    resolving to a unique premises.

Notes:
  - Org names skew the token vocabulary (LTD, LIMITED, HOLDINGS, &). If used as canonical,
    RE-DERIVE term frequencies. The single-token abbrev map already expands & -> AND.
  - Registered offices are non-dwelling. If mixing with a dwelling source, the
    dwelling-vs-non-dwelling caveat applies (EXTENDING §).
"""

from __future__ import annotations

import duckdb


def to_canonical(raw: duckdb.DuckDBPyRelation, con: duckdb.DuckDBPyConnection):
    """Project the CH snapshot onto the canonical contract.

    Assumes the snapshot columns have been renamed to: company_number, company_name,
    address_line_1, address_line_2, post_town, county, postcode.
    """
    return con.sql(
        f"""
        SELECT
            CAST(company_number AS VARCHAR)                                          AS unique_id,
            concat_ws(' ', address_line_1, address_line_2, post_town, county)        AS address_concat,
            postcode                                                                 AS postcode
        FROM ({raw.sql_query()}) ch
        WHERE company_number IS NOT NULL
        """
    )


def fetch(*_args, **_kwargs):
    raise NotImplementedError(
        "Download the Companies House Free Company Data snapshot CSV, rename the "
        "RegAddress.* columns, then call to_canonical(). See module docstring."
    )
