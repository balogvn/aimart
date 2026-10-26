"""AImart Parquet: messy data in, clean Parquet out — paid per call by AI agents via x402."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aimart-parquet")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.1.0"
