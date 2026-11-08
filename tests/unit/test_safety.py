"""Decompression guards: every expansion is capped, zips are vetted before inflating."""

from __future__ import annotations

import bz2
import gzip
import io
import zipfile

import pytest

import aimart_testkit as kit
from aimart_parquet.engine import safety
from aimart_parquet.errors import InputTooLarge, InvalidInput

MB = 1024 * 1024


@pytest.mark.parametrize(
    ("magic", "expected"),
    [
        (b"\x1f\x8b\x08\x00", "gzip"),
        (b"BZh91AY&SY", "bz2"),
        (b"BZh1\x17rE8P\x90", "bz2"),  # an empty stream: its end, where a block would be
        (b"PK\x03\x04", "zip"),
        (b"a,b\n", None),
        (b"", None),
        # Until XA1 'BZh' alone was bz2, and this case was b"BZh91AY" (7 bytes): text may begin
        # 'BZh' (a CSV header 'BZh_code' was refused as a corrupt bz2 stream), and a stream is
        # known by its block size and its first block's magic, SNIFF_BYTES in all.
        (b"BZh_code,a", None),
        (b"BZh91AY", None),
        (b"BZh01AY&SY", None),
    ],
)
def test_sniff_compression(magic: bytes, expected: str | None) -> None:
    assert safety.sniff_compression(magic) == expected


def test_read_magic_reads_a_prefix(tmp_path) -> None:
    path = kit.write(tmp_path / "f", b"x" * 1000)
    assert safety.read_magic(path) == b"x" * 600
    assert safety.read_magic(path, 4) == b"xxxx"


class TestCopyCapped:
    def test_copies_up_to_the_cap(self) -> None:
        out = io.BytesIO()
        assert safety.copy_capped(io.BytesIO(b"a" * 100), out, 100) == 100
        assert out.getvalue() == b"a" * 100

    def test_one_byte_over_the_cap_raises(self) -> None:
        with pytest.raises(InputTooLarge) as info:
            safety.copy_capped(io.BytesIO(b"a" * 101), io.BytesIO(), 100)
        assert info.value.details == {"limit_bytes": 100}
        assert info.value.http_status == 413

    def test_stops_reading_at_the_cap(self) -> None:
        """A bomb must be stopped as it inflates, not after: read at most one chunk past the cap."""

        class Endless:
            reads = 0

            def read(self, size: int = -1) -> bytes:
                self.reads += 1
                return b"z" * size

        src = Endless()
        with pytest.raises(InputTooLarge):
            safety.copy_capped(src, io.BytesIO(), 3 * MB)
        assert src.reads <= 4


class TestDecompressStream:
    @pytest.mark.parametrize(("kind", "pack"), [("gzip", gzip.compress), ("bz2", bz2.compress)])
    def test_round_trip(self, tmp_path, kind, pack) -> None:
        src = kit.write(tmp_path / "in", pack(b"hello,world\n" * 10))
        dst = tmp_path / "out"
        assert safety.decompress_stream(src, dst, kind, MB) == 120
        assert dst.read_bytes() == b"hello,world\n" * 10

    def test_gzip_bomb_is_capped(self, tmp_path) -> None:
        src = kit.write(tmp_path / "bomb.gz", gzip.compress(b"\0" * (3 * MB)))
        assert src.stat().st_size < 10_000  # small on the wire, large when inflated
        with pytest.raises(InputTooLarge):
            safety.decompress_stream(src, tmp_path / "out", "gzip", 2 * MB)

    @pytest.mark.parametrize(("kind", "pack"), [("gzip", gzip.compress), ("bz2", bz2.compress)])
    def test_truncated_stream_is_invalid_input(self, tmp_path, kind, pack) -> None:
        src = kit.write(tmp_path / "in", pack(b"x" * 10_000)[:-8])
        with pytest.raises(InvalidInput) as info:
            safety.decompress_stream(src, tmp_path / "out", kind, MB)
        assert kind in info.value.message


class TestGzipChunks:
    """What DuckDB is let read from a gzip: only one member, which Python's gzip module reads
    the same way."""

    @pytest.mark.parametrize(
        "data",
        [
            gzip.compress(b"a,b\n1,2\n"),
            kit.gzip_member(b"a,b\n1,2\n", 4),
            kit.gzip_member(b"a,b\n1,2\n", 4 | 8),
            gzip.compress(b""),
            kit.gzip_member(b"a,b\n1,2\n", 4 | 8, extra=b"e" * 65_535, name=b"n" * 500_000),
        ],
        ids=["one", "fextra", "fextra-fname", "empty", "header-long"],
    )
    def test_yields_what_python_reads(self, tmp_path, data) -> None:
        # Until CS5 this also passed gzips of two or three members, and later headers of up to
        # 32,759 bytes (the 'more-than-one-member' cases below now): DuckDB drops members when
        # its buffer's refill ends the file (test_fix_csv_followup_safety.py).
        path = kit.write(tmp_path / "in.gz", data)
        assert b"".join(safety.gzip_chunks(path, MB)) == gzip.decompress(data)

    def test_a_bomb_inflates_a_chunk_at_a_time(self, tmp_path) -> None:
        path = kit.write(tmp_path / "bomb.gz", gzip.compress(b"\0" * (20 * MB)))
        assert path.stat().st_size < 50_000  # one read of it holds the whole bomb
        assert max(map(len, safety.gzip_chunks(path, 20 * MB))) <= MB

    def test_past_the_cap_is_too_large(self, tmp_path) -> None:
        path = kit.write(tmp_path / "bomb.gz", gzip.compress(b"\0" * (3 * MB)))
        with pytest.raises(InputTooLarge) as info:
            b"".join(safety.gzip_chunks(path, 2 * MB))
        assert info.value.details == {"limit_bytes": 2 * MB}

    @pytest.mark.parametrize(
        "data",
        [
            kit.gzip_member(b"a\n", 1),
            kit.gzip_member(b"a\n", 2),
            kit.gzip_member(b"a\n", 16),
            kit.gzip_member(b"a\n", 32),
            gzip.compress(b"a\n") + b"\0",
            gzip.compress(b"a\n") + b"\0" * 64,
            gzip.compress(b"a\n") + b"junk after it",
            gzip.compress(b"a\n")[:-8] + b"\0\0\0\0" + gzip.compress(b"a\n")[-4:],
            gzip.compress(b"a\n")[:-4] + b"\x09\0\0\0",
            gzip.compress(b"a\n")[:-3],
            gzip.compress(b"a\n" * 1000)[:20],
            gzip.compress(b"a\n")[:12],
            b"\x1f\x8b\x08\x08" + b"\0" * 6 + b"no end to the name" * 70_000,
            b"\x1f\x8b\x08\x00" + b"\0" * 6 + b"not deflate",
            gzip.compress(b"a,b\n") + gzip.compress(b"") + gzip.compress(b"1,2\n"),
            kit.gzip_member(b"a,b\n", 4) + kit.gzip_member(b"1,2\n", 8),
            kit.gzip_member(b"a,b\n", 4 | 8, extra=b"e" * 65_535, name=b"n" * 500_000)
            + gzip.compress(b"1,2\n"),
            gzip.compress(b"a\n") + kit.gzip_member(b"b\n", 8, name=b"n" * 32_748),
            gzip.compress(b"a\n") + kit.gzip_member(b"b\n", 4, extra=b"e" * 32_747),
            gzip.compress(b"a\n") + kit.gzip_member(b"b\n", 8, name=b"n" * 32_749),
            gzip.compress(b"a\n") + kit.gzip_member(b"b\n", 4, extra=b"e" * 32_748),
            gzip.compress(b"a\n")
            + kit.gzip_member(b"b\n", 4 | 8, extra=b"e" * 20_000, name=b"n" * 12_747),
            gzip.compress(b"a\n") * 2 + kit.gzip_member(b"b\n", 8, name=b"n" * 40_000),
        ],
        ids=[
            "ftext",
            "fhcrc",
            "fcomment",
            "reserved",
            "zero-byte",
            "zero-padding",
            "trailing",
            "crc",
            "length",
            "trailer-cut",
            "body-cut",
            "header-only",
            "endless-name",
            "not-deflate",
            "three-members",
            "two-members-fextra-fname",
            "first-header-long-two-members",
            "later-name-at-limit",
            "later-extra-at-limit",
            "later-name-past-limit",
            "later-extra-past-limit",
            "later-fields-past-limit",
            "third-name-past-limit",
        ],
    )
    def test_anything_duckdb_might_read_otherwise_is_a_mismatch(self, tmp_path, data) -> None:
        # Python reads the first four and skips the padding, which DuckDB refuses; DuckDB reads
        # the next eight, which Python refuses, as whatever inflates. Python reads the rest,
        # gzips of more than one member, and DuckDB may stop before their end (no error, rows
        # lost: CS5, test_fix_csv_followup_safety.py) or refuse a later member's header of
        # 32,760 bytes or more (InternalException; a 422 where d83c738 converted). The first
        # three and the two 'at-limit' cases passed this check until CS5.
        path = kit.write(tmp_path / "in.gz", data)
        with pytest.raises(safety.GzipMismatch):
            b"".join(safety.gzip_chunks(path, MB))


def test_gzip_head(tmp_path) -> None:
    path = kit.write(tmp_path / "in.gz", gzip.compress(b"x" * 5000))
    assert safety.gzip_head(path, 1024) == b"x" * 1024
    short = kit.write(tmp_path / "short.gz", gzip.compress(b"a,b\n"))
    assert safety.gzip_head(short, 1024) == b"a,b\n"
    cut = kit.write(tmp_path / "cut.gz", gzip.compress(b"a,b\n")[:12])
    assert safety.gzip_head(cut, 1024) is None  # decompress_stream says what is wrong


class TestCheckZip:
    def test_a_normal_zip_passes(self, tmp_path) -> None:
        path = kit.write(tmp_path / "a.zip", kit.zip_bytes({"a.csv": b"a,b\n1,2\n"}))
        infos = safety.check_zip(path, MB)
        assert [i.filename for i in infos] == ["a.csv"]

    def test_not_a_zip(self, tmp_path) -> None:
        path = kit.write(tmp_path / "a.zip", b"PK\x03\x04 garbage")
        with pytest.raises(InvalidInput, match="Not a readable zip"):
            safety.check_zip(path, MB)

    def test_declared_size_over_cap_is_rejected_before_inflating(self, tmp_path) -> None:
        path = kit.write(tmp_path / "a.zip", kit.zip_bytes({"a.csv": b"0" * (2 * MB)}))
        with pytest.raises(InputTooLarge) as info:
            safety.check_zip(path, MB)
        # expanded_bytes since the workbook check names the size in its own message.
        assert info.value.details == {"limit_bytes": MB, "expanded_bytes": 2 * MB}

    def test_high_ratio_zip_bomb_is_rejected(self, tmp_path) -> None:
        bomb = kit.zip_bytes({"bomb.csv": b"0" * (30 * MB)})
        assert len(bomb) * safety.MAX_ZIP_RATIO < 30 * MB  # the ratio is what gives it away
        path = kit.write(tmp_path / "bomb.zip", bomb)
        with pytest.raises(InvalidInput, match="zip bomb"):
            safety.check_zip(path, 512 * MB)

    def test_high_ratio_but_small_total_is_allowed(self, tmp_path) -> None:
        """Very repetitive small files compress absurdly well; only big expansions are bombs."""
        path = kit.write(tmp_path / "small.zip", kit.zip_bytes({"a.csv": b"0" * (5 * MB)}))
        assert safety.check_zip(path, 512 * MB)

    def test_member_count_limit(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(safety, "MAX_ZIP_MEMBERS", 3)
        path = kit.write(
            tmp_path / "many.zip", kit.zip_bytes({f"{i}.csv": b"a\n" for i in range(4)})
        )
        with pytest.raises(InvalidInput, match="4 members"):
            safety.check_zip(path, MB)


class TestDataMembers:
    def test_ignores_directories_and_mac_metadata(self, tmp_path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("folder/", b"")
            zf.writestr("__MACOSX/._data.csv", b"junk")
            zf.writestr("folder/._data.csv", b"junk")
            zf.writestr("._top.csv", b"junk")
            zf.writestr(".DS_Store", b"junk")
            zf.writestr("folder/data.csv", b"a\n1\n")
        path = kit.write(tmp_path / "a.zip", buf.getvalue())
        members = safety.data_members(safety.check_zip(path, MB))
        assert [m.filename for m in members] == ["folder/data.csv"]


class TestExtractSingleMember:
    def test_extracts_to_a_fixed_name_inside_workdir(self, tmp_path) -> None:
        archive = kit.write(
            tmp_path / "a.zip", kit.zip_bytes({"../../etc/evil.csv.gz": b"payload"})
        )
        workdir = tmp_path / "work"
        workdir.mkdir()
        dst, name = safety.extract_single_member(archive, workdir, MB)
        assert name == "evil.csv.gz"
        assert dst.parent == workdir  # the member path never decides where bytes land
        assert dst.name == "unzipped.csv.gz"
        assert dst.read_bytes() == b"payload"

    def test_more_than_one_data_file_is_rejected(self, tmp_path) -> None:
        archive = kit.write(tmp_path / "a.zip", kit.zip_bytes({"a.csv": b"1", "b.csv": b"2"}))
        with pytest.raises(InvalidInput) as info:
            safety.extract_single_member(archive, tmp_path, MB)
        assert info.value.details == {"members": ["a.csv", "b.csv"]}

    def test_empty_archive_is_rejected(self, tmp_path) -> None:
        archive = kit.write(tmp_path / "a.zip", kit.zip_bytes({"__MACOSX/._x": b"1"}))
        with pytest.raises(InvalidInput, match="found 0"):
            safety.extract_single_member(archive, tmp_path, MB)

    def test_inflation_is_capped_even_if_the_directory_check_is_fooled(
        self, tmp_path, monkeypatch
    ) -> None:
        """Defence in depth: the streaming cap holds on its own, whatever the header claims."""
        archive = kit.write(tmp_path / "a.zip", kit.zip_bytes({"a.csv": b"0" * (3 * MB)}))
        real_check = safety.check_zip
        monkeypatch.setattr(safety, "check_zip", lambda path, _cap: real_check(path, 10**12))
        workdir = tmp_path / "work"
        workdir.mkdir()
        with pytest.raises(InputTooLarge):
            safety.extract_single_member(archive, workdir, 2 * MB)


class TestEnsureSize:
    def test_at_limit_is_fine(self, tmp_path) -> None:
        path = kit.write(tmp_path / "f", b"x" * 10)
        assert safety.ensure_size(path, 10) == 10

    def test_over_limit(self, tmp_path) -> None:
        path = kit.write(tmp_path / "f", b"x" * 11)
        with pytest.raises(InputTooLarge) as info:
            safety.ensure_size(path, 10)
        assert info.value.details == {"limit_bytes": 10}


def test_copy_file(tmp_path) -> None:
    # Still the engine's copy of a local caller's file, which is never linked (convert._link).
    src = kit.write(tmp_path / "a", b"abc")
    safety.copy_file(src, tmp_path / "b")
    assert (tmp_path / "b").read_bytes() == b"abc"


def test_link_or_copy_links(tmp_path) -> None:
    src = kit.write(tmp_path / "a", b"abc")
    safety.link_or_copy(src, tmp_path / "b")
    assert (tmp_path / "b").stat().st_ino == src.stat().st_ino  # no second copy of the bytes


def test_link_or_copy_copies_across_filesystems(tmp_path, monkeypatch) -> None:
    def cross_device(*_: object, **__: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(safety.os, "link", cross_device)
    src = kit.write(tmp_path / "a", b"abc")
    safety.link_or_copy(src, tmp_path / "b")
    assert (tmp_path / "b").read_bytes() == b"abc"


def test_link_or_copy_never_replaces_a_file(tmp_path) -> None:
    src = kit.write(tmp_path / "a", b"abc")
    theirs = kit.write(tmp_path / "b", b"keep")
    with pytest.raises(FileExistsError):
        safety.link_or_copy(src, theirs)
    assert theirs.read_bytes() == b"keep"
