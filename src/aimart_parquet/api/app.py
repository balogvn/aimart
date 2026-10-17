"""The HTTP application: REST API + MCP Streamable HTTP + discovery, in one ASGI app.

Layering, outermost first:

    AccessLog -> RateLimit -> BodyLimit -> x402 payment middleware (REST /v1 routes only)
      -> FastAPI routes | MCP app mounted at "/" (endpoint /mcp, paid per tool via extension)

The x402 HTTP middleware is deliberately NOT in front of /mcp: initialize and tools/list must
stay free so directories and agents can discover the tools. MCP charges per tools/call instead.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from aimart_parquet import __version__
from aimart_parquet.api import discovery
from aimart_parquet.api.middleware import (
    AccessLogMiddleware,
    BodyLimitMiddleware,
    RateLimitMiddleware,
)
from aimart_parquet.api.schemas import (
    ConvertBody,
    PortfolioBody,
    PreviewBody,
    QuantumCompressBody,
    QueryBody,
    SourceBody,
    VerifyBody,
)
from aimart_parquet.artifacts import ArtifactStore, build_store
from aimart_parquet.config import Settings, get_settings
from aimart_parquet.engine.models import ConvertOptions, ConvertResult, InspectResult, TabularResult
from aimart_parquet.errors import AimartError, ArtifactNotFound, InputTooLarge, InvalidRequest
from aimart_parquet.logs import configure as configure_logging
from aimart_parquet.mcp.server import build_mcp
from aimart_parquet.payments.pricing import BY_KEY
from aimart_parquet.provenance import Signer
from aimart_parquet.quantum import available as quantum_available
from aimart_parquet.quantum.models import PortfolioResult, QuantumCompressResult
from aimart_parquet.service import Service, SourceRef

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aimart_parquet.payments import Payments

log = logging.getLogger("aimart")

PARQUET_MIME = "application/vnd.apache.parquet"
MCP_BODY_LIMIT = 16 * 1024 * 1024
JSON_BODY_LIMIT = 16 * 1024 * 1024
SWEEP_INTERVAL_S = 60


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    host = urlsplit(settings.public_base_url).netloc
    hosts = {host, f"{host.split(':')[0]}:*", "localhost:*", "127.0.0.1:*", "[::1]:*"}
    origins = {settings.public_base_url, "http://localhost:*", "http://127.0.0.1:*"}
    return TransportSecuritySettings(allowed_hosts=sorted(hosts), allowed_origins=sorted(origins))


def create_app(
    settings: Settings | None = None, *, payments: Payments | Literal[False] | None = None
) -> FastAPI:
    """Build the app. ``payments=None`` builds them from settings, ``False`` disables them."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    store = build_store(settings)
    signer = Signer.from_b64(
        settings.signing_key_mldsa.get_secret_value() if settings.signing_key_mldsa else None
    )
    service = Service(settings, store, local_mode=False, signer=signer)

    def paid(key: str) -> dict[str, Any]:
        """OpenAPI x-payment-info: how x402scan and agents learn the price from the schema."""
        price = BY_KEY[key].price(settings).removeprefix("$")
        return {
            "x-payment-info": {
                "price": {"mode": "fixed", "currency": "USD", "amount": price},
                "protocols": [{"x402": {"version": 2}}],
            },
            "responses": {
                "402": {"description": "Payment Required: see the PAYMENT-REQUIRED header."}
            },
        }

    free: dict[str, Any] = {"security": []}

    pay: Payments | None = None
    if payments is None and settings.payments_enabled:
        from aimart_parquet.payments import Payments as _Payments  # noqa: PLC0415 - x402 is heavy; skip when unpaid

        pay = _Payments.from_settings(settings)
    elif payments:
        pay = payments

    mcp = build_mcp(
        service, settings, local_mode=False, extensions=[pay.mcp_extension()] if pay else []
    )
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(settings),
        max_request_body_size=MCP_BODY_LIMIT,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run(), anyio.create_task_group() as tg:
            tg.start_soon(_sweeper, store)
            log.info(
                "started",
                extra={
                    "version": __version__,
                    "network_mode": settings.network_mode.value,
                    "payments": bool(pay),
                    "base_url": settings.public_base_url,
                },
            )
            yield
            tg.cancel_scope.cancel()
        if pay is not None:
            await pay.aclose()

    app = FastAPI(
        title=settings.service_name,
        version=__version__,
        summary="Messy data in, clean Parquet out. Paid per call by AI agents via x402.",
        description=discovery.DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.payments = pay

    # ------------------------------------------------------------------ errors
    @app.exception_handler(AimartError)
    async def _aimart_error(_: Request, exc: AimartError) -> JSONResponse:
        return JSONResponse(exc.to_dict(), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            {
                "field": ".".join(str(p) for p in e.get("loc", ()) if p != "body"),
                "problem": e.get("msg"),
            }
            for e in exc.errors()[:20]
        ]
        err = InvalidRequest(
            "The request body is invalid.", hint="Fix the listed fields.", problems=problems
        )
        return JSONResponse(err.to_dict(), status_code=400)

    # ------------------------------------------------------------------ free routes
    networks = pay.networks() if pay else []

    @app.get("/", include_in_schema=False)
    async def index() -> dict[str, Any]:
        return discovery.service_index(settings)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/pricing", summary="Per-call prices (free)")
    async def pricing() -> dict[str, Any]:
        return discovery.pricing(settings, networks)

    @app.get("/llms.txt", include_in_schema=False, response_class=PlainTextResponse)
    async def llms() -> str:
        return discovery.llms_txt(settings)

    @app.get("/.well-known/x402", include_in_schema=False)
    async def well_known_x402() -> dict[str, Any]:
        return pay.discovery_document() if pay else {"x402Version": 2, "resources": []}

    @app.get("/.well-known/mcp/server-card.json", include_in_schema=False)
    async def server_card() -> dict[str, Any]:
        return discovery.mcp_server_card(settings)

    @app.get(
        "/.well-known/aimart-signing-key",
        summary="Post-quantum (ML-DSA-65) public key for artifact provenance (free)",
        openapi_extra=free,
    )
    async def signing_key() -> dict[str, Any]:
        return signer.public_document(settings.public_base_url)

    @app.post(
        "/v1/verify", summary="Verify an artifact's provenance signature (free)", openapi_extra=free
    )
    async def verify(body: VerifyBody) -> dict[str, Any]:
        return await service.verify_provenance(
            body.manifest, body.signature, artifact_id=body.artifact_id
        )

    @app.get(
        "/v1/artifacts/{artifact_id}/{filename}",
        summary="Download a converted Parquet file (free, signed link)",
        response_class=FileResponse,
        openapi_extra=free,
    )
    async def download(
        artifact_id: str,
        filename: str,
        exp: Annotated[int, Query()],
        sig: Annotated[str, Query(max_length=64)],
    ) -> FileResponse:
        if not store.verify(artifact_id, exp, sig):
            raise ArtifactNotFound(
                "This download link is invalid or has expired.", hint="Convert the file again."
            )
        art, path = store.get(artifact_id)
        return FileResponse(path, media_type=PARQUET_MIME, filename=art.filename)

    # ------------------------------------------------------------------ paid routes
    @app.post(
        "/v1/convert",
        summary="Convert a file (URL or base64) to Parquet",
        response_model=ConvertResult,
        openapi_extra=paid("convert"),
    )
    async def convert(body: ConvertBody) -> ConvertResult:
        return await service.convert(body.source(), body.options)

    @app.post(
        "/v1/convert/upload",
        summary="Convert an uploaded file to Parquet",
        response_model=ConvertResult,
        openapi_extra=paid("convert_upload"),
    )
    async def convert_upload(
        file: Annotated[UploadFile, File(description="The data file.")],
        options: Annotated[str | None, Form(description="ConvertOptions as a JSON string.")] = None,
    ) -> ConvertResult:
        try:
            parsed = ConvertOptions.model_validate_json(options) if options else ConvertOptions()
        except ValidationError as exc:
            raise InvalidRequest(
                "options is not valid ConvertOptions JSON.", hint=str(exc.errors()[:3])
            ) from exc
        with tempfile.TemporaryDirectory(dir=settings.artifact_dir) as tmp:
            dest = Path(tmp) / "upload"
            size = 0
            with dest.open("wb") as out:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_input_bytes:
                        raise InputTooLarge(
                            f"Upload exceeds {settings.max_input_bytes // (1024 * 1024)} MB.",
                            hint="Split the file or pass a url.",
                        )
                    out.write(chunk)
            return await service.convert(SourceRef(upload=dest, filename=file.filename), parsed)

    @app.post(
        "/v1/inspect",
        summary="Describe a Parquet file without reading rows",
        response_model=InspectResult,
        openapi_extra=paid("inspect"),
    )
    async def inspect(body: SourceBody) -> InspectResult:
        return await service.inspect(body.source())

    @app.post(
        "/v1/preview",
        summary="Return a slice of Parquet rows",
        response_model=TabularResult,
        openapi_extra=paid("preview"),
    )
    async def preview(body: PreviewBody) -> TabularResult:
        return await service.preview(
            body.source(),
            columns=body.columns,
            offset=body.offset,
            limit=body.limit,
            fmt=body.format,
        )

    @app.post(
        "/v1/query",
        summary="Run one read-only SQL SELECT over a Parquet file",
        response_model=TabularResult,
        openapi_extra=paid("query"),
    )
    async def query(body: QueryBody) -> TabularResult:
        return await service.query(
            {"data": body.source()}, body.sql, limit=body.limit, fmt=body.format
        )

    if quantum_available():  # keep in step with pricing.active_operations()

        @app.post(
            "/v1/quantum/compress",
            summary="Premium: quantum-kernel PCA with a classical PCA baseline (simulated)",
            response_model=QuantumCompressResult,
            openapi_extra=paid("quantum_compress"),
        )
        async def quantum_compress(body: QuantumCompressBody) -> QuantumCompressResult:
            return await service.quantum_compress(body.source(), body.options)

        @app.post(
            "/v1/quantum/portfolio",
            summary="Premium: QAOA portfolio selection verified by an exact classical solver (simulated)",
            response_model=PortfolioResult,
            openapi_extra=paid("quantum_portfolio"),
        )
        async def quantum_portfolio(body: PortfolioBody) -> PortfolioResult:
            return await service.quantum_portfolio(body.source(), body.options)

    if pay is not None:
        app.middleware("http")(pay.http_middleware())

    app.mount("/", mcp_app)  # after every route: a "/" mount swallows anything declared later

    app.add_middleware(
        BodyLimitMiddleware,
        default_limit=JSON_BODY_LIMIT,
        upload_limit=settings.max_input_bytes + 1024 * 1024,
        upload_paths=("/v1/convert/upload",),
    )
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=settings.rate_limit_per_minute,
        trust_forwarded_for=settings.trust_forwarded_for,
    )
    app.add_middleware(AccessLogMiddleware)
    return app


async def _sweeper(store: ArtifactStore) -> None:
    while True:
        await anyio.sleep(SWEEP_INTERVAL_S)
        try:
            removed = await anyio.to_thread.run_sync(store.sweep)
            if removed:
                log.info("artifacts swept", extra={"removed": removed})
        except Exception:  # the sweeper must outlive any single bad directory
            log.exception("artifact sweep failed")
