"""REST request bodies. Field text doubles as OpenAPI documentation agents read."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aimart_parquet.engine.models import ConvertOptions
from aimart_parquet.service import SourceRef


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


class QueryBody(SourceBody):
    sql: str = Field(
        min_length=1, max_length=20_000, description="One DuckDB SELECT over table `data`."
    )
    limit: int = Field(default=100, ge=1, le=1000)
    format: Literal["json", "markdown", "csv"] = "json"
