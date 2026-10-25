"""The HTTP application: REST API + MCP Streamable HTTP + discovery, in one ASGI app.

Layering, outermost first:

    AccessLog -> RateLimit -> BodyLimit -> x402 payment middleware (REST /v1 routes only)
      -> FastAPI routes | MCP app mounted at "/" (endpoint /mcp, paid per tool via extension)

The x402 HTTP middleware is deliberately NOT in front of /mcp: initialize and tools/list must
stay free so directories and agents can discover the tools. MCP charges per tools/call instead.

A paid request's body is validated before the x402 middleware quotes a price (``_body_check``):
FastAPI only validates inside the route, after the 402, so an agent signed a payment for a
request that could only fail. MCP does the same through ``argument_checker``. The upload
form's small fields are read for the same check by ``FormPeekMiddleware`` (``_form_check``),
which sits outside the payment middleware so the file itself still streams to the route.
"""

from __future__ import annotations

import contextlib
import email.message
import hmac
import json
import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi import params as fastapi_params
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.routing import APIRoute
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ValidationError
from starlette.requests import ClientDisconnect

from aimart_parquet import __version__
from aimart_parquet.api import discovery
from aimart_parquet.api.middleware import (
    FORM_TYPES,
    AccessLogMiddleware,
    BodyLimitMiddleware,
    FormPeek,
    FormPeekMiddleware,
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
from aimart_parquet.demand import CapabilityAck, CapabilityRequest, DemandSignals
from aimart_parquet.engine.models import ConvertOptions, ConvertResult, InspectResult, TabularResult
from aimart_parquet.errors import AimartError, ArtifactNotFound, InvalidRequest
from aimart_parquet.logs import configure as configure_logging
from aimart_parquet.mcp.server import argument_checker, build_mcp
from aimart_parquet.payments.pricing import BY_KEY, quantum_enabled
from aimart_parquet.provenance import Signer
from aimart_parquet.quantum.models import PortfolioResult, QuantumCompressResult
from aimart_parquet.service import Service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping

    from starlette.types import Scope

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


def _invalid_body(errors: Iterable[Mapping[str, Any]]) -> JSONResponse:
    """The one 400 for a body that fails validation, before payment or inside the route."""
    problems = [
        {
            "field": ".".join(str(p) for p in e.get("loc", ()) if p != "body"),
            "problem": e.get("msg"),
        }
        for e in list(errors)[:20]
    ]
    err = InvalidRequest(
        "The request body is invalid.", hint="Fix the listed fields.", problems=problems
    )
    return JSONResponse(err.to_dict(), status_code=400)


def _is_json(content_type: str) -> bool:
    message = email.message.Message()  # parsed exactly as FastAPI decides to read JSON
    message["content-type"] = content_type
    subtype = message.get_content_subtype()
    return message.get_content_maintype() == "application" and (
        subtype == "json" or subtype.endswith("+json")
    )


def _price_probe(raw: bytes) -> bool:
    """No input at all: how x402scan's crawler and `make smoke` ask a paid route for its price.
    Answering it 400 instead of 402 would silently drop the route from discovery."""
    return raw.strip() in {b"", b"{}"}


def _form_routes(app: FastAPI) -> dict[tuple[str, str], list[tuple[str, bool]]]:
    """(method, path) of every form route -> its required parts as (name, is a file)."""
    forms: dict[tuple[str, str], list[tuple[str, bool]]] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.body_field is None:
            continue
        body_params = route.dependant.body_params
        if not any(isinstance(p.field_info, fastapi_params.Form) for p in body_params):
            continue
        required = [
            (p.alias, isinstance(p.field_info, fastapi_params.File))
            for p in body_params
            if p.field_info.is_required()
        ]
        for method in route.methods or ():
            forms[(method, route.path)] = required
    return forms


def _convert_options(options: str | None) -> ConvertOptions:
    """The upload form's ``options`` field, as the route parses it (and the check before it)."""
    try:
        return ConvertOptions.model_validate_json(options) if options else ConvertOptions()
    except ValidationError as exc:
        raise InvalidRequest(
            "options is not valid ConvertOptions JSON.", hint=str(exc.errors()[:3])
        ) from exc


# Form fields a route parses itself (to FastAPI they are plain strings), by route path.
_FORM_FIELDS: dict[str, dict[str, Callable[[str | None], object]]] = {
    BY_KEY["convert_upload"].path: {"options": _convert_options},
}


def _form_check(
    app: FastAPI, paying: Callable[[Scope], bool]
) -> tuple[tuple[str, ...], Callable[[Scope, FormPeek], JSONResponse | None]]:
    """The form paths, and what their routes would refuse, decided from ``FormPeekMiddleware``.

    Without it every upload reached the payment middleware unread: invalid ``options`` or no
    ``file`` part was quoted (402), and the paid retry verified, before the route's 400. An
    invalid field or a malformed body is refused for anyone. A missing part only for a paid
    request whose whole body was read: an unpaid form without its file is how catalogs replay
    the advertised example to learn the price, so it still gets its 402.
    """
    forms = _form_routes(app)

    def check(scope: Scope, peek: FormPeek) -> JSONResponse | None:
        if peek.error is not None:
            return _invalid_body([{"loc": (), "msg": peek.error}])
        for name, parse in _FORM_FIELDS.get(scope["path"], {}).items():
            try:
                parse(peek.fields.get(name))
            except AimartError as exc:
                return JSONResponse(exc.to_dict(), status_code=exc.http_status)
        missing = [
            name
            for name, is_file in forms.get((scope["method"], scope["path"]), [])
            if name not in (peek.files if is_file else peek.fields)
        ]
        if missing and peek.complete and paying(scope):
            return _invalid_body({"loc": (name,), "msg": "Field required"} for name in missing)
        return None

    return tuple(sorted({path for method, path in forms if method == "POST"})), check


def _body_check(app: FastAPI) -> Callable[[Request, bool], Awaitable[JSONResponse | None]]:
    """Validate a request body against its route's own model, before the payment middleware.

    The models come from the routes as declared, so this cannot drift from what the route will
    accept; the route still validates again, so this only ever rejects earlier what the route
    would reject anyway. Mirrors FastAPI's reading of the body (JSON only for a JSON or absent
    content type; an empty body is a missing one). An unpaid price probe is let through to its
    402. A form body is not read here: ``_form_check`` saw it already, before this middleware.
    """
    models: dict[tuple[str, str], type[BaseModel]] = {}
    forms = {key: [name for name, _ in parts] for key, parts in _form_routes(app).items()}
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.body_field is None:
            continue
        body_params = route.dependant.body_params
        for method in route.methods or ():
            key = (method, route.path)
            annotation = route.body_field.field_info.annotation
            if (
                key not in forms
                and len(body_params) == 1
                and isinstance(annotation, type)
                and issubclass(annotation, BaseModel)
            ):
                models[key] = annotation

    async def check(request: Request, paying: bool) -> JSONResponse | None:
        key = (request.method, request.url.path)
        if key in forms:
            if request.headers.get("content-type", "").startswith(FORM_TYPES):
                return None
            length = request.headers.get("content-length")
            if length is None:
                # Neither Content-Length nor chunked: HTTP/1.1 has no body (RFC 9112 6.3), as
                # in `curl -X POST <route>`. Treated as a large body, that probe got a 400.
                tiny = "transfer-encoding" not in request.headers
            else:
                tiny = length.isdigit() and int(length) <= 16  # never buffer a real upload here
            if not paying and tiny and _price_probe(await request.body()):
                return None
            return _invalid_body({"loc": (name,), "msg": "Field required"} for name in forms[key])
        model = models.get(key)
        if model is None:
            return None
        try:
            raw = await request.body()  # Starlette replays it to the route
        except ClientDisconnect:
            return None  # over the body limit: BodyLimitMiddleware answers 413
        if not paying and _price_probe(raw):
            return None
        if not raw:
            return _invalid_body([{"loc": (), "msg": "Field required"}])
        value: Any = raw
        declared = request.headers.get("content-type")
        if declared is None or _is_json(declared):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                return _invalid_body([{"loc": (exc.pos,), "msg": "JSON decode error"}])
            except ValueError:
                return None  # not UTF-8: FastAPI words this one itself
        try:
            model.model_validate(value, from_attributes=True)
        except ValidationError as exc:
            return _invalid_body(cast("list[Mapping[str, Any]]", exc.errors(include_url=False)))
        return None

    return check


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
    demand = DemandSignals()
    service = Service(settings, store, local_mode=False, signer=signer, demand=demand)

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

    gate = pay.mcp_extension(store=store) if pay else None
    mcp = build_mcp(service, settings, local_mode=False, extensions=[gate] if gate else [])
    if gate is not None:
        gate.check_arguments = argument_checker(mcp)
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
        return _invalid_body(exc.errors())

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
        return await service.convert_upload(file.read, file.filename, _convert_options(options))

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
            body.table_sources(), body.sql, limit=body.limit, fmt=body.format
        )

    @app.post(
        "/v1/feedback",
        summary="Tell us what you needed and would pay for (free)",
        openapi_extra=free,
    )
    async def feedback(body: CapabilityRequest) -> CapabilityAck:
        return demand.capability_request(body)

    @app.get("/v1/demand", include_in_schema=False)
    async def demand_report(request: Request) -> dict[str, Any]:
        token = settings.admin_token.get_secret_value() if settings.admin_token else ""
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        # Off unless a token is configured; constant-time compare; 404 either way on failure,
        # so the endpoint's existence is not advertised.
        if not token or not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=404, detail="Not Found")
        return demand.snapshot()

    if quantum_enabled(settings):  # keep in step with pricing.active_operations()

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
        from aimart_parquet.payments import carries_payment  # noqa: PLC0415 - loaded with pay

        app.middleware("http")(pay.http_middleware(prevalidate=_body_check(app), store=store))
        form_paths, form_check = _form_check(app, lambda scope: carries_payment(Request(scope)))
        # Added after the payment middleware, so it runs before it (and inside BodyLimit).
        app.add_middleware(FormPeekMiddleware, paths=form_paths, check=form_check)

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
        trusted_hops=settings.trusted_proxy_hops,
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
