"""Settings defaults that are safety properties, not preferences."""

from __future__ import annotations

import pytest

from aimart_parquet.config import NetworkMode, Settings, get_settings


def test_safe_defaults_without_any_environment() -> None:
    s = Settings(_env_file=None)
    assert s.network_mode is NetworkMode.TESTNET  # mainnet only by explicit choice
    assert s.allow_private_urls is False  # the SSRF guard is on
    assert s.trust_forwarded_for is False
    assert s.payments_enabled is True
    assert s.isolate_jobs is True
    assert s.pay_to_evm is None
    assert s.pay_to_svm is None
    assert s.signing_key_mldsa is None
    assert (s.price_convert, s.price_inspect, s.price_preview, s.price_query) == (
        "$0.01",
        "$0.002",
        "$0.002",
        "$0.005",
    )
    assert (s.price_quantum_compress, s.price_quantum_portfolio) == ("$0.50", "$1.00")
    assert not any("private_key" in name for name in Settings.model_fields)  # receive-only


def test_signing_secret_is_random_per_boot() -> None:
    a = Settings(_env_file=None).signing_secret.get_secret_value()
    b = Settings(_env_file=None).signing_secret.get_secret_value()
    assert a != b
    assert len(a) >= 32


def test_environment_is_read_with_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIMART_PRICE_QUERY", "$0.009")
    monkeypatch.setenv("AIMART_NETWORK_MODE", "mainnet")
    get_settings.cache_clear()
    s = get_settings()
    assert s.price_query == "$0.009"
    assert s.network_mode is NetworkMode.MAINNET
    assert get_settings() is s  # cached
    get_settings.cache_clear()


def test_invalid_environment_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIMART_PAY_TO_EVM", "0xnotanaddress")
    with pytest.raises(ValueError, match="AIMART_PAY_TO_EVM"):
        Settings(_env_file=None)


def test_signing_key_must_be_a_32_byte_seed() -> None:
    import base64

    from aimart_parquet.provenance import Signer

    with pytest.raises(ValueError, match="32-byte seed"):
        Signer.from_b64(base64.b64encode(b"short").decode())
    with pytest.raises(ValueError, match="32-byte seed"):
        Signer.from_b64("!!!not base64!!!")
    assert Signer.from_b64(base64.b64encode(b"\x01" * 32).decode()).ephemeral is False
