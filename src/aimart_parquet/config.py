"""Runtime settings, read from ``AIMART_*`` environment variables.

The server never needs a private key: it only names the addresses that receive payment. A
leaked environment can therefore redirect future revenue but can never move funds already
received — which is why there is no ``*_PRIVATE_KEY`` setting here, and there must never be.
"""

from __future__ import annotations

import re
import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_PRICE = re.compile(r"^\$\d+(\.\d{1,6})?$")


class NetworkMode(StrEnum):
    TESTNET = "testnet"
    MAINNET = "mainnet"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIMART_", env_file=".env", extra="ignore")

    # ---------------------------------------------------------------- service identity
    service_name: str = "AImart Parquet"
    public_base_url: str = Field(
        default="http://localhost:8000",
        description="Externally reachable origin, used in x402 resource URLs and download links.",
    )

    # ---------------------------------------------------------------- payments
    network_mode: NetworkMode = NetworkMode.TESTNET
    pay_to_evm: str | None = Field(
        default=None, description="Address on Base that receives USDC. Receive-only."
    )
    pay_to_svm: str | None = Field(
        default=None, description="Address on Solana that receives USDC. Receive-only."
    )
    facilitator_urls: str | None = Field(
        default=None,
        description=(
            "Comma-separated facilitator URLs, in priority order. Each (network, scheme) is served "
            "by the first facilitator that supports it. Default depends on network_mode."
        ),
    )
    payments_enabled: bool = True
    # Floors are set by facilitator economics, not taste: Dexter rejects payments under about
    # $0.0013 on Solana mainnet, and fee-charging facilitators cost $0.001-0.002 per settlement.
    # Below $0.002 a call can be unsettleable or settle at a loss. Compare fee to margin.
    price_convert: str = "$0.02"  # buyer panel: every persona would pay $0.03-$0.10
    price_inspect: str = "$0.002"
    price_preview: str = "$0.002"
    price_query: str = "$0.005"
    price_quantum_compress: str = "$0.50"
    price_quantum_portfolio: str = "$1.00"
    bazaar_tag: str | None = Field(
        default=None, description="Optional discovery tag placed in accepts[].extra.tag."
    )

    # ---------------------------------------------------------------- limits
    max_input_bytes: int = 50 * 1024 * 1024
    max_decompressed_bytes: int = 512 * 1024 * 1024
    max_json_repair_bytes: int = 5 * 1024 * 1024
    max_preview_rows: int = 500
    max_query_rows: int = 1000
    max_cell_chars: int = 2000
    convert_timeout_s: float = 120.0
    query_timeout_s: float = 15.0
    fetch_timeout_s: float = 60.0
    max_concurrent_jobs: int = 2
    isolate_jobs: bool = Field(
        default=True,
        description="Run DuckDB-heavy jobs in a child process (crash isolation, hard deadline).",
    )
    queue_wait_s: float = 10.0
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = 2
    rate_limit_per_minute: int = 120
    trust_forwarded_for: bool = Field(
        default=False,
        description=(
            "Behind a proxy (Cloud Run): read the client address from X-Forwarded-For, counting "
            "trusted_proxy_hops entries from the right. Off: use the TCP peer."
        ),
    )
    trusted_proxy_hops: int = Field(
        default=1,
        ge=1,
        le=5,
        description=(
            "Proxies that append to X-Forwarded-For (Cloud Run's front end: 1; behind an extra "
            "HTTPS load balancer: 2). The client address is read this many entries from the right."
        ),
    )
    allow_private_urls: bool = Field(
        default=False, description="Test-only escape hatch for the SSRF guard. Never in prod."
    )

    # ---------------------------------------------------------------- artifacts
    artifact_dir: Path = Path("/tmp/aimart-artifacts")  # noqa: S108 - ephemeral by design
    artifact_ttl_s: int = 3600
    artifact_max_total_bytes: int = Field(
        default=512 * 1024 * 1024,
        description="Local artifact budget. On Cloud Run the disk is RAM: keep this small.",
    )
    gcs_bucket: str | None = Field(
        default=None, description="Store artifacts in this GCS bucket (needs the gcs extra)."
    )
    signing_secret: SecretStr = Field(
        default_factory=lambda: SecretStr(secrets.token_urlsafe(32)),
        description="HMAC key for download links. Random per boot unless set.",
    )

    # ---------------------------------------------------------------- quantum + provenance
    enable_quantum: bool = Field(
        default=False,
        description=(
            "Serve the premium quantum tools (needs the quantum extra). Off by default: a buyer "
            "panel valued them at $0 and said they made the core product look like hype."
        ),
    )
    quantum_timeout_s: float = 90.0
    signing_key_mldsa: SecretStr | None = Field(
        default=None,
        description="Base64 of a 32-byte ML-DSA-65 seed. Unset: ephemeral key per boot (dev only).",
    )
    hosted_url: str | None = Field(
        default=None,
        description="Local mode: URL of the hosted service, mentioned once in results for premium tools.",
    )

    admin_token: SecretStr | None = Field(
        default=None,
        description="Bearer token for GET /v1/demand (demand signals). Unset: the endpoint is off.",
    )

    # ---------------------------------------------------------------- ops
    log_level: str = "INFO"
    log_json: bool = True

    @model_validator(mode="after")
    def _validate(self) -> Self:
        self.public_base_url = self.public_base_url.rstrip("/")
        if self.pay_to_evm and not _EVM_ADDRESS.match(self.pay_to_evm):
            msg = "AIMART_PAY_TO_EVM must be a 0x-prefixed 40-hex-character address"
            raise ValueError(msg)
        if self.pay_to_svm and not _SOLANA_ADDRESS.match(self.pay_to_svm):
            msg = "AIMART_PAY_TO_SVM must be a base58 Solana address"
            raise ValueError(msg)
        for name in (
            "price_convert",
            "price_inspect",
            "price_preview",
            "price_query",
            "price_quantum_compress",
            "price_quantum_portfolio",
        ):
            if not _PRICE.match(getattr(self, name)):
                msg = f"AIMART_{name.upper()} must look like '$0.01'"
                raise ValueError(msg)
        # A missing payee is checked by the HTTP app factory, not here: the local stdio MCP mode
        # never takes payment and must start with no environment at all.
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
