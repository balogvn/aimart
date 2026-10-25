"""Preview after the batched rewrite: no false 413s, no slow deep offsets, bounded bombs.

Round one replaced ``pads.dataset(path).take()`` (which decoded whole row groups) with 64-row
batches. Reviewed again, that went wrong in three ways:

* A dictionary column (a pandas categorical) hands every batch the whole column dictionary;
  counting it once per batch refused a 9.6 MB file of 200,000 categories with 413 at
  ``limit=500``.
* 64 rows were decoded whatever the limit: a 776-byte file of 128 rows pointing at one 16 MB
  value took 1.5 GB of RAM to preview one row, and eight 4 MB columns 1.7 GB.
* Rows before the offset were decoded and thrown away 64 at a time in Python: 5 s to reach the
  end of one 40M-row row group.

Batches sized from the footer, with text that has a dictionary page read as a dictionary,
fix the last two, except for dictionaries that fall back to plain pages
(TestDictionariesThatFallBackToPlainPages). Column selection also matched names
ignoring case through a dict, so ``a`` returned ``A``.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aimart_parquet.engine import tabular
from aimart_parquet.engine.tabular import preview
from aimart_parquet.errors import InputTooLarge, InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path


needs_resource = pytest.mark.skipif(
    sys.platform == "win32", reason="peak RSS comes from the resource module"
)


def _peak_rss_growth_mb(code: str, *args: str) -> tuple[float, str]:
    """Run ``code`` in a fresh interpreter; return (peak RSS growth in MB, its stdout)."""
    script = (
        "import resource, sys\n"
        "scale = 1 if sys.platform == 'darwin' else 1024  # ru_maxrss: bytes on macOS, else KiB\n"
        "def peak():\n"
        "    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale / 2**20\n"
        "import pyarrow, pyarrow.parquet, duckdb\n"
        "from aimart_parquet.engine.tabular import preview, query\n"
        "before = peak()\n" + textwrap.dedent(code) + "\nprint(f'GROWTH {peak() - before:.0f}')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    ).stdout
    return float(out.rsplit("GROWTH ", 1)[1]), out


def show(path: Path, *, offset: int = 0, limit: int = 500, columns=None, max_chars: int = 2000):
    return preview(
        path, columns=columns, offset=offset, limit=limit, fmt="json", max_chars=max_chars
    )


def _categorical(path: Path, rows: int, width: int) -> Path:
    """Like pandas ``astype('category')``: every value distinct, stored as an Arrow dictionary."""
    values = pa.array([f"{i:08d}" + "y" * (width - 8) for i in range(rows)])
    table = pa.table({"id": pa.array(range(rows)), "s": values.dictionary_encode()})
    pq.write_table(table, path, row_group_size=rows)
    return path


class TestDictionaryColumns:
    def test_a_high_cardinality_dictionary_is_not_counted_once_per_batch(
        self, tmp_path, monkeypatch
    ) -> None:
        # 20,000 distinct 100-char values: a 2 MB dictionary in every batch the reader returns.
        path = _categorical(tmp_path / "cat.parquet", 20_000, 100)
        monkeypatch.setattr(tabular, "MAX_RESULT_BYTES", 4 * 1024 * 1024)
        # Before: 8 batches x 2 MB of dictionary > 4 MB -> InputTooLarge for 500 rows of 100 B.
        result = show(path, limit=500)
        assert result.row_count == 500
        assert result.rows[0] == [0, "00000000" + "y" * 92]
        assert result.rows[499] == [499, "00000499" + "y" * 92]
        assert result.types == ["int64", "dictionary<values=string, indices=int32, ordered=0>"]

    def test_the_reviewers_file_previews_at_every_limit(self, tmp_path) -> None:
        # 300,000 distinct ~208-char values (6.4 MB on disk): 413 at limit 128, 300 and 500.
        path = _categorical(tmp_path / "hicard.parquet", 300_000, 208)
        for limit in (1, 64, 128, 300, 500):
            assert show(path, limit=limit).row_count == limit
        deep = show(path, offset=299_990, limit=50)
        assert [r[0] for r in deep.rows] == list(range(299_990, 300_000))

    def test_dictionary_values_are_cut_and_nulls_kept(self, tmp_path) -> None:
        path = tmp_path / "d.parquet"
        values = pa.array(["a" * 50, None, "bc", "a" * 50, "bc"]).dictionary_encode()
        pq.write_table(pa.table({"s": values}), path)
        result = show(path, max_chars=5)
        assert result.rows == [["aaaa…"], [None], ["bc"], ["aaaa…"], ["bc"]]
        assert result.types == ["dictionary<values=string, indices=int32, ordered=0>"]


def _ones(path: Path, rows: int, columns: int = 1) -> Path:
    zeros = pa.nulls(rows, pa.int8()).fill_null(1)
    pq.write_table(
        pa.table({f"z{c}": zeros for c in range(columns)}),
        path,
        row_group_size=rows,
        compression="zstd",
    )
    return path


class TestDeepOffsets:
    def test_a_deep_offset_in_one_huge_row_group_is_fast(self, tmp_path) -> None:
        path = _ones(tmp_path / "ones.parquet", 40_000_000, columns=2)
        assert path.stat().st_size < 2_000_000
        started = time.perf_counter()
        result = show(path, offset=39_999_990, limit=50)
        elapsed = time.perf_counter() - started
        assert result.rows == [[1, 1]] * 10
        assert result.total_rows == 40_000_000
        # Before: 625,000 Python iterations of 64 rows, about 3 s here. take() needed 0.3 s.
        assert elapsed < 1.5, elapsed

    @pytest.mark.parametrize("row_group_size", [1000, 4096, 300_000])
    @pytest.mark.parametrize(
        ("offset", "limit"), [(0, 1), (63, 2), (4095, 3), (150_000, 500), (299_999, 5)]
    )
    def test_rows_are_exact_after_any_skip(
        self, tmp_path, row_group_size: int, offset: int, limit: int
    ) -> None:
        n = 300_000
        table = pa.table(
            {
                "i": pa.array(range(n)),
                "s": pa.array([f"v{i % 977}" for i in range(n)]),
                "l": pa.array([[i, -i] if i % 3 else None for i in range(n)]),
                "st": pa.array([{"t": f"t{i}", "n": i} for i in range(n)]),
            }
        )
        path = tmp_path / "mix.parquet"
        pq.write_table(table, path, row_group_size=row_group_size)
        result = show(path, offset=offset, limit=limit)
        want = range(offset, min(offset + limit, n))
        assert [r[0] for r in result.rows] == list(want)
        assert [r[1] for r in result.rows] == [f"v{i % 977}" for i in want]
        assert [r[2] for r in result.rows] == [[i, -i] if i % 3 else None for i in want]
        assert [r[3] for r in result.rows] == [{"t": f"t{i}", "n": i} for i in want]
        assert result.types == [
            "int64",
            "string",
            "list<element: int64>",
            "struct<t: string, n: int64>",
        ]


def _bomb(path: Path, *, columns: int, rows: int = 20_000, width: int = 4 * 1024 * 1024) -> Path:
    """Each column's dictionary is one ``width``-byte value that every row points at."""
    table = pa.table(
        {
            f"s{c}": pa.DictionaryArray.from_arrays(
                pa.array([0] * rows, pa.int32()), pa.array([chr(97 + c) * width])
            )
            for c in range(columns)
        }
    )
    pq.write_table(
        table,
        path,
        store_schema=False,  # readers see plain string columns, not dictionaries
        row_group_size=rows,
        compression="zstd",
        dictionary_pagesize_limit=1 << 30,
    )
    return path


class TestBombs:
    @needs_resource
    @pytest.mark.parametrize(
        ("columns", "rows", "width", "before"),
        [
            pytest.param(1, 128, 16 * 1024 * 1024, 1468, id="1-column-128-rows-16MB"),
            pytest.param(8, 128, 4 * 1024 * 1024, 1697, id="8-columns-128-rows-4MB"),
            pytest.param(8, 20_000, 4 * 1024 * 1024, 1700, id="8-columns-20000-rows-4MB"),
        ],
    )
    def test_few_rows_pointing_at_huge_values(
        self, tmp_path, columns: int, rows: int, width: int, before: int
    ) -> None:
        bomb = _bomb(tmp_path / "bomb.parquet", columns=columns, rows=rows, width=width)
        assert bomb.stat().st_size < 4096  # gigabytes of text once decoded
        growth, out = _peak_rss_growth_mb(
            """
            from pathlib import Path
            rows = int(sys.argv[2])
            for limit, offset in ((1, 0), (500, 0), (500, rows - 50)):
                r = preview(Path(sys.argv[1]), columns=None, offset=offset, limit=limit,
                            fmt="json", max_chars=2000)
                print("ROWS", r.row_count, len(r.rows[0]), len(r.rows[-1][-1]))
            """,
            str(bomb),
            str(rows),
        )
        assert f"ROWS 1 {columns} 2000" in out
        assert f"ROWS {min(rows, 500)} {columns} 2000" in out
        assert f"ROWS 50 {columns} 2000" in out
        # Before (64 rows decoded per batch, as plain strings): this many MB for limit=1.
        assert growth < 300, (before, out)

    def test_a_list_of_bomb_values_is_cut_too(self, tmp_path) -> None:
        rows, width = 2_000, 1024 * 1024
        dictionary = pa.DictionaryArray.from_arrays(pa.array([0] * rows, pa.int32()), ["x" * width])
        lists = pa.ListArray.from_arrays(pa.array(range(rows + 1), pa.int32()), dictionary)
        path = tmp_path / "lbomb.parquet"
        pq.write_table(
            pa.table({"l": lists}),
            path,
            store_schema=False,
            compression="zstd",
            dictionary_pagesize_limit=1 << 30,
        )
        result = show(path, limit=500, max_chars=10)
        assert result.rows[0] == [["x" * 9 + "…"]]
        assert result.types == ["list<element: string>"]


def _high_cardinality(path: Path, rows: int, **options: Any) -> Path:
    """Distinct text: the writer's dictionary page fills up and it falls back to plain pages."""
    table = pa.table(
        {
            "i": pa.array(range(rows)),
            "s": pa.array([f"{i:08d}" + "y" * 200 for i in range(rows)]),
            "c": pa.array([f"cat{i % 5}" for i in range(rows)]),  # stays a small dictionary
            "l": pa.array([[f"{i}-a", f"{i}-b"] if i % 4 else None for i in range(rows)]),
            "st": pa.array([{"t": f"t{i}", "n": i} for i in range(rows)]),
        }
    )
    pq.write_table(table, path, **options)
    return path


class TestDictionariesThatFallBackToPlainPages:
    """Round 2: every text column with a dictionary page was read as a dictionary.

    Once a writer's dictionary page is full it writes plain pages, and Arrow then collects every
    plain value read so far into one dictionary: a 6 MB file of 300,000 distinct 208-character
    values took 306 MB to preview at offset 290,000, against 16 MB read as plain text.
    """

    @needs_resource
    def test_a_deep_offset_reads_them_as_plain_text(self, tmp_path) -> None:
        path = _high_cardinality(tmp_path / "hicard.parquet", 300_000, row_group_size=300_000)
        group = pq.ParquetFile(path).metadata.row_group(0).column(1)
        assert group.has_dictionary_page  # ...and plain pages after it
        growth, out = _peak_rss_growth_mb(
            """
            from pathlib import Path
            r = preview(Path(sys.argv[1]), columns=None, offset=290_000, limit=500,
                        fmt="json", max_chars=2000)
            print("ROWS", r.row_count, r.rows[0][0], r.rows[-1][1][:8])
            """,
            str(path),
        )
        assert "ROWS 500 290000 00290499" in out
        assert growth < 150, out

    @pytest.mark.parametrize(
        ("offset", "limit"),
        [(0, 5), (8191, 3), (16_380, 10), (16_000, 500), (40_000, 50), (49_999, 1), (7, 49_993)],
    )
    def test_rows_are_exact_across_the_switch(self, tmp_path, offset: int, limit: int) -> None:
        n = 50_000
        path = _high_cardinality(
            tmp_path / "fb.parquet", n, row_group_size=30_000, dictionary_pagesize_limit=4096
        )
        result = show(path, offset=offset, limit=limit, max_chars=300)
        want = range(offset, min(offset + limit, n))
        assert [r[0] for r in result.rows] == list(want)
        assert [r[1] for r in result.rows] == [f"{i:08d}" + "y" * 200 for i in want]
        assert [r[2] for r in result.rows] == [f"cat{i % 5}" for i in want]
        assert [r[3] for r in result.rows] == [
            [f"{i}-a", f"{i}-b"] if i % 4 else None for i in want
        ]
        assert [r[4] for r in result.rows] == [{"t": f"t{i}", "n": i} for i in want]
        assert result.types == [
            "int64",
            "string",
            "string",
            "list<element: string>",
            "struct<t: string, n: int64>",
        ]

    def test_reading_too_much_again_is_refused_unpaid(self, tmp_path, monkeypatch) -> None:
        path = _high_cardinality(
            tmp_path / "fb.parquet", 50_000, row_group_size=50_000, dictionary_pagesize_limit=4096
        )
        monkeypatch.setattr(tabular, "_PREVIEW_REREAD_BYTES", 10_000)
        assert show(path, offset=0, limit=500).row_count == 500  # one batch: nothing re-read
        with pytest.raises(InputTooLarge) as info:
            show(path, offset=40_000, limit=5)
        assert info.value.http_status == 413
        assert info.value.details == {"limit_bytes": 10_000}
        assert info.value.hint is not None
        assert "offset" in info.value.hint


class TestColumnSelection:
    @pytest.fixture
    def cased(self, tmp_path) -> Path:
        path = tmp_path / "case.parquet"
        table = pa.Table.from_arrays(
            [pa.array([1, 2]), pa.array([3, 4]), pa.array([5, 6])], names=["a", "A", "Name"]
        )
        pq.write_table(table, path)
        return path

    def test_an_exact_name_wins_over_a_case_variant(self, cased) -> None:
        # Before: {n.lower(): n} kept the last of a/A, so asking for "a" returned column "A".
        assert show(cased, columns=["a"]).rows == [[1], [2]]
        assert show(cased, columns=["A"]).rows == [[3], [4]]
        assert show(cased, columns=["A", "a"]).columns == ["A", "a"]

    def test_a_unique_case_insensitive_match_still_works(self, cased) -> None:
        result = show(cased, columns=["name"])
        assert result.columns == ["Name"]
        assert result.rows == [[5], [6]]

    def test_an_ambiguous_name_is_refused_with_the_candidates(self, tmp_path) -> None:
        path = tmp_path / "amb.parquet"
        pq.write_table(
            pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=["ab", "AB"]), path
        )
        with pytest.raises(InvalidRequest) as info:
            show(path, columns=["Ab"])
        assert info.value.http_status == 400
        assert "'Ab'" in info.value.message
        assert info.value.details["columns"] == ["AB", "ab"]
        assert info.value.hint is not None
        assert "exactly" in info.value.hint

    def test_unknown_names_are_still_listed(self, cased) -> None:
        with pytest.raises(InvalidRequest, match="Unknown columns") as info:
            show(cased, columns=["Name", "zz"])
        assert "zz" in info.value.message
        assert info.value.details["columns"] == ["a", "A", "Name"]
