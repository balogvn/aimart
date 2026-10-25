"""Pydantic models shared by the engine, the REST API and the MCP tools.

These are the public contract: field descriptions become OpenAPI and MCP JSON Schema text that
LLMs read to decide how to call us, so they are written for that reader.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ExpectedColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Column name after normalization (snake_case by default).")
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
    encoding: str | None = Field(
        default=None, description="Text encoding, e.g. 'latin-1'. Omit to auto-detect."
    )
    null_values: list[str] = Field(
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
    sort_by: list[str] | None = Field(
        default=None,
        max_length=8,
        description="Sort rows by these columns so Parquet min/max stats prune well.",
    )
    compression: Literal["zstd", "snappy", "gzip", "none"] = "zstd"
    row_group_size: int = Field(default=122_880, ge=1_000, le=10_000_000)


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
