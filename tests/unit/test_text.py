"""Text decoding: valid UTF-8 passes through untouched; everything else becomes UTF-8."""

from __future__ import annotations

import pytest

import aimart_testkit as kit
from aimart_parquet.engine import text
from aimart_parquet.engine.text import to_utf8
from aimart_parquet.errors import InvalidRequest


def test_valid_utf8_is_returned_as_is(tmp_path) -> None:
    src = kit.write(tmp_path / "in.csv", "name\nJosé\n北京\n".encode())
    info = to_utf8(src, tmp_path)
    assert info.path == src
    assert info.encoding == "utf-8"
    assert info.replaced_chars == 0


def test_utf8_is_validated_across_the_whole_file(tmp_path, monkeypatch) -> None:
    """ASCII for the first chunk and Latin-1 later must not be passed through as UTF-8."""
    monkeypatch.setattr(text, "_CHUNK", 8)
    src = kit.write(tmp_path / "in.csv", b"a,b\n1,2\n" * 4 + "x,Jos\xe9\n".encode("latin-1"))
    info = to_utf8(src, tmp_path)
    assert info.path != src
    assert info.encoding != "utf-8"
    assert info.path.read_text(encoding="utf-8").startswith("a,b\n1,2\n")


def test_utf8_split_across_chunks_is_still_valid(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(text, "_CHUNK", 3)
    src = kit.write(tmp_path / "in.csv", "€€€€".encode())  # 3-byte chars straddle chunk edges
    assert to_utf8(src, tmp_path).path == src


@pytest.mark.parametrize("bom", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_with_bom_is_transcoded(tmp_path, bom: str) -> None:
    content = "a,b\n1,2\n"
    data = content.encode(bom) if bom == "utf-16" else (("﻿" + content).encode(bom))
    src = kit.write(tmp_path / "in.csv", data)
    info = to_utf8(src, tmp_path)
    assert info.encoding.startswith("utf-16")
    assert info.path.read_text(encoding="utf-8").lstrip("﻿") == content


def test_forced_encoding_is_used(tmp_path) -> None:
    src = kit.write(tmp_path / "in.csv", "Nombre\nNiño\nEspaña\n".encode("latin-1"))
    info = to_utf8(src, tmp_path, "latin-1")
    assert info.encoding == "iso8859-1"
    assert info.path.read_text(encoding="utf-8") == "Nombre\nNiño\nEspaña\n"
    assert info.replaced_chars == 0


def test_forced_utf8_alias_skips_transcoding(tmp_path) -> None:
    src = kit.write(tmp_path / "in.csv", b"a\n1\n")
    info = to_utf8(src, tmp_path, "UTF8")
    assert info.path == src
    assert info.encoding == "utf-8"


def test_unknown_forced_encoding_is_a_400(tmp_path) -> None:
    src = kit.write(tmp_path / "in.csv", b"a\n")
    with pytest.raises(InvalidRequest) as info:
        to_utf8(src, tmp_path, "klingon-8")
    assert info.value.http_status == 400
    assert info.value.hint is not None
    assert "latin-1" in info.value.hint


def test_undecodable_bytes_are_counted(tmp_path) -> None:
    # 0x81 is undefined in cp1252: forcing cp1252 must count it, not hide it.
    src = kit.write(tmp_path / "in.csv", b"a\nx\x81y\n\x81\n")
    info = to_utf8(src, tmp_path, "cp1252")
    assert info.replaced_chars == 2
    assert info.path.read_text(encoding="utf-8") == "a\nx�y\n�\n"


def test_detection_failure_falls_back_to_cp1252(tmp_path, monkeypatch) -> None:
    class NoGuess:
        @staticmethod
        def best() -> None:
            return None

    monkeypatch.setattr(text, "from_bytes", lambda _sample: NoGuess())
    src = kit.write(tmp_path / "in.csv", b"caf\xe9\n")
    info = to_utf8(src, tmp_path)
    assert info.encoding == "cp1252"
    assert info.path.read_text(encoding="utf-8") == "café\n"


# These are the files the module docstring promises to handle: Excel's Windows-1252 exports
# and Latin-1 from legacy systems. Small files are the common case for agents.
_LEGACY = [
    pytest.param("Name,Price\nA,€5\nB,€6\n", "cp1252", id="cp1252-euro"),
    pytest.param("Nombre,País\nNiño,España\nPeña,México\n", "latin-1", id="latin1-spanish"),
    pytest.param(
        "Name,Price,Note\n" + "".join(f"Item {i},€{i},Café crème naïve\n" for i in range(200)),
        "cp1252",
        id="cp1252-french-200-rows",
    ),
]


@pytest.mark.parametrize(("content", "encoding"), _LEGACY)
def test_legacy_western_encodings_round_trip_exactly(tmp_path, content, encoding) -> None:
    src = kit.write(tmp_path / "in.csv", content.encode(encoding))
    info = to_utf8(src, tmp_path)
    assert info.path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize(
    ("content", "encoding"),
    [
        ("Name,Stadt\nJürgen,München\nGroß,Köln\n", "latin-1"),
        ("Nom,Ville\nJosé,Montréal\nZoë,Köln\n", "latin-1"),
    ],
)
def test_legacy_encodings_that_detection_gets_right(tmp_path, content, encoding) -> None:
    src = kit.write(tmp_path / "in.csv", content.encode(encoding))
    info = to_utf8(src, tmp_path)
    assert info.path.read_text(encoding="utf-8") == content
    assert info.replaced_chars == 0
