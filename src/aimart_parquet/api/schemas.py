"""REST request bodies. Field text doubles as OpenAPI documentation agents read."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.errors import InvalidRequest
from aimart_parquet.quantum.models import PortfolioOptions, QuantumCompressOptions
from aimart_parquet.service import MAX_QUERY_TABLES, TABLE_NAME, SourceRef

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema

# `tables` holds the joins; `data` is the top-level input and counts toward the service's cap.
MAX_EXTRA_TABLES = MAX_QUERY_TABLES - 1


class _NamedKeys:
    """JSON Schema for a dict whose keys must match a pattern: ``propertyNames``.

    Pydantic emits ``patternProperties``, under which a key that does not match is simply
    unconstrained, so a client validating against the schema would send "Bad-Name".
    """

    def __get_pydantic_json_schema__(
        self, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        patterns = schema.get("patternProperties")
        if isinstance(patterns, dict) and len(patterns) == 1:
            [(pattern, value)] = patterns.items()
            del schema["patternProperties"]
            schema["propertyNames"] = {"pattern": pattern}
            schema["additionalProperties"] = value
        return schema


TableName = Annotated[str, StringConstraints(pattern=TABLE_NAME.pattern)]
NamedKeys = _NamedKeys()


class SourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(
        default=None, max_length=4096, description="Public http(s) URL of the input file."
    )
    content_base64: str | None = Field(
        default=None, description="Raw file bytes, base64-encoded (small files)."
    )
    filename: str | None = Field(
        default=None, max_length=255, description="Original filename; helps detection."
    )
    artifact_id: str | None = Field(
        default=None, max_length=64, description="artifact_id from /v1/convert."
    )

    def source(self) -> SourceRef:
        return SourceRef(
            url=self.url,
            content_base64=self.content_base64,
            filename=self.filename,
            artifact_id=self.artifact_id,
        )


class ConvertBody(SourceBody):
    options: ConvertOptions = Field(default_factory=ConvertOptions)


class PreviewBody(SourceBody):
    columns: list[str] | None = Field(default=None, max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=500)
    format: Literal["json", "markdown", "csv"] = "json"


class TableRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=4096)

    def source(self) -> SourceRef:
        return SourceRef(artifact_id=self.artifact_id, url=self.url)


class QueryBody(SourceBody):
    sql: str = Field(
        min_length=1,
        max_length=20_000,
        description="One DuckDB SELECT. The top-level input is table `data`; `tables` adds more.",
    )
    # max_length used to be 8 extra tables while the service caps the total at 8: data + 8
    # joins passed the schema, was quoted and paid for, and then failed with a 400.
    tables: Annotated[dict[TableName, TableRef], NamedKeys] | None = Field(
        default=None,
        max_length=MAX_EXTRA_TABLES,
        description=(
            f"Extra named tables for joins, up to {MAX_EXTRA_TABLES} besides `data`, e.g. "
            "{'gl': {'artifact_id': ...}, 'bank': {...}}. Names: lowercase letters, digits, "
            "underscores."
        ),
    )

    def table_sources(self) -> dict[str, SourceRef]:
        refs: dict[str, SourceRef] = {}
        if self.source().kinds():
            refs["data"] = self.source()
        for name, ref in (self.tables or {}).items():
            if name in refs:
                raise InvalidRequest(
                    f"Table name {name!r} is used twice.", hint="`data` is the top-level input."
                )
            refs[name] = ref.source()
        return refs

    limit: int = Field(default=100, ge=1, le=1000)
    format: Literal["json", "markdown", "csv"] = "json"


class ParquetSourceBody(BaseModel):
    """Premium tools read Parquet: pass an artifact_id from convert, or a url to a .parquet file."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str | None = Field(
        default=None, max_length=64, description="artifact_id from /v1/convert."
    )
    url: str | None = Field(
        default=None, max_length=4096, description="Public http(s) URL of a Parquet file."
    )

    def source(self) -> SourceRef:
        return SourceRef(artifact_id=self.artifact_id, url=self.url)


class QuantumCompressBody(ParquetSourceBody):
    options: QuantumCompressOptions = Field(default_factory=QuantumCompressOptions)


class PortfolioBody(ParquetSourceBody):
    options: PortfolioOptions = Field(default_factory=PortfolioOptions)


class VerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any] = Field(
        description="The `provenance.manifest` object exactly as received."
    )
    signature: str = Field(max_length=8192, description="The `provenance.signature` (base64).")
    artifact_id: str | None = Field(
        default=None,
        max_length=64,
        description="Also re-check the stored file's hash and size against the manifest.",
    )
