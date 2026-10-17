"""The MCP server: four tools, written for the model that has to choose and call them.

Tool descriptions say what each tool is for, when to use it instead of another, what it costs,
and what to call next — that text is the whole interface an agent sees. Results are typed
(``outputSchema`` + ``structuredContent``), compact, and refer to converted files by
``artifact_id`` and download link instead of inlining bytes.

Remote mode takes ``url`` / ``content_base64`` / ``artifact_id`` inputs and is paid per call by
the x402 extension (``payments.mcp_gate``). Local stdio mode adds ``path`` inputs and an
``output_path``, and is free.
"""

# No `from __future__ import annotations`: the MCP SDK evaluates tool signatures at runtime,
# and the parameter types below are local to build_mcp (they embed per-deployment limits).

import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Extension, MCPServer
from mcp_types import CallToolResult, ResourceLink, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from aimart_parquet import __version__
from aimart_parquet.config import Settings
from aimart_parquet.engine.models import ConvertOptions, ConvertResult, InspectResult, TabularResult
from aimart_parquet.errors import AimartError
from aimart_parquet.quantum import available as quantum_available
from aimart_parquet.quantum.models import (
    PortfolioOptions,
    PortfolioResult,
    QuantumCompressOptions,
    QuantumCompressResult,
)
from aimart_parquet.service import Service, SourceRef

PARQUET_MIME = "application/vnd.apache.parquet"

INSTRUCTIONS = """\
Turns messy tabular files into clean, typed, compressed Parquet, then lets you look inside \
without reading everything.

Typical flow: convert_to_parquet (returns an artifact_id and a schema report) -> \
inspect_parquet or preview_parquet to look -> query_parquet to answer questions with SQL. \
Pass the artifact_id between calls instead of re-sending the file.

Inputs: a public http(s) `url` (best for large files), or `content_base64` plus `filename` \
(small files, under ~3 MB). Formats: CSV/TSV, JSON/NDJSON, Excel (xlsx/xls/ods), \
SPSS (sav/zsav/por), Stata (dta), SAS (sas7bdat/xpt), Parquet; optionally gzip/bz2/zip.\
"""

UrlArg = Annotated[
    str | None,
    Field(
        description="Public http(s) URL of the input file. Preferred for anything over ~3 MB.",
        max_length=4096,
    ),
]
B64Arg = Annotated[
    str | None,
    Field(description="The file's raw bytes, base64-encoded. Small files only (under ~3 MB)."),
]
NameArg = Annotated[
    str | None,
    Field(
        description="Original filename, e.g. 'survey.sav'. The extension helps format detection.",
        max_length=255,
    ),
]
ArtifactArg = Annotated[
    str | None,
    Field(description="artifact_id returned by an earlier convert_to_parquet call.", max_length=64),
]
PathArg = Annotated[
    str | None, Field(description="Local filesystem path of the input file.", max_length=4096)
]
FormatArg = Annotated[
    Literal["json", "markdown", "csv"],
    Field(description="json rows (default), a Markdown table, or CSV text."),
]


def _ok(model: BaseModel, extra: list[TextContent | ResourceLink] | None = None) -> CallToolResult:
    data = model.model_dump(mode="json", exclude_none=True)
    content: list[Any] = [
        TextContent(type="text", text=json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    ]
    return CallToolResult(
        content=[*content, *(extra or [])], structured_content=data, is_error=False
    )


def _err(exc: AimartError) -> CallToolResult:
    body = exc.to_dict()
    return CallToolResult(
        content=[
            TextContent(
                type="text", text=json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            )
        ],
        structured_content=body,
        is_error=True,
    )


async def _wrap(coro: Awaitable[BaseModel]) -> CallToolResult:
    return _ok(await coro)


async def _guard(fn: Callable[[], Awaitable[CallToolResult]]) -> CallToolResult:
    """Every failure becomes an isError result — which the payment gate never settles."""
    try:
        return await fn()
    except AimartError as exc:
        return _err(exc)


def _price_note(price: str, *, local: bool) -> str:
    return (
        "Free in local mode."
        if local
        else f"Price: {price} USDC per successful call, paid via x402."
    )


def build_mcp(
    service: Service,
    settings: Settings,
    *,
    local_mode: bool,
    extensions: list[Extension] | None = None,
) -> MCPServer:
    server = MCPServer(
        name="aimart-parquet",
        title="AImart Parquet",
        description="Convert messy CSV/JSON/Excel/SPSS/Stata/SAS files to clean Parquet; inspect, preview and query them.",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/balogvn/aimart",
        extensions=extensions or [],
    )
    read_only = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    )

    def note(price: str) -> str:
        return _price_note(price, local=local_mode)

    def convert_result(result: ConvertResult) -> CallToolResult:
        extra: list[TextContent | ResourceLink] = []
        if result.artifact.download_url:
            extra.append(
                ResourceLink(
                    type="resource_link",
                    name=result.artifact.filename,
                    uri=result.artifact.download_url,
                    mime_type=PARQUET_MIME,
                    size=result.artifact.size_bytes,
                    description="Converted Parquet file (signed link, expires with the artifact).",
                )
            )
        return _ok(result, extra)

    convert_description = f"""Convert a messy data file into clean, typed, zstd-compressed Parquet.

Use this first for any CSV/TSV, JSON/NDJSON, Excel, SPSS, Stata or SAS file. It detects the \
format, encoding and delimiter; skips title rows; repairs broken JSON; infers int/float/bool/\
date/timestamp columns (including 1.234,56 and $1,234 formats) while keeping IDs with leading \
zeros as text; normalizes column names to snake_case; keeps SPSS/Stata/SAS variable and value \
labels; and can validate against an expected_schema.

Returns an artifact_id (pass it to inspect_parquet / preview_parquet / query_parquet), a \
download link, and a per-column report: type, null count, values that did not fit the chosen \
type, and warnings. {note(settings.price_convert)}"""

    if local_mode:

        @server.tool(
            name="convert_to_parquet",
            title="Convert to Parquet",
            description=convert_description,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def convert_local(
            path: PathArg = None,
            url: UrlArg = None,
            content_base64: B64Arg = None,
            filename: NameArg = None,
            output_path: Annotated[
                str | None,
                Field(description="Where to write the .parquet file. Default: next to the input."),
            ] = None,
            options: ConvertOptions | None = None,
        ) -> Annotated[CallToolResult, ConvertResult]:
            src = SourceRef(path=path, url=url, content_base64=content_base64, filename=filename)
            return await _guard(lambda: _convert(src, options, output_path))

    else:

        @server.tool(
            name="convert_to_parquet",
            title="Convert to Parquet",
            description=convert_description,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        )
        async def convert_remote(
            url: UrlArg = None,
            content_base64: B64Arg = None,
            filename: NameArg = None,
            options: ConvertOptions | None = None,
        ) -> Annotated[CallToolResult, ConvertResult]:
            src = SourceRef(url=url, content_base64=content_base64, filename=filename)
            return await _guard(lambda: _convert(src, options, None))

    async def _convert(
        src: SourceRef, options: ConvertOptions | None, output_path: str | None
    ) -> CallToolResult:
        result = await service.convert(src, options or ConvertOptions(), output_path=output_path)
        return convert_result(result)

    inspect_description = f"""Describe a Parquet file from its footer without reading rows: row count, \
columns, types, null counts, min/max per column, and SPSS/Stata/SAS labels. The cheapest way to \
understand a dataset before querying it. Input: artifact_id from convert_to_parquet, or a url to \
a .parquet file. {note(settings.price_inspect)}"""
    preview_description = f"""Return a slice of rows from a Parquet file (default 20, max \
{settings.max_preview_rows}), optionally only some columns, as JSON rows, a Markdown table, or \
CSV. Use to eyeball data; use query_parquet to filter or aggregate. Long cells are truncated. \
{note(settings.price_preview)}"""
    query_description = f"""Run one read-only DuckDB SELECT over a Parquet file, available as \
table `data`. Supports aggregates, window functions, CTEs, DESCRIBE data and SUMMARIZE data. \
Up to {settings.max_query_rows} rows are returned; `truncated` says if more matched. Writes, \
file access and multiple statements are rejected. Example: SELECT region, sum(units) FROM data \
GROUP BY 1 ORDER BY 2 DESC. {note(settings.price_query)}"""

    Columns = Annotated[list[str] | None, Field(description="Only these columns.", max_length=200)]
    Offset = Annotated[int, Field(ge=0, description="Rows to skip.")]
    PreviewLimit = Annotated[
        int, Field(ge=1, le=settings.max_preview_rows, description="Rows to return.")
    ]
    QueryLimit = Annotated[
        int, Field(ge=1, le=settings.max_query_rows, description="Maximum rows to return.")
    ]
    Sql = Annotated[
        str,
        Field(
            description="One DuckDB SELECT statement over table `data`.",
            min_length=1,
            max_length=20_000,
        ),
    ]

    async def _inspect(src: SourceRef) -> CallToolResult:
        return await _guard(lambda: _wrap(service.inspect(src)))

    async def _preview(
        src: SourceRef, columns: list[str] | None, offset: int, limit: int, fmt: Any
    ) -> CallToolResult:
        return await _guard(
            lambda: _wrap(
                service.preview(src, columns=columns, offset=offset, limit=limit, fmt=fmt)
            )
        )

    async def _query(src: SourceRef, sql: str, limit: int, fmt: Any) -> CallToolResult:
        return await _guard(lambda: _wrap(service.query({"data": src}, sql, limit=limit, fmt=fmt)))

    if local_mode:

        @server.tool(
            name="inspect_parquet",
            title="Inspect Parquet schema",
            annotations=read_only,
            description=inspect_description,
        )
        async def inspect_local(
            path: PathArg = None, url: UrlArg = None
        ) -> Annotated[CallToolResult, InspectResult]:
            return await _inspect(SourceRef(path=path, url=url))

        @server.tool(
            name="preview_parquet",
            title="Preview Parquet rows",
            annotations=read_only,
            description=preview_description,
        )
        async def preview_local(
            path: PathArg = None,
            url: UrlArg = None,
            columns: Columns = None,
            offset: Offset = 0,
            limit: PreviewLimit = 20,
            format: FormatArg = "json",
        ) -> Annotated[CallToolResult, TabularResult]:
            return await _preview(SourceRef(path=path, url=url), columns, offset, limit, format)

        @server.tool(
            name="query_parquet",
            title="Query Parquet with SQL",
            annotations=read_only,
            description=query_description,
        )
        async def query_local(
            sql: Sql,
            path: PathArg = None,
            url: UrlArg = None,
            limit: QueryLimit = 100,
            format: FormatArg = "json",
        ) -> Annotated[CallToolResult, TabularResult]:
            return await _query(SourceRef(path=path, url=url), sql, limit, format)

    else:

        @server.tool(
            name="inspect_parquet",
            title="Inspect Parquet schema",
            annotations=read_only,
            description=inspect_description,
        )
        async def inspect_remote(
            artifact_id: ArtifactArg = None, url: UrlArg = None
        ) -> Annotated[CallToolResult, InspectResult]:
            return await _inspect(SourceRef(artifact_id=artifact_id, url=url))

        @server.tool(
            name="preview_parquet",
            title="Preview Parquet rows",
            annotations=read_only,
            description=preview_description,
        )
        async def preview_remote(
            artifact_id: ArtifactArg = None,
            url: UrlArg = None,
            columns: Columns = None,
            offset: Offset = 0,
            limit: PreviewLimit = 20,
            format: FormatArg = "json",
        ) -> Annotated[CallToolResult, TabularResult]:
            return await _preview(
                SourceRef(artifact_id=artifact_id, url=url), columns, offset, limit, format
            )

        @server.tool(
            name="query_parquet",
            title="Query Parquet with SQL",
            annotations=read_only,
            description=query_description,
        )
        async def query_remote(
            sql: Sql,
            artifact_id: ArtifactArg = None,
            url: UrlArg = None,
            limit: QueryLimit = 100,
            format: FormatArg = "json",
        ) -> Annotated[CallToolResult, TabularResult]:
            return await _query(SourceRef(artifact_id=artifact_id, url=url), sql, limit, format)

    if quantum_available():  # keep in step with pricing.active_operations()
        _register_quantum_tools(server, service, settings, local_mode=local_mode)
    if not local_mode and service.signer is not None:
        _register_verify_tool(server, service)
    return server


QUANTUM_HONESTY = (
    "Runs on a quantum circuit SIMULATOR (PennyLane); at this size that gives no speed or "
    "accuracy advantage over classical methods, so a classical baseline is always returned too."
)


def _register_quantum_tools(
    server: MCPServer, service: Service, settings: Settings, *, local_mode: bool
) -> None:
    def price(amount: str) -> str:
        return _price_note(amount, local=local_mode)

    compress_description = f"""Premium. Reduce many numeric columns to a few dimensions with \
quantum-kernel PCA: rows are embedded by an entangling quantum feature map (IQPEmbedding), \
compared by state fidelity, and decomposed in kernel space. Writes a new Parquet artifact with \
qpc_1..k (quantum) and pc_1..k (classical PCA) side by side, plus a comparable \
neighbor-preservation score for each so you can use whichever serves you better. \
Input: artifact_id from convert_to_parquet (or a url to a .parquet). {QUANTUM_HONESTY} \
{price(settings.price_quantum_compress)}"""
    portfolio_description = f"""Premium. Pick `budget` of up to 14 assets minimizing \
risk_aversion*variance - expected return, using QAOA (an XY-mixer quantum optimization \
circuit) — and an exact classical exhaustive search, returned side by side with the \
approximation ratio. Input: a Parquet artifact whose numeric columns are asset prices (or \
returns with data_kind='returns'), rows in time order or with a date column. Use \
classical_optimum when you only need the best basket. {QUANTUM_HONESTY} \
{price(settings.price_quantum_portfolio)}"""
    compute = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    )

    async def compress(
        src: SourceRef, options: QuantumCompressOptions | None, output_path: str | None
    ) -> CallToolResult:
        async def run() -> CallToolResult:
            result = await service.quantum_compress(
                src, options or QuantumCompressOptions(), output_path=output_path
            )
            extra: list[TextContent | ResourceLink] = []
            if result.artifact.download_url:
                extra.append(
                    ResourceLink(
                        type="resource_link",
                        name=result.artifact.filename,
                        uri=result.artifact.download_url,
                        mime_type=PARQUET_MIME,
                        size=result.artifact.size_bytes,
                        description="Parquet with qpc_* (quantum) and pc_* (classical) components.",
                    )
                )
            return _ok(result, extra)

        return await _guard(run)

    async def portfolio(src: SourceRef, options: PortfolioOptions | None) -> CallToolResult:
        return await _guard(
            lambda: _wrap(service.quantum_portfolio(src, options or PortfolioOptions()))
        )

    if local_mode:

        @server.tool(
            name="quantum_compress_features",
            title="Quantum-kernel PCA (premium)",
            description=compress_description,
            annotations=compute,
        )
        async def compress_local(
            path: PathArg = None,
            url: UrlArg = None,
            options: QuantumCompressOptions | None = None,
            output_path: Annotated[
                str | None, Field(description="Where to write the output Parquet.")
            ] = None,
        ) -> Annotated[CallToolResult, QuantumCompressResult]:
            return await compress(SourceRef(path=path, url=url), options, output_path)

        @server.tool(
            name="quantum_optimize_portfolio",
            title="QAOA portfolio optimization (premium)",
            description=portfolio_description,
            annotations=compute,
        )
        async def portfolio_local(
            path: PathArg = None,
            url: UrlArg = None,
            options: PortfolioOptions | None = None,
        ) -> Annotated[CallToolResult, PortfolioResult]:
            return await portfolio(SourceRef(path=path, url=url), options)

    else:

        @server.tool(
            name="quantum_compress_features",
            title="Quantum-kernel PCA (premium)",
            description=compress_description,
            annotations=compute,
        )
        async def compress_remote(
            artifact_id: ArtifactArg = None,
            url: UrlArg = None,
            options: QuantumCompressOptions | None = None,
        ) -> Annotated[CallToolResult, QuantumCompressResult]:
            return await compress(SourceRef(artifact_id=artifact_id, url=url), options, None)

        @server.tool(
            name="quantum_optimize_portfolio",
            title="QAOA portfolio optimization (premium)",
            description=portfolio_description,
            annotations=compute,
        )
        async def portfolio_remote(
            artifact_id: ArtifactArg = None,
            url: UrlArg = None,
            options: PortfolioOptions | None = None,
        ) -> Annotated[CallToolResult, PortfolioResult]:
            return await portfolio(SourceRef(artifact_id=artifact_id, url=url), options)


class VerifyResult(BaseModel):
    valid: bool
    signature_valid: bool
    file_matches: bool | None = None
    algorithm: str
    problems: list[str]


def _register_verify_tool(server: MCPServer, service: Service) -> None:
    @server.tool(
        name="verify_provenance",
        title="Verify artifact provenance (free)",
        description="""Free. Check that a file this service produced is authentic and unaltered: \
verifies the post-quantum ML-DSA-65 (NIST FIPS 204) signature in an artifact's `provenance`, and \
with artifact_id also re-checks the stored file's hash and size against the signed manifest.""",
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    async def verify_provenance(
        manifest: Annotated[
            dict[str, Any], Field(description="provenance.manifest exactly as received.")
        ],
        signature: Annotated[
            str, Field(description="provenance.signature (base64).", max_length=8192)
        ],
        artifact_id: ArtifactArg = None,
    ) -> Annotated[CallToolResult, VerifyResult]:
        async def run() -> CallToolResult:
            return _ok(
                VerifyResult(
                    **await service.verify_provenance(manifest, signature, artifact_id=artifact_id)
                )
            )

        return await _guard(run)
