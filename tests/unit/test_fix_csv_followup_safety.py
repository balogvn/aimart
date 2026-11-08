"""Follow-ups to the 1886f60 merge (perf/csv-memory), found by the safety review of 4510071.

CS5, a regression found while fixing CS1, and the worst of them: a 200 for wrong data. Since
1886f60 DuckDB read a gzip of CSV or TSV itself whenever ``safety.gzip_chunks`` found it read
as Python reads it. DuckDB 1.5.6 refills its 32 KiB input buffer at the end of each member.
When a refill has read exactly to the end of the file, the next member's end refills nothing,
and DuckDB stops: every member still in its buffer is dropped, with no error and no warning.
A 2-member CSV of exactly 10 + 2 x 32,768 bytes lost its last 50 rows of 2,550. A gzip of one
member per line (what ``gzip.open(path, 'ab')`` per row, or ``echo row | gzip >> f.gz``,
writes) lost its last 712 of 9,000. In 3,000 random multi-member layouts 88 lost rows, every
one where a model of those refills says. d83c738 unpacked every gzip with Python. Now DuckDB
reads only a gzip of one member (anything written in one go: gzip, pigz, Python, pandas). Any
other gzip is unpacked by Python, as at d83c738.

CS1, a regression, resolved by the same rule. ``gzip_chunks`` sliced its 1 MiB read buffer
about three times per member. DuckDB starts every member afresh, about 3.2 us each on each of
its two reads. A 50 MB gzip of a million one-line members converted 2.7-3.5x slower than at
d83c738, and on a slow core it was a 504 where d83c738 converted it and charged. A gzip of more
than one member now takes d83c738's path. Text that is not UTF-8 was also checked twice before
its transcode (``to_utf8`` repeated the check ``_gzip_text`` had made): it is transcoded
straight after the one check.

CS2, a regression. Text that is not UTF-8 was transcoded straight from the gzip, and only the
input was capped. Up to 3x the 256 MB stream cap of UTF-8 was written before the cap fired
(cp1252 0x80 is '€', three bytes in UTF-8). In the isolated child the job's 700 MB watchdog
stopped the job first, with a 413 that blamed the job's memory, where d83c738 refused the
stream itself ('Decompressed input exceeds the 256 MB limit.'). In-process it wrote 513-764
MiB, where d83c738 wrote 245. The whole stream is now counted against the cap before any of its
UTF-8 version is written (the wire test is in
``integration/test_final_integrate_stream_cap.py``).

CS3 and CS4, from the same review, were fixed before this file (``test_fix_csv_followup.py``,
CF2 and CF1). CS3, a later member's header DuckDB cannot hold, is moot now that DuckDB reads no
later member at all.
"""

from __future__ import annotations

import gzip
import random
import time
import zlib
from contextlib import closing, suppress
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

import aimart_testkit as kit
from aimart_parquet.engine import convert as convert_module
from aimart_parquet.engine import safety
from aimart_parquet.engine.limits import Limits
from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.engine.text import size_and_utf8
from aimart_parquet.errors import InputTooLarge

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_MB = 1024 * 1024
_BUFFER = 32 * 1024  # DuckDB's gzip input buffer
_HEADER = b"id,name,city,amount\n"
_LAST = b"9999999,Ren\xe9 Dupont,Paris,1.25\n"  # cp1252, not UTF-8: a name in the last row


def _rows(n: int, start: int = 0) -> list[bytes]:
    return [b"%d,user%d,Lagos,%d.25\n" % (i, i % 997, i % 10_000) for i in range(start, start + n)]


def _line_members(rows: int, *, last: bytes = b"", first: bytes | None = None) -> bytes:
    """A gzip of a CSV, one member per line: what appending a row at a time writes. ``first``
    replaces the header's member."""
    members = [gzip.compress(line, 6, mtime=0) for line in [*_rows(rows), last] if line]
    return (first or gzip.compress(_HEADER, 6, mtime=0)) + b"".join(members)


def _as_at_d83c738(rows: int, *, last: bytes = b"") -> bytes:
    """The same gzip with FCOMMENT on its first member. DuckDB refuses that header, so Python
    unpacks the file and the reader reads the text, as d83c738 did with every gzip."""
    return _line_members(rows, last=last, first=kit.gzip_member(_HEADER, 16))


def _member(data: bytes, *, name: bytes = b"", level: int = 6) -> bytes:
    """One gzip member of ``data``, with an FNAME of ``name`` when it is not empty."""
    return kit.gzip_member(data, 8, name=name) if name else gzip.compress(data, level, mtime=0)


def _cut_at_a_refill(rows: int) -> bytes:
    """Two members, the file exactly 10 + 2 x 32,768 bytes long. DuckDB's buffer starts at the
    first member's body (byte 10); its second refill reads exactly to the end of the file, and
    the first member ends after it: the refill at that end reads nothing, and DuckDB stops."""
    rng = random.Random(5)
    first = gzip.compress(
        _HEADER
        + b"".join(
            b"%d,%s,Lagos,1.25\n" % (i, rng.randbytes(12).hex().encode()) for i in range(rows)
        ),
        6,
        mtime=0,
    )
    second = _rows(50, start=rows)
    body = len(_member(b"".join(second), name=b"n")) - 1  # without the name's one byte
    name = b"n" * (10 + 2 * _BUFFER - len(first) - body)
    data = first + _member(b"".join(second), name=name)
    assert len(data) == 10 + 2 * _BUFFER
    assert _BUFFER < len(first) - 8 < 2 * _BUFFER  # the first ends in the second refill
    return data


def _python_rows(data: bytes) -> int:
    return gzip.decompress(data).count(b"\n") - 1


def _in_the_call_directory(work: Path, name: str, data: bytes, **options: Any) -> kit.Converted:
    """Convert as the service lays out a call: the upload is ``upload`` in the work directory."""
    upload = kit.write(work / "upload", data)
    out = work / "out.parquet"
    report = convert_module.convert_file(
        upload, out, filename=name, options=ConvertOptions(**options), workdir=work, limits=Limits()
    )
    return kit.Converted(report, out)


@pytest.fixture
def unpacked(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every gzip Python unpacks, as d83c738 did."""
    seen: list[Path] = []
    real = safety.decompress_stream

    def spy(src: Path, *args: Any) -> int:
        seen.append(src)
        return real(src, *args)

    monkeypatch.setattr(safety, "decompress_stream", spy)
    return seen


# --------------------------------------------- CS5: DuckDB reads only a gzip of one member
@pytest.mark.parametrize(
    "layout",
    ["one-member", "two-members", "cut-at-a-refill", "one-member-per-line"],
)
def test_every_row_of_a_gzip_is_converted_and_only_one_member_is_read_by_duckdb(
    tmp_path: Path, unpacked: list[Path], layout: str
) -> None:
    # 4510071: 'cut-at-a-refill' converted 2,500 rows of 2,550 and 'one-member-per-line' 8,288
    # of 9,000, each a 200 with no warning. d83c738 unpacked both and converted every row.
    layouts: dict[str, Callable[[], bytes]] = {
        "one-member": lambda: gzip.compress(_HEADER + b"".join(_rows(9_000)), 6),
        "two-members": lambda: (
            gzip.compress(_HEADER + b"".join(_rows(4_000)), 6)
            + gzip.compress(b"".join(_rows(5_000, start=4_000)), 6)
        ),
        "cut-at-a-refill": lambda: _cut_at_a_refill(2_500),
        "one-member-per-line": lambda: _line_members(9_000),
    }
    data = layouts[layout]()
    result = _in_the_call_directory(tmp_path / "call", "rows.csv.gz", data)
    assert result.report.rows == _python_rows(data)
    assert result.rows["id"] == [
        int(line.split(b",")[0]) for line in gzip.decompress(data).splitlines()[1:]
    ]
    assert len(unpacked) == (layout != "one-member")  # one member: DuckDB reads the gzip


def test_duckdb_drops_the_members_left_when_a_refill_reads_to_the_end(tmp_path: Path) -> None:
    # Why a gzip of more than one member is unpacked, pinned on this DuckDB: an upgrade that
    # reads every member fails here, and then only that rule needs a second look.
    con = duckdb.connect()
    con.execute("SET threads=1")
    sql = (
        "SELECT count(*) FROM read_csv('{}', header=true, auto_detect=false, columns="
        "{{'id': 'VARCHAR', 'name': 'VARCHAR', 'city': 'VARCHAR', 'amount': 'VARCHAR'}}, "
        "compression='gzip')"
    )
    for data, read in [(_cut_at_a_refill(2_500), 2_500), (_line_members(9_000), 8_288)]:
        path = kit.write(tmp_path / f"{read}.csv.gz", data)
        assert _python_rows(data) > read
        assert con.execute(sql.format(path)).fetchone() == (read,)
        with pytest.raises(safety.GzipMismatch):
            b"".join(safety.gzip_chunks(path, 10**9))


@pytest.mark.parametrize("seed", range(12))
def test_no_layout_loses_a_row(tmp_path: Path, seed: int) -> None:
    # Random layouts in which a member ends, and so a refill reads, exactly 32 KiB before the
    # end of the file (a refill at a member's end starts at its 8-byte trailer), and a member
    # that is not the last ends after it. 4510071 converted fewer rows than each holds.
    rng = random.Random(seed)
    members, start = [], 0
    for k in range(rng.choice([2, 3, 6, 40])):
        n = rng.choice([20, 600, 1_500]) if k == 0 else rng.choice([1, 20, 300])
        text = (_HEADER if k == 0 else b"") + b"".join(_rows(n, start))
        members.append(_member(text, level=rng.choice([0, 1, 6, 9])))
        start += n
    members.append(_member(b"".join(_rows(1, start)), level=rng.choice([0, 6])))
    start += 1
    tail = b"".join(_rows(30, start))
    trailer = sum(map(len, members[:-1])) - 8  # of the member before the last
    body = len(_member(tail, name=b"t")) - 1
    name = b"t" * (trailer + _BUFFER - sum(map(len, members)) - body)
    data = b"".join(members) + _member(tail, name=name)
    assert len(data) == trailer + _BUFFER
    result = kit.convert_bytes(tmp_path, "rows.csv.gz", data)
    assert result.report.rows == _python_rows(data) == start + 30
    assert result.rows["id"] == list(range(start + 30))


# --------------------------------------------- CS1: a gzip of many members, a second check
def _best(run: Callable[[], object], times: int = 3) -> float:
    took = []
    for _ in range(times):
        started = time.perf_counter()
        run()
        took.append(time.perf_counter() - started)
    return min(took)


@pytest.mark.parametrize("layout", ["one-member", "one-member-per-line"])
def test_the_check_costs_what_pythons_gzip_module_takes_to_read_the_file(
    tmp_path: Path, layout: str
) -> None:
    # 4510071's gzip_chunks re-sliced its 1 MiB read buffer three times for every member: 2.4x
    # the time Python's gzip module takes to read this gzip of 20,000 one-line members (the
    # review's 50 MB one: 24.0 s against 9.3 s), where for one member the two take the same. It
    # stops at the second member now. gzip.decompress is no measure of it: it slices its input
    # for every member too, and took 3.6 s for 60,000 members against 4510071's 1.2 s.
    layouts: dict[str, Callable[[], bytes]] = {
        "one-member": lambda: gzip.compress(_HEADER + b"".join(_rows(400_000)), 6),
        "one-member-per-line": lambda: _line_members(20_000),
    }
    path = kit.write(tmp_path / "t.csv.gz", layouts[layout]())

    def check() -> None:
        with closing(safety.gzip_chunks(path, 10**9)) as chunks, suppress(safety.GzipMismatch):
            for _ in chunks:
                pass

    def python() -> None:
        with gzip.open(path, "rb") as fh:
            while fh.read(_MB):
                pass

    took, python_took = _best(check), _best(python)
    assert took < 1.5 * python_took, (took, python_took)


@pytest.mark.parametrize("last", [b"", _LAST], ids=["utf8", "cp1252-last-row"])
def test_a_gzip_of_many_members_converts_as_fast_as_at_d83c738(tmp_path: Path, last: bytes) -> None:
    # 4510071 converted these 2.5-3x slower than their twins, which take d83c738's path (the
    # header is FCOMMENT, which DuckDB refuses, so Python unpacks the file).
    data, twin = _line_members(9_000, last=last), _as_at_d83c738(9_000, last=last)
    read: list[kit.Converted] = []
    here = _best(lambda: read.append(kit.convert_bytes(tmp_path, "lines.csv.gz", data)))
    there = _best(lambda: read.append(kit.convert_bytes(tmp_path, "lines.csv.gz", twin)))
    assert read[0].rows == read[-1].rows
    assert read[0].report.rows == 9_000 + bool(last)
    assert here < 1.5 * there, (here, there)


@pytest.fixture
def inflated(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every byte inflated from a gzip, by gzip_chunks or by Python's gzip module."""
    total = [0]
    chunks = safety.gzip_chunks

    def counted(path: Path, cap: int) -> Iterator[bytes]:
        inner = chunks(path, cap)
        try:
            for chunk in inner:
                total[0] += len(chunk)
                yield chunk
        finally:
            inner.close()

    read = gzip.GzipFile.read

    def counted_read(self: gzip.GzipFile, size: int = -1) -> bytes:
        data = read(self, size)
        total[0] += len(data)
        return data

    monkeypatch.setattr(safety, "gzip_chunks", counted)
    monkeypatch.setattr(gzip.GzipFile, "read", counted_read)
    return total


@pytest.mark.parametrize("encoding", [None, "cp1252"])
def test_text_that_is_not_utf8_is_inflated_twice(
    tmp_path: Path, inflated: list[int], encoding: str | None
) -> None:
    # Once to count it against the cap (and check it is UTF-8), once to transcode it. 4510071
    # checked it again through Python's gzip module first (to_utf8): three passes over a text
    # whose only cp1252 byte is in its last row.
    text = _HEADER + b"".join(_rows(150_000)) + _LAST
    result = kit.convert_bytes(tmp_path, "t.csv.gz", gzip.compress(text, 6), encoding=encoding)
    assert result.report.rows == 150_001
    if encoding:  # else detected from the first 2 MB, ASCII, as at d83c738: U+FFFD
        assert result.rows["name"][-1] == "René Dupont"
    # Detection reads 2 MB, keep_gzip 1 KB, and the reads stop up to 1 MiB past those.
    assert inflated[0] <= 2 * len(text) + 4 * _MB, (inflated[0], len(text))


# ------------------------------------------------- CS2: a bomb of text that is not UTF-8
@pytest.mark.parametrize(
    ("byte", "encoding", "members"),
    [
        (b"\x80", None, 1),
        (b"\x80", "cp1252", 1),
        (b"\xe9", None, 1),
        (b"\xe9", "latin-1", 1),
        (b"\x80", None, 12),
    ],
    ids=["euro-detected", "euro-named", "e-acute-detected", "e-acute-named", "members"],
)
def test_a_bomb_of_text_that_is_not_utf8_is_refused_before_its_utf8_is_written(
    tmp_path: Path, byte: bytes, encoding: str | None, members: int
) -> None:
    # 4510071 gave d83c738's message in-process too, but only after writing 2-3x the cap in
    # UTF-8 (513-764 MiB for the 256 MB cap). In the isolated child the job's watchdog stopped
    # it first, with another 413. The cap is over detection's 2 MB sample, as in production.
    cap = 3 * _MB
    rows = (b"1," + byte * 1000 + b"\n") * 1000  # 1 MB of text
    if members == 1:
        deflate = zlib.compressobj(9, zlib.DEFLATED, 31)
        data = deflate.compress(b"a,b\n") + b"".join(deflate.compress(rows) for _ in range(12))
        data += deflate.flush()
    else:
        data = gzip.compress(b"a,b\n", 9) + gzip.compress(rows, 9) * (members - 1)
    work = tmp_path / "call"
    upload = kit.write(work / "upload", data)
    with pytest.raises(InputTooLarge) as info:
        convert_module.convert_file(
            upload,
            work / "out.parquet",
            filename="bomb.csv.gz",
            options=ConvertOptions(encoding=encoding),
            workdir=work,
            limits=Limits(max_decompressed_bytes=cap),
        )
    assert info.value.message == "Decompressed input exceeds the 3 MB limit."
    assert info.value.hint == "Split the file, or send a smaller extract."
    assert info.value.details == {"limit_bytes": cap}
    upload_ino = upload.stat().st_ino
    written = {
        p.name: p.stat().st_size
        for p in work.rglob("*")
        if p.is_file() and p.stat().st_ino != upload_ino
    }
    # Nothing, or the unpacked text up to the cap, as at d83c738 (for more than one member).
    assert sum(written.values()) <= cap, written


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ([b"ab", "é".encode()], (4, True)),
        ([b"a\xe9", b"bc"], (4, False)),  # counted on past the first byte that is not UTF-8
        ([b"ab\xc3", b"\xa9c"], (5, True)),  # a character split across two chunks
        ([b"ab\xc3"], (3, False)),  # the stream ends inside a character
    ],
    ids=["utf8", "cp1252", "split", "cut"],
)
def test_the_count_reads_to_the_end_whatever_it_finds(
    chunks: list[bytes], expected: tuple[int, bool]
) -> None:
    # What _gzip_text counts against the cap before any UTF-8 is written: every byte.
    assert size_and_utf8(iter(chunks)) == expected
