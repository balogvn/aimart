"""Turn a caller's input reference into a file inside the job's work directory.

Inputs arrive as exactly one of: an HTTPS/HTTP ``url``, inline ``content_base64``, an
``artifact_id`` from an earlier call, an uploaded file (REST multipart), or — local stdio mode
only — a filesystem ``path``. A remote caller can never name a server path.

URL fetching is the dangerous part. This is a paid service an anonymous agent can point at any
URL, so it must not become a proxy into the host's private network (cloud metadata at
169.254.169.254, localhost admin ports, the VPC). The guard is enforced at connect time, not
just at parse time: every hostname is resolved, every resolved address must be globally
routable, and the socket connects to the address that was checked — so DNS rebinding between
check and connect is impossible. Redirects re-enter the same path, so each hop is re-checked.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlsplit

import anyio
import httpcore

from aimart_parquet import __version__
from aimart_parquet.errors import FetchFailed, InputTooLarge, InvalidRequest, UrlNotAllowed

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_REDIRECTS = 5
_CHUNK = 256 * 1024
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")  # deprecated ::a.b.c.d form; is_global lies here
# Data lives on web ports. Refusing the rest stops the fetcher being aimed at third parties'
# SMTP, Redis or SSH on public addresses.
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses, unwrapping IPv6 encapsulations."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_is_public(ip.ipv4_mapped)
        if ip in _NAT64 or ip in _IPV4_COMPATIBLE:
            return ip_is_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.sixtofour is not None:
            return ip_is_public(ip.sixtofour)
        if ip.teredo is not None:
            return False
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
    )


class GuardedBackend(httpcore.AnyIOBackend):
    """Resolve, validate, and connect to the validated address — in one step."""

    def __init__(self, *, allow_private: bool = False) -> None:
        self.allow_private = allow_private

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's signature, not ours
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UrlNotAllowed(f"Could not resolve host {host!r}.", hint="Check the URL.") from exc
        addresses: list[str] = []
        for _family, _type, _proto, _canon, sockaddr in infos:
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
            if not self.allow_private and not ip_is_public(ip):
                raise UrlNotAllowed(
                    f"Host {host!r} resolves to a non-public address.",
                    hint="Only publicly reachable http(s) URLs can be fetched.",
                )
            addresses.append(str(ip))
        if not addresses:
            raise UrlNotAllowed(f"Host {host!r} has no addresses.", hint="Check the URL.")
        last_error: Exception | None = None
        for address in dict.fromkeys(addresses):
            try:
                # TLS still verifies against the original hostname: httpcore passes the URL's
                # host as server_hostname to start_tls, independent of the address dialled.
                return await super().connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise FetchFailed(
            f"Could not connect to {host!r}: {last_error}", hint="Check the URL is up."
        )


@dataclass(frozen=True)
class Fetched:
    path: Path
    filename: str | None
    content_type: str | None
    size: int


def _filename_from(url: str, disposition: str | None) -> str | None:
    if disposition:
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.IGNORECASE)
        if match:
            return safe_filename(unquote(match.group(1)))
    name = PurePosixPath(unquote(urlsplit(url).path)).name
    return safe_filename(name) if name else None


def safe_filename(name: str) -> str:
    """Keep only the final path component, in a conservative alphabet."""
    base = PurePosixPath(name.replace("\\", "/")).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "input"
    return cleaned[:120]


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UrlNotAllowed("Only http and https URLs are supported.", hint="Pass an https:// URL.")
    if not parts.hostname:
        raise UrlNotAllowed("The URL has no host.", hint="Pass a full https:// URL.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UrlNotAllowed("The URL has an invalid port.", hint="Check the URL.") from exc
    if port not in ALLOWED_PORTS:
        raise UrlNotAllowed(
            f"Port {port} is not allowed.",
            hint=f"Use a URL on one of ports {sorted(ALLOWED_PORTS)}.",
        )


async def fetch_url(
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    deadline_s: float,
    allow_private: bool = False,
) -> Fetched:
    ssl_context = ssl.create_default_context()
    headers = [
        (
            b"User-Agent",
            f"aimart-parquet/{__version__} (+https://github.com/balogvn/aimart)".encode(),
        ),
        (b"Accept", b"*/*"),
        (b"Accept-Encoding", b"identity"),
    ]
    current = url
    try:
        with anyio.fail_after(deadline_s):
            async with httpcore.AsyncConnectionPool(
                ssl_context=ssl_context,
                network_backend=GuardedBackend(allow_private=allow_private),
                max_connections=4,
                retries=0,
            ) as pool:
                for _hop in range(MAX_REDIRECTS + 1):
                    _validate_url(current)
                    async with pool.stream("GET", current, headers=headers) as resp:
                        if resp.status in {301, 302, 303, 307, 308}:
                            location = _header(resp.headers, b"location")
                            if not location:
                                raise FetchFailed(
                                    "Redirect without a Location header.", hint="Check the URL."
                                )
                            current = urljoin(current, location.decode("latin-1"))
                            continue
                        if resp.status >= 400:
                            raise FetchFailed(
                                f"The URL returned HTTP {resp.status}.",
                                hint="Check the URL is public and the file exists (no login required).",
                                status=resp.status,
                            )
                        length = _header(resp.headers, b"content-length")
                        if length and length.isdigit() and int(length) > max_bytes:
                            raise _too_large(max_bytes)
                        size = 0
                        with dest.open("wb") as out:
                            async for chunk in resp.aiter_stream():
                                size += len(chunk)
                                if size > max_bytes:
                                    raise _too_large(max_bytes)
                                out.write(chunk)
                        disposition = _header(resp.headers, b"content-disposition")
                        ctype = _header(resp.headers, b"content-type")
                        return Fetched(
                            path=dest,
                            filename=_filename_from(
                                current, disposition.decode("latin-1") if disposition else None
                            ),
                            content_type=ctype.decode("latin-1") if ctype else None,
                            size=size,
                        )
                raise FetchFailed(
                    f"More than {MAX_REDIRECTS} redirects.", hint="Pass the final URL."
                )
    except TimeoutError as exc:
        raise FetchFailed(
            f"Downloading the URL took longer than {deadline_s:g} s.",
            hint="Use a faster host or a smaller file.",
        ) from exc
    except (
        httpcore.ProtocolError,
        httpcore.NetworkError,
        httpcore.UnsupportedProtocol,
        ssl.SSLError,
    ) as exc:
        raise FetchFailed(
            f"Could not download the URL: {exc}", hint="Check the URL and its TLS certificate."
        ) from exc


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def _too_large(max_bytes: int) -> InputTooLarge:
    return InputTooLarge(
        f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit.",
        hint="Send a smaller extract or split the file.",
        limit_bytes=max_bytes,
    )


def decode_base64(data: str, dest: Path, *, max_bytes: int) -> int:
    # base64 inflates by 4/3; refuse before decoding rather than after allocating.
    if len(data) > max_bytes * 4 // 3 + 4:
        raise _too_large(max_bytes)
    payload = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise InvalidRequest(
            "content_base64 is not valid base64.", hint="Base64-encode the raw file bytes."
        ) from exc
    if len(raw) > max_bytes:
        raise _too_large(max_bytes)
    dest.write_bytes(raw)
    return len(raw)
