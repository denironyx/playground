"""Verify both packages import from the LOCAL editable clones (not PyPI) and the stack runs.

Pass criteria:
  * splink/ukam '... from' paths point into ...\\oss\\splink and ...\\oss\\uk_address_matcher
    (NOT into .venv\\Lib\\site-packages\\)
  * sqlglot reports 26.6.0
  * AddressMatcher resolves
Run:  uv run python experiments\\smoke_test.py
"""

import importlib.util
import sys

import duckdb
import sqlglot

import splink
import uk_address_matcher
from uk_address_matcher import AddressMatcher  # imports splink internally


def origin(mod: str) -> str:
    spec = importlib.util.find_spec(mod)
    return spec.origin if spec else "<not found>"


print("python      :", sys.version.split()[0])
print("duckdb      :", duckdb.__version__)
print("sqlglot     :", sqlglot.__version__)              # expect 26.6.0
print("splink ver  :", splink.__version__)
print("splink from :", origin("splink"))                 # expect ...\\oss\\splink\\splink\\__init__.py
print("ukam ver    :", uk_address_matcher.__version__)
print("ukam from   :", origin("uk_address_matcher"))     # expect ...\\oss\\uk_address_matcher\\...
print("AddressMatcher OK:", AddressMatcher is not None)
