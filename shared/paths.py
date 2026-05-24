"""Path helpers so notebooks/scripts in nested folders don't fight relative paths."""

from pathlib import Path

# shared/ lives directly under the playground root.
PLAYGROUND_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PLAYGROUND_ROOT / "data"

# The upstream contribution clones, as siblings of the playground.
OSS_ROOT = PLAYGROUND_ROOT.parent
SPLINK_REPO = OSS_ROOT / "splink"
UKAM_REPO = OSS_ROOT / "uk_address_matcher"
