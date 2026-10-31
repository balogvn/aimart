"""Pydantic models shared by the engine, the REST API and the MCP tools.

These are the public contract: field descriptions become OpenAPI and MCP JSON Schema text that
LLMs read to decide how to call us, so they are written for that reader.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from aimart_parquet.engine.text import encoding_problem


class InputFormat(StrEnum):
    CSV = "csv"
    TSV = "tsv"
    JSON = "json"
    NDJSON = "ndjson"
    XLSX = "xlsx"
    XLS = "xls"
    ODS = "ods"
    SAV = "sav"
    ZSAV = "zsav"
    POR = "por"
    DTA = "dta"
    SAS7BDAT = "sas7bdat"
    XPT = "xpt"
    PARQUET = "parquet"
    PDF = "pdf"


class DateOrder(StrEnum):
    AUTO = "auto"
    DMY = "dmy"
    MDY = "mdy"
    YMD = "ymd"


class ValueLabelMode(StrEnum):
    METADATA = "metadata"
    APPLY = "apply"
    BOTH = "both"


ColumnType = Literal[
    "string", "int64", "float64", "bool", "date", "timestamp", "timestamptz", "decimal"
]


def _duck_text(value: str) -> str:
    # DuckDB reads the SQL it is given as a C string: a NUL ends it. In a name or a null token
    # that cut the statement short inside a quote (a bare 500 after payment); in a query it
    # silently dropped everything after it ("SELECT * FROM data\0 WHERE ..." ran unfiltered).
    if "\x00" in value:
        msg = "must not contain a NUL character (\\x00)"
        raise ValueError(msg)
    # A lone surrogate ('\ud800' is a valid JSON escape) has no UTF-8 form, so DuckDB cannot be
    # given it: as a null token it raised UnicodeEncodeError, a 500 after payment, and in SQL
    # the parser's TypeError.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        msg = "must be valid Unicode text; it holds a lone surrogate (such as \\ud800)"
        raise ValueError(msg) from None
    return value


DuckText = AfterValidator(_duck_text)
"""For text that reaches DuckDB: refused when it holds a NUL (DuckDB reads it as the end of the
text) or a lone surrogate (it has no UTF-8 form)."""

# Values ``delimiter`` can never take: csv.reader refuses the quote character and line breaks
# (a ValueError, a bare 500 after payment), and a NUL ends the SQL literal DuckDB is given.
_UNUSABLE_DELIMITERS = {
    '"': "the quote character",
    "\n": "a line break",
    "\r": "a line break",
    "\x00": "NUL",
}
# One part of pdf_pages, as the PDF reader parses it: '3' or '1-5', spaces allowed.
_PAGE_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")


def pdf_page_ranges(spec: str) -> list[tuple[int, int]]:
    """'1-3,7' -> [(1, 3), (7, 7)]; ValueError for a part no PDF has, decided from the text alone.

    How many pages a range selects depends on the document, so that is the reader's to check.
    """
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        match = _PAGE_RANGE.match(part)
        if not match:
            msg = f"pdf_pages {spec!r} is not a page list. Use a form like '1-3,7'."
            raise ValueError(msg)
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            msg = (
                f"pdf_pages range {part.strip()!r} is invalid: pages start at 1, and a range "
                "runs low to high."
            )
            raise ValueError(msg)
        ranges.append((start, end))
    return ranges


class ExpectedColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, DuckText] = Field(
        min_length=1, description="Column name after normalization (snake_case by default)."
    )
    type: ColumnType
    nullable: bool = True
    precision: int | None = Field(
        default=None, ge=1, le=38, description="decimal only: total digits. Default 18."
    )
    scale: int | None = Field(
        default=None,
        ge=0,
        le=38,
        description=(
            "decimal only: digits after the point, at most precision. Default 2; 0 for whole "
            "numbers. A value that would round to fit is a counted reject, never rounded."
        ),
    )

    @model_validator(mode="after")
    def _scale_fits(self) -> ExpectedColumn:
        # Checked here so the request schema refuses it before any work (or any payment);
        # convert repeats the check for callers that build the model without validation.
        if self.type == "decimal":
            precision = 18 if self.precision is None else self.precision
            scale = 2 if self.scale is None else self.scale
            if scale > precision:
                default = " (the default)" if self.scale is None else ""
                msg = f"decimal scale {scale}{default} exceeds precision {precision}"
                raise ValueError(msg)
        return self


class ConvertOptions(BaseModel):
    """How to read the input and shape the Parquet output. Every field is optional."""

    model_config = ConfigDict(extra="forbid")

    format: InputFormat | None = Field(
        default=None, description="Force the input format. Omit to auto-detect."
    )
    sheet: str | int | None = Field(
        default=None,
        description="Excel/ODS sheet name or 0-based index. Default: first non-empty sheet.",
    )
    delimiter: str | None = Field(
        default=None, min_length=1, max_length=1, description="CSV delimiter. Omit to sniff."
    )
    header: bool | None = Field(
        default=None, description="Whether the first row is a header. Omit to auto-detect."
    )
    # max_length is checked before _text_encoding: codecs.lookup caches every unknown name for
    # good, so unpaid requests with megabyte-long names grew the API process by their size (and
    # each took ~1 s of the event loop). The longest stdlib codec name has 24 characters.
    encoding: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Text encoding as a Python codec name, e.g. 'latin-1', 'cp1252' or 'utf-16-le'. "
            "Omit to auto-detect."
        ),
    )
    null_values: list[Annotated[str, DuckText]] = Field(
        default_factory=list,
        max_length=50,
        description="Extra tokens to treat as null, in addition to '', NA, N/A, null, None, -.",
    )
    normalize_column_names: bool = Field(
        default=True, description="Rewrite column names to unique snake_case."
    )
    infer_types: bool = Field(
        default=True,
        description="Infer int/float/bool/date/timestamp columns from text. False keeps text.",
    )
    date_order: DateOrder = Field(
        default=DateOrder.AUTO,
        description="Order for ambiguous dates like 03/04/2025. auto assumes day-first.",
    )
    pdf_pages: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^[0-9 ,\-]+$",
        description="PDF only: pages to read, e.g. '1-3,7'. Default: all (up to 300).",
    )
    pdf_table: int | None = Field(
        default=None,
        ge=0,
        description="PDF only: which detected table to convert (see available_sheets). Default: the largest.",
    )
    json_records_path: str | None = Field(
        default=None,
        max_length=200,
        description="Dot path to the array of records inside a JSON document, e.g. 'data.items'.",
    )
    flatten_nested: bool = Field(
        default=False,
        description="Flatten nested JSON objects into parent__child columns; lists become JSON.",
    )
    value_labels: ValueLabelMode = Field(
        default=ValueLabelMode.METADATA,
        description=(
            "SPSS/Stata/SAS value labels: 'metadata' keeps codes and stores labels in Parquet "
            "metadata; 'apply' replaces codes with label text; 'both' adds <col>_label columns."
        ),
    )
    expected_schema: list[ExpectedColumn] | None = Field(
        default=None,
        max_length=2000,
        description="Validate and cast to this schema. Unknown columns are kept unless strict.",
    )
    strict: bool = Field(
        default=False,
        description="With expected_schema: fail on any value that does not fit, or extra columns.",
    )
    sort_by: list[Annotated[str, DuckText]] | None = Field(
        default=None,
        max_length=8,
        description="Sort rows by these columns so Parquet min/max stats prune well.",
    )
    compression: Literal["zstd", "snappy", "gzip", "none"] = "zstd"
    row_group_size: int = Field(default=122_880, ge=1_000, le=10_000_000)

    # The rules below are decided by the request alone, so the request schema refuses them
    # before any price is quoted. Each used to be found only after the payment was verified.
    @field_validator("delimiter")
    @classmethod
    def _usable_delimiter(cls, value: str | None) -> str | None:
        if value is not None and value in _UNUSABLE_DELIMITERS:
            msg = (
                f"delimiter {value!r} cannot separate fields: it is "
                f"{_UNUSABLE_DELIMITERS[value]}. Omit delimiter to detect it."
            )
            raise ValueError(msg)
        return value

    @field_validator("encoding")
    @classmethod
    def _text_encoding(cls, value: str | None) -> str | None:
        problem = encoding_problem(value) if value is not None else None
        if problem is not None:
            raise ValueError(problem)
        return value

    @field_validator("pdf_pages")
    @classmethod
    def _page_list(cls, value: str | None) -> str | None:
        if value is not None:
            pdf_page_ranges(value)
        return value

    @model_validator(mode="after")
    def _unique_expected_names(self) -> ConvertOptions:
        # Two declarations of one column (DuckDB names are case-insensitive) each became an
        # output: DuckDB renamed the second ('a_1'), the report described the first twice, and
        # 'a' with 'A' crashed the report (KeyError: 'A'), all after the payment was verified.
        seen: dict[str, str] = {}
        for col in self.expected_schema or ():
            key = col.name.lower()  # the key _apply_expected_schema matches input columns by
            if key in seen:
                first = seen[key]
                case = (
                    ""
                    if first == col.name
                    else f" (as {first!r} and {col.name!r}; names are case-insensitive)"
                )
                msg = f"expected_schema names column {first!r} twice{case}."
                raise ValueError(msg)
            seen[key] = col.name
        return self


class ColumnReport(BaseModel):
    name: str
    original_name: str
    type: str = Field(description="Arrow type of the column in the output file.")
    nullable: bool
    null_count: int
    inferred_from_text: bool = Field(
        default=False, description="True when the type was inferred from text values."
    )
    rejected_count: int = Field(
        default=0, description="Values that did not fit the chosen type and were set to null."
    )
    rejected_examples: list[str] = Field(default_factory=list, max_length=3)
    label: str | None = Field(default=None, description="Variable label (SPSS/Stata/SAS).")
    value_labels: dict[str, str] | None = None
    notes: list[str] = Field(default_factory=list)


class ConversionReport(BaseModel):
    source_format: InputFormat
    source_bytes: int
    output_bytes: int
    rows: int
    columns: list[ColumnReport]
    encoding: str | None = None
    delimiter: str | None = None
    sheet: str | None = None
    available_sheets: list[str] | None = None
    warnings: list[str] = Field(default_factory=list)
    duration_ms: int
    estimated_tokens_raw: int = Field(
        description="Rough LLM tokens to read the input as text (bytes / 4)."
    )
    estimated_tokens_schema: int = Field(
        description=(
            "Rough LLM tokens for a name/type/null-count digest of the output schema (bytes / 4). "
            "The full report you are reading is larger."
        )
    )


class Provenance(BaseModel):
    algorithm: str = Field(description="ML-DSA-65 (NIST FIPS 204), a post-quantum signature.")
    manifest: dict[str, Any] = Field(
        description="Signed facts: sha256, size, operation, time, service version."
    )
    signature: str = Field(description="Base64 signature over the canonical JSON of the manifest.")
    public_key_url: str = Field(description="Where to fetch the verification key.")


class ArtifactRef(BaseModel):
    id: str = Field(description="Pass as artifact_id to inspect/preview/query without re-upload.")
    filename: str
    size_bytes: int
    sha256: str
    expires_at: str = Field(
        description=(
            "RFC 3339 UTC time after which the artifact and its download link are unreachable "
            "(deletion follows; see data_retention in /pricing)."
        )
    )
    download_url: str | None = Field(
        default=None, description="Signed HTTPS link to the Parquet file, valid until expiry."
    )
    path: str | None = Field(default=None, description="Local file path (local mode only).")
    provenance: Provenance | None = Field(
        default=None,
        description="Post-quantum signature proving this file came unaltered from this service.",
    )


class ConvertResult(BaseModel):
    artifact: ArtifactRef
    report: ConversionReport
    hint: str | None = Field(
        default=None, description="Pointer to premium features (local mode only)."
    )


class ColumnStats(BaseModel):
    name: str
    type: str
    nullable: bool
    null_count: int | None = None
    min: Any = None
    max: Any = None
    label: str | None = None
    value_labels: dict[str, str] | None = None


class InspectResult(BaseModel):
    rows: int
    row_groups: int
    columns: list[ColumnStats]
    size_bytes: int
    created_by: str | None = None
    key_value_metadata: dict[str, str] = Field(
        default_factory=dict, description="File-level metadata, long values truncated."
    )


class TabularResult(BaseModel):
    columns: list[str]
    types: list[str]
    rows: list[list[Any]] | None = Field(default=None, description="Present when format=json.")
    text: str | None = Field(default=None, description="Present when format=markdown or csv.")
    row_count: int
    truncated: bool = Field(description="True when more rows matched than were returned.")
    total_rows: int | None = None
