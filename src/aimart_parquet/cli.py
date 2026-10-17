"""Command line: run the paid HTTP server, the free local MCP server, or convert a file.

aimart-parquet serve              # REST + MCP over HTTP (x402-paid)
aimart-parquet mcp                # MCP over stdio, local files, free
aimart-parquet convert data.sav   # one-off conversion, prints the report
aimart-parquet inspect out.parquet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import anyio

from aimart_parquet import __version__


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from aimart_parquet.api.app import create_app

    port = args.port or int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        create_app(),
        host=args.host,
        port=port,
        proxy_headers=True,  # behind a TLS proxy the app must see https:// and the real host
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
        log_config=None,
        timeout_graceful_shutdown=10,
    )
    return 0


def _local_service() -> object:
    from aimart_parquet.artifacts import LocalArtifactStore
    from aimart_parquet.config import Settings
    from aimart_parquet.service import Service

    settings = Settings(payments_enabled=False, log_json=False)
    store = LocalArtifactStore(
        settings.artifact_dir,
        ttl_s=settings.artifact_ttl_s,
        max_total_bytes=settings.artifact_max_total_bytes,
        secret=settings.signing_secret.get_secret_value().encode(),
    )
    return Service(settings, store, local_mode=True)


def _mcp(_: argparse.Namespace) -> int:
    from aimart_parquet.config import Settings
    from aimart_parquet.mcp.server import build_mcp

    service = _local_service()
    build_mcp(service, Settings(payments_enabled=False), local_mode=True).run("stdio")  # type: ignore[arg-type]
    return 0


def _convert(args: argparse.Namespace) -> int:
    from aimart_parquet.engine.models import ConvertOptions
    from aimart_parquet.errors import AimartError
    from aimart_parquet.service import SourceRef

    options = ConvertOptions.model_validate_json(args.options) if args.options else ConvertOptions()
    service = _local_service()

    async def run() -> object:
        return await service.convert(SourceRef(path=args.input), options, output_path=args.output)  # type: ignore[attr-defined]

    try:
        result = anyio.run(run)
    except AimartError as exc:
        print(json.dumps(exc.to_dict(), indent=2), file=sys.stderr)
        return 2
    print(result.model_dump_json(indent=2, exclude_none=True))  # type: ignore[attr-defined]
    return 0


def _inspect(args: argparse.Namespace) -> int:
    from aimart_parquet.engine.inspect import inspect_parquet
    from aimart_parquet.errors import AimartError

    try:
        print(inspect_parquet(Path(args.file)).model_dump_json(indent=2, exclude_none=True))
    except AimartError as exc:
        print(json.dumps(exc.to_dict(), indent=2), file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aimart-parquet", description="Messy data in, clean Parquet out."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the HTTP server (REST + MCP, x402-paid).")
    serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))  # noqa: S104 - container default
    serve.add_argument("--port", type=int, default=None, help="Default: $PORT or 8000.")
    serve.set_defaults(func=_serve)

    mcp = sub.add_parser("mcp", help="Run the MCP server over stdio for local files (free).")
    mcp.set_defaults(func=_mcp)

    convert = sub.add_parser("convert", help="Convert one file and print the report.")
    convert.add_argument("input")
    convert.add_argument(
        "-o", "--output", help="Output .parquet path (default: next to the input)."
    )
    convert.add_argument(
        "--options", help='ConvertOptions as JSON, e.g. \'{"sort_by": ["date"]}\'.'
    )
    convert.set_defaults(func=_convert)

    inspect = sub.add_parser("inspect", help="Describe a Parquet file without reading rows.")
    inspect.add_argument("file")
    inspect.set_defaults(func=_inspect)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
