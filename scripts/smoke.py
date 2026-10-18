#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx==0.28.1"]
# ///
"""Smoke-test a live AImart Parquet deployment by checking the VALUES it serves.

    uv run scripts/smoke.py https://host --expect-pay-to 0x... --expect-network eip155:84532

An HTTP 200 proves a process is listening; it does not prove that a caller who pays is paying the
right amount, in the right token, on the right chain, to the right address. So every paid route
and every paid MCP tool is called unpaid, the base64 ``PAYMENT-REQUIRED`` challenge is decoded, and
each ``accepts[]`` entry is compared with:

* ``/pricing``, the deployment's own price list (one source of truth for what we advertise), with
  dollar prices converted to USDC atomic units by an independent implementation (``Decimal``
  arithmetic here, string splitting in the package), so one bug cannot hide in both;
* the payee and network given on the command line (the deployer's intent), compared strictly: an
  extra network or payee in the challenge is a failure, because an unexpected ``payTo`` is revenue
  going somewhere else;
* the canonical USDC contract for that network, hard-coded below: the right amount of the wrong
  token is worthless.

This script deliberately does not import ``aimart_parquet``: a smoke test must test the
deployment, not re-run the code under test against itself. It needs only the stdlib and httpx,
and prints nothing that is not already public (prices, addresses, URLs).

Exit status: 0 every check passed; 1 at least one check failed; 2 the target never became
healthy, or the arguments were invalid.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

USER_AGENT = "aimart-smoke/1 (+https://github.com/balogvn/aimart)"
MCP_PROTOCOL_VERSION = "2025-11-25"  # what we offer; the server's answer is what we then send
MAX_TOOL_PAGES = 50  # tools/list pagination guard: exceeding it FAILS, it never truncates
USDC_DECIMALS = 6
ML_DSA_65_PUBLIC_KEY_BYTES = 1952  # FIPS 204, table 2

# Canonical USDC per CAIP-2 network (Circle's deployments). EVM entries also pin the EIP-712
# domain (name, version): with the wrong name, every payer's signature fails verification.
USDC: dict[str, dict[str, str]] = {
    "eip155:84532": {
        "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "name": "USDC",
        "version": "2",
    },
    "eip155:8453": {
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "name": "USD Coin",
        "version": "2",
    },
    "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1": {
        "asset": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
    },
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp": {
        "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    },
}
MAINNETS = frozenset({"eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"})

# Operations every deployment must sell. Others (e.g. the quantum tier) are checked automatically
# whenever /pricing lists them; --require-operation makes their presence mandatory as well.
CORE_OPERATIONS = ("convert", "convert_upload", "inspect", "preview", "query")

# Other spellings of a paid path must be paywalled or unroutable, never served unpaid: a paywall
# keyed on an exact string can be walked around by a router that normalizes more liberally.
VARIANT_OK_STATUSES = frozenset({402, 404, 405, 307, 308})


def path_variants(path: str) -> list[str]:
    head, _, last = path.rpartition("/")
    encoded = f"{head}/%{ord(last[0]):02x}{last[1:]}" if last else path
    return [f"{path}/", encoded]


# ------------------------------------------------------------------------------- money maths
def usd_to_atomic(price: str, decimals: int = USDC_DECIMALS) -> int:
    """'$0.01' -> 10000. Decimal arithmetic on purpose: independent of the package's code."""
    text = price.strip()
    if not re.fullmatch(r"\$?\d+(\.\d+)?", text):
        msg = f"not a dollar price: {price!r}"
        raise ValueError(msg)
    try:
        scaled = Decimal(text.removeprefix("$")).scaleb(decimals)
    except InvalidOperation as exc:
        msg = f"not a dollar price: {price!r}"
        raise ValueError(msg) from exc
    if scaled != scaled.to_integral_value():
        msg = f"{price} is finer than {decimals} decimals"
        raise ValueError(msg)
    return int(scaled)


def same_address(network: str, a: str, b: str) -> bool:
    # EVM hex addresses are case-insensitive (case is only a checksum); base58 is not.
    return a.lower() == b.lower() if network.startswith("eip155:") else a == b


def decode_payment_required(header: str) -> dict[str, Any]:
    data = json.loads(base64.b64decode(header + "=" * (-len(header) % 4)))
    if not isinstance(data, dict):
        msg = "PAYMENT-REQUIRED is not a JSON object"
        raise TypeError(msg)
    return data


class PatientTransport(httpx.HTTPTransport):
    """Waits out HTTP 429 instead of reporting it as a failure.

    The service rate-limits per client IP, and one smoke run sends ~50 requests; two runs from
    the same CI runner within a minute (candidate revision, then live) can exhaust the bucket.
    A 429 says nothing about prices or payees, so honour Retry-After and ask again (bounded).
    """

    max_wait_s = 90.0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        waited = 0.0
        while True:
            response = super().handle_request(request)
            if response.status_code != 429 or waited >= self.max_wait_s:
                return response
            response.read()
            response.close()
            try:
                delay = min(max(float(response.headers.get("retry-after", "2")), 0.5), 30.0)
            except ValueError:
                delay = 2.0
            print(f"WAIT  429 from {request.url.path}: retrying in {delay:.0f}s", flush=True)
            time.sleep(delay)
            waited += delay


# ------------------------------------------------------------------------------- reporting
@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    passes: int = 0
    skips: int = 0
    lines: list[tuple[str, str]] = field(default_factory=list)

    def _emit(self, kind: str, msg: str) -> None:
        self.lines.append((kind, msg))
        print(f"{kind:<5} {msg}", flush=True)

    def ok(self, msg: str) -> None:
        self.passes += 1
        self._emit("PASS", msg)

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        self._emit("FAIL", msg)

    def skip(self, msg: str) -> None:
        self.skips += 1
        self._emit("SKIP", msg)

    def note(self, msg: str) -> None:
        self._emit("NOTE", msg)

    def check(self, cond: bool, msg: str, detail: str = "") -> bool:
        if cond:
            self.ok(msg)
        else:
            self.fail(f"{msg}: {detail}" if detail else msg)
        return cond

    def write_github_summary(self, target: str) -> None:
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not path:
            return
        verdict = "passed" if not self.failures else f"FAILED ({len(self.failures)} checks)"
        out = [f"### Smoke test {verdict}: `{target}`", "", "| result | check |", "|---|---|"]
        out += [f"| {kind} | {msg.replace('|', '/')} |" for kind, msg in self.lines]
        with Path(path).open("a", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n\n")


@dataclass
class Expect:
    base_url: str  # the origin the deployment should advertise in 402s, links and MCP urls
    accepts: dict[str, str]  # network -> payTo
    prices: dict[str, str]  # operation -> "$x" (optional pins)
    required_ops: set[str]
    version: str | None
    require_provenance: bool
    key_sha256: str | None


# ------------------------------------------------------------------------------- payment checks
def check_entry(report: Report, tag: str, entry: dict[str, Any], want: int, expect: Expect) -> None:
    network = str(entry.get("network"))
    report.check(entry.get("scheme") == "exact", f"{tag}: scheme exact", str(entry.get("scheme")))
    amount = str(entry.get("amount"))
    report.check(amount == str(want), f"{tag}: amount {amount} == {want}", f"got {amount}")
    pay_to = str(entry.get("payTo"))
    report.check(
        same_address(network, pay_to, expect.accepts[network]),
        f"{tag}: payTo is the expected address",
        f"got {pay_to}, expected {expect.accepts[network]}",
    )
    canon = USDC[network]
    asset = str(entry.get("asset"))
    report.check(
        same_address(network, asset, canon["asset"]),
        f"{tag}: asset is canonical USDC",
        f"got {asset}, expected {canon['asset']}",
    )
    extra = entry.get("extra") or {}
    if network.startswith("eip155:"):
        report.check(
            extra.get("name") == canon["name"] and extra.get("version") == canon["version"],
            f"{tag}: EIP-712 domain ({canon['name']!r}, {canon['version']!r})",
            f"got ({extra.get('name')!r}, {extra.get('version')!r})",
        )
    else:
        report.check(bool(extra.get("feePayer")), f"{tag}: Solana feePayer present")
    timeout = entry.get("maxTimeoutSeconds")
    report.check(
        isinstance(timeout, int) and 0 < timeout <= 3600,
        f"{tag}: maxTimeoutSeconds sane ({timeout})",
        str(timeout),
    )


def check_accepts(report: Report, where: str, accepts: Any, price: str, expect: Expect) -> None:
    """Validate one PaymentRequired.accepts[] list against what the deployer intended."""
    if not isinstance(accepts, list) or not accepts:
        report.fail(f"{where}: accepts[] is empty or missing")
        return
    try:
        want = usd_to_atomic(price)
    except ValueError as exc:
        report.fail(f"{where}: /pricing price unusable: {exc}")
        return
    report.check(want > 0, f"{where}: {price} is a positive amount", "this operation is free")
    seen: set[str] = set()
    for entry in accepts:
        network = str(entry.get("network")) if isinstance(entry, dict) else "?"
        if network not in expect.accepts:
            # Strict: an unexpected payee is revenue going somewhere else.
            report.fail(f"{where}: unexpected accepts[] entry {json.dumps(entry)[:200]}")
            continue
        seen.add(network)
        check_entry(report, f"{where} [{network}]", entry, want, expect)
    missing = sorted(set(expect.accepts) - seen)
    report.check(not missing, f"{where}: every expected network offered", f"missing {missing}")


def check_paid_route(
    client: httpx.Client, base: str, op: dict[str, Any], expect: Expect, report: Report
) -> None:
    http = str(op.get("http", ""))
    method, _, path = http.partition(" ")
    if method != "POST" or not path.startswith("/"):
        report.fail(f"/pricing {op.get('operation')}: unusable http route {http!r}")
        return
    r = client.post(f"{base}{path}", json={})
    if r.status_code in {429, 503}:
        report.fail(f"{http}: HTTP {r.status_code} {r.text[:200]} (rate limited / facilitator?)")
        return
    if not report.check(
        r.status_code == 402, f"{http}: unpaid call answers 402", f"HTTP {r.status_code}"
    ):
        return
    header = r.headers.get("payment-required")
    if not report.check(bool(header), f"{http}: PAYMENT-REQUIRED header present"):
        return
    try:
        pr = decode_payment_required(str(header))
    except (ValueError, TypeError, binascii.Error) as exc:
        report.fail(f"{http}: PAYMENT-REQUIRED does not decode: {exc}")
        return
    report.check(pr.get("x402Version") == 2, f"{http}: x402Version 2", str(pr.get("x402Version")))
    resource = (pr.get("resource") or {}).get("url")
    report.check(
        resource == f"{expect.base_url}{path}",
        f"{http}: resource url {expect.base_url}{path}",
        f"got {resource} (is AIMART_PUBLIC_BASE_URL right?)",
    )
    check_accepts(report, http, pr.get("accepts"), str(op.get("price")), expect)
    for variant in path_variants(path):
        vr = client.post(f"{base}{variant}", json={})
        report.check(
            vr.status_code in VARIANT_OK_STATUSES,
            f"POST {variant}: not served unpaid (HTTP {vr.status_code})",
            vr.text[:160],
        )


# ------------------------------------------------------------------------------- REST checks
def wait_healthy(client: httpx.Client, base: str, wait_s: float) -> dict[str, Any] | None:
    """Scale-to-zero hosts cold-start on the first request: give them time, then give up."""
    deadline = time.monotonic() + wait_s
    delay, last = 2.0, "no response"
    while True:
        try:
            r = client.get(f"{base}/health")
            if r.status_code == 200:
                body: dict[str, Any] = r.json()
                return body
            last = f"HTTP {r.status_code}"
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if time.monotonic() + delay > deadline:
            print(f"FAIL  {base}/health not healthy within {wait_s:.0f}s ({last})", flush=True)
            return None
        time.sleep(delay)
        delay = min(delay * 1.5, 15.0)


def check_pricing(client: httpx.Client, base: str, expect: Expect, report: Report) -> list[Any]:
    r = client.get(f"{base}/pricing")
    if not report.check(r.status_code == 200, "/pricing reachable", f"HTTP {r.status_code}"):
        return []
    pricing: dict[str, Any] = r.json()
    report.check(pricing.get("x402Version") == 2, "/pricing x402Version 2")
    report.check(pricing.get("currency") == "USDC", "/pricing currency USDC")
    advertised = {str(n.get("network")): str(n.get("payTo")) for n in pricing.get("networks", [])}
    report.check(
        set(advertised) == set(expect.accepts),
        f"/pricing networks == {sorted(expect.accepts)}",
        f"got {sorted(advertised)}",
    )
    for network, pay_to in advertised.items():
        if network in MAINNETS:
            report.note(f"{network} is a MAINNET network: callers pay real USDC")
        if network in expect.accepts:
            report.check(
                same_address(network, pay_to, expect.accepts[network]),
                f"/pricing payTo for {network} is the expected address",
                f"got {pay_to}",
            )
    ops = [o for o in pricing.get("operations", []) if isinstance(o, dict)]
    by_key = {str(o.get("operation")): o for o in ops}
    missing = sorted(expect.required_ops - set(by_key))
    report.check(not missing, "/pricing lists every required operation", f"missing {missing}")
    for key, price in expect.prices.items():
        got = str(by_key.get(key, {}).get("price"))
        report.check(
            key in by_key and usd_to_atomic(got) == usd_to_atomic(price),
            f"/pricing {key} price == pinned {price}",
            f"got {got}",
        )
    return ops


def check_discovery(
    client: httpx.Client, base: str, expect: Expect, ops: list[Any], report: Report
) -> None:
    paths = {str(o.get("http", "")).partition(" ")[2] for o in ops}
    mcp_url = f"{expect.base_url}/mcp"

    r = client.get(f"{base}/llms.txt")
    if report.check(r.status_code == 200, "/llms.txt reachable", f"HTTP {r.status_code}"):
        ctype = r.headers.get("content-type", "")
        report.check(ctype.startswith("text/plain"), "/llms.txt is text/plain", ctype)
        report.check(r.text.startswith("# "), "/llms.txt starts with an H1 title")
        report.check(mcp_url in r.text, f"/llms.txt links {mcp_url}")
        for o in ops:
            line = f"`{o.get('http')}` ({o.get('price')})"
            report.check(line in r.text, f"/llms.txt advertises {line}")

    r = client.get(f"{base}/.well-known/x402")
    if report.check(r.status_code == 200, "/.well-known/x402 reachable", f"HTTP {r.status_code}"):
        got = set(r.json().get("resources", []))
        want = {f"{expect.base_url}{p}" for p in paths}
        report.check(
            got == want,
            "/.well-known/x402 resources == /pricing routes",
            f"missing {sorted(want - got)}, extra {sorted(got - want)}",
        )

    r = client.get(f"{base}/.well-known/mcp/server-card.json")
    if report.check(r.status_code == 200, "MCP server card reachable", f"HTTP {r.status_code}"):
        card = r.json()
        url = (card.get("transport") or {}).get("url")
        report.check(url == mcp_url, "server card transport url", f"got {url}")
        prices: dict[str, Any] = {}
        for o in ops:
            prices.setdefault(o.get("mcp_tool"), o.get("price"))
        for tool in card.get("tools", []):
            report.check(
                prices.get(tool.get("name")) == tool.get("price"),
                f"server card {tool.get('name')} price == /pricing",
                f"card {tool.get('price')}, pricing {prices.get(tool.get('name'))}",
            )

    r = client.get(f"{base}/")
    if report.check(r.status_code == 200, "/ service index reachable", f"HTTP {r.status_code}"):
        report.check(r.json().get("mcp") == mcp_url, "service index mcp url", r.text[:160])

    r = client.get(f"{base}/openapi.json")
    if report.check(r.status_code == 200, "/openapi.json reachable", f"HTTP {r.status_code}"):
        undocumented = sorted(paths - set(r.json().get("paths", {})))
        report.check(
            not undocumented, "/openapi.json documents every paid route", f"{undocumented}"
        )


def check_provenance(client: httpx.Client, base: str, expect: Expect, report: Report) -> None:
    r = client.get(f"{base}/.well-known/aimart-signing-key")
    if r.status_code == 404 and not expect.require_provenance:
        report.skip("ML-DSA signing key endpoint not deployed (--require-provenance enforces)")
        return
    if not report.check(r.status_code == 200, "signing key endpoint", f"HTTP {r.status_code}"):
        return
    doc = r.json()
    algorithm = doc.get("algorithm")
    report.check(algorithm == "ML-DSA-65", "signing key algorithm ML-DSA-65", str(algorithm))
    try:
        key = base64.b64decode(str(doc.get("public_key")), validate=True)
    except (binascii.Error, ValueError):
        key = b""
    report.check(
        len(key) == ML_DSA_65_PUBLIC_KEY_BYTES,
        f"signing public_key decodes to {ML_DSA_65_PUBLIC_KEY_BYTES} bytes",
        f"got {len(key)} (fields: {sorted(doc)})",
    )
    if expect.key_sha256:
        got = hashlib.sha256(key).hexdigest()
        report.check(
            got == expect.key_sha256.lower(),
            f"signing public key sha256 == {expect.key_sha256[:12]}...",
            f"got {got} (wrong secret version, or a rotation not reflected in the variable?)",
        )
    # An ephemeral key is regenerated on every boot: signatures issued before a restart (or by
    # another instance) stop verifying. Fine on a laptop, a silent defect in production.
    if expect.require_provenance:
        report.check(
            doc.get("ephemeral") is False,
            "signing key is persistent (AIMART_SIGNING_KEY_MLDSA is set)",
            f"ephemeral={doc.get('ephemeral')!r}",
        )
    elif doc.get("ephemeral") is not False:
        report.note("signing key is ephemeral: provenance signatures will not survive a restart")
    # Verification is free by design: it must never answer with a payment challenge.
    vr = client.post(f"{base}/v1/verify", json={})
    report.check(
        vr.status_code != 402, "POST /v1/verify is free (no 402)", f"HTTP {vr.status_code}"
    )


# ------------------------------------------------------------------------------- MCP checks
class Mcp:
    """Raw JSON-RPC over Streamable HTTP: what a directory crawler or an agent actually sends."""

    def __init__(self, client: httpx.Client, url: str) -> None:
        self.client = client
        self.url = url
        self.protocol: str | None = None
        self.session: str | None = None
        self._id = 0

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        if self.session:
            h["Mcp-Session-Id"] = self.session
        return h

    def notify(self, method: str) -> int:
        body = {"jsonrpc": "2.0", "method": method}
        return self.client.post(self.url, headers=self._headers(), json=body).status_code

    def call(self, method: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        r = self.client.post(self.url, headers=self._headers(), json=body)
        if sid := r.headers.get("mcp-session-id"):
            self.session = sid
        if r.status_code != 200:
            return r.status_code, {"error": {"message": r.text[:300]}}
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == self._id:
                        return 200, msg
            return 200, {"error": {"message": "no response event in the SSE stream"}}
        return 200, r.json()


def mcp_list_tools(mcp: Mcp, report: Report) -> dict[str, dict[str, Any]] | None:
    tools: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    for _ in range(MAX_TOOL_PAGES):
        status, msg = mcp.call("tools/list", {"cursor": cursor} if cursor else {})
        if "result" not in msg:
            report.fail(f"MCP tools/list: HTTP {status} {msg.get('error')}")
            return None
        for tool in msg["result"].get("tools", []):
            tools[str(tool.get("name"))] = tool
        cursor = msg["result"].get("nextCursor")
        if not cursor:
            report.ok(f"MCP tools/list: {', '.join(sorted(tools))}")
            return tools
    report.fail(f"MCP tools/list still paginating after {MAX_TOOL_PAGES} pages")
    return None


def check_mcp_tool(
    mcp: Mcp, tool: dict[str, Any], price: str, expect: Expect, report: Report
) -> None:
    name = str(tool.get("name"))
    where = f"MCP {name}"
    advertised = re.search(r"Price:\s*(\$\d+(?:\.\d+)?)", str(tool.get("description", "")))
    if advertised:
        report.check(
            usd_to_atomic(advertised.group(1)) == usd_to_atomic(price),
            f"{where}: description price {advertised.group(1)} == /pricing {price}",
        )
    # Unpaid call: must be refused with a PaymentRequired, never run. Empty arguments suffice:
    # the paywall decides before argument validation, so no work is attempted.
    status, msg = mcp.call("tools/call", {"name": name, "arguments": {}})
    res = msg.get("result") or {}
    sc = res.get("structuredContent") or {}
    if not report.check(
        res.get("isError") is True and "accepts" in sc,
        f"{where}: unpaid call refused with PaymentRequired",
        f"HTTP {status} isError={res.get('isError')} keys={sorted(sc)[:6]} {msg.get('error')}",
    ):
        return
    report.check(sc.get("x402Version") == 2, f"{where}: x402Version 2", str(sc.get("x402Version")))
    resource = (sc.get("resource") or {}).get("url")
    want = f"{expect.base_url}/mcp#{name}"
    report.check(resource == want, f"{where}: resource url {want}", f"got {resource}")
    check_accepts(report, where, sc.get("accepts"), price, expect)


def check_mcp(
    client: httpx.Client, base: str, expect: Expect, ops: list[Any], report: Report
) -> None:
    mcp = Mcp(client, f"{base}/mcp")
    init = {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "aimart-smoke", "version": "1"},
    }
    status, msg = mcp.call("initialize", init)
    if "result" not in msg:
        report.fail(f"MCP initialize: HTTP {status} {msg.get('error')}")
        if status == 421:
            report.note("421 = Host not allowed: AIMART_PUBLIC_BASE_URL must match the URL used")
        return
    result = msg["result"]
    mcp.protocol = str(result.get("protocolVersion"))
    info = result.get("serverInfo") or {}
    report.check(info.get("name") == "aimart-parquet", f"MCP initialize ({mcp.protocol})")
    if expect.version:
        got = info.get("version")
        report.check(got == expect.version, f"MCP serverInfo.version {expect.version}", f"{got}")
    status = mcp.notify("notifications/initialized")
    report.check(status in {200, 202}, "MCP notifications/initialized", f"HTTP {status}")

    tools = mcp_list_tools(mcp, report)
    if tools is None:
        return
    prices: dict[str, str] = {}
    for o in ops:
        prices.setdefault(str(o.get("mcp_tool")), str(o.get("price")))
    for name, price in prices.items():
        if report.check(name in tools, f"MCP tool {name} listed"):
            check_mcp_tool(mcp, tools[name], price, expect, report)
    # Tools /pricing does not sell are free (e.g. verify_provenance): they must never demand
    # payment, or an agent is asked to pay for something we advertise as free.
    for name in sorted(set(tools) - set(prices)):
        _, msg = mcp.call("tools/call", {"name": name, "arguments": {}})
        sc = (msg.get("result") or {}).get("structuredContent") or {}
        report.check("accepts" not in sc, f"MCP {name}: free tool asks for no payment")


# ------------------------------------------------------------------------------- main
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smoke-test a live AImart Parquet deployment.")
    p.add_argument("url", help="Deployment origin, e.g. https://svc-123.us-central1.run.app")
    p.add_argument("--expect-pay-to", help="payTo the 402 must name on --expect-network")
    p.add_argument("--expect-network", default="eip155:84532", help="CAIP-2 network")
    p.add_argument(
        "--expect-accept",
        action="append",
        default=[],
        metavar="NETWORK=PAYTO",
        help="Another expected (network, payTo) pair, e.g. a Solana payee. Repeatable.",
    )
    p.add_argument("--expect-base-url", help="Origin the server should advertise (default: url)")
    p.add_argument(
        "--expect-price", action="append", default=[], metavar="OP=PRICE", help="e.g. convert=0.01"
    )
    p.add_argument("--expect-version", help="Package version /health and MCP must report")
    p.add_argument(
        "--require-operation",
        action="append",
        default=[],
        metavar="OP",
        help="Operation that must be on sale, besides the core five. Repeatable.",
    )
    p.add_argument(
        "--require-provenance",
        action="store_true",
        help="Fail if the ML-DSA-65 signing key is missing or ephemeral (production).",
    )
    p.add_argument(
        "--expect-signing-key-sha256",
        metavar="HEX",
        help="sha256 of the raw ML-DSA-65 public key the deployment must serve.",
    )
    p.add_argument(
        "--skip-mcp",
        action="store_true",
        help="Skip MCP: a tagged-revision URL fails the server's Host allow-list by design.",
    )
    p.add_argument("--wait", type=float, default=120.0, help="Seconds to wait for /health")
    p.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout, seconds")
    return p.parse_args(argv)


def build_expect(args: argparse.Namespace) -> Expect:
    accepts: dict[str, str] = {}
    if args.expect_pay_to:
        accepts[args.expect_network] = args.expect_pay_to
    for item in args.expect_accept:
        network, sep, pay_to = item.partition("=")
        if not (sep and network and pay_to):
            msg = f"--expect-accept must be NETWORK=PAYTO, got {item!r}"
            raise ValueError(msg)
        accepts[network] = pay_to
    if not accepts:
        msg = "say who must be paid: --expect-pay-to ADDRESS and/or --expect-accept NETWORK=PAYTO"
        raise ValueError(msg)
    if unknown := sorted(set(accepts) - set(USDC)):
        msg = f"no canonical USDC known for {unknown}; known networks: {sorted(USDC)}"
        raise ValueError(msg)
    prices: dict[str, str] = {}
    for item in args.expect_price:
        op, sep, price = item.partition("=")
        if not sep:
            msg = f"--expect-price must be OP=PRICE, got {item!r}"
            raise ValueError(msg)
        usd_to_atomic(price)  # reject a malformed pin before touching the network
        prices[op] = price if price.startswith("$") else f"${price}"
    return Expect(
        base_url=(args.expect_base_url or args.url).rstrip("/"),
        accepts=accepts,
        prices=prices,
        required_ops=set(CORE_OPERATIONS) | set(args.require_operation),
        version=args.expect_version,
        require_provenance=args.require_provenance,
        key_sha256=args.expect_signing_key_sha256,
    )


def run(client: httpx.Client, base: str, args: argparse.Namespace, expect: Expect) -> Report:
    report = Report()
    health = wait_healthy(client, base, args.wait)
    if health is None:
        report.fail("/health never answered 200")
        return report
    report.check(health.get("status") == "ok", "/health status ok", json.dumps(health))
    if expect.version:
        got = health.get("version")
        report.check(got == expect.version, f"/health version {expect.version}", f"got {got}")
    try:
        ops = check_pricing(client, base, expect, report)
        if not ops:
            report.fail("/pricing lists no operations: nothing to verify")
            return report
        for op in ops:
            check_paid_route(client, base, op, expect, report)
        check_discovery(client, base, expect, ops, report)
        check_provenance(client, base, expect, report)
        if args.skip_mcp:
            report.skip("MCP checks (--skip-mcp)")
        else:
            check_mcp(client, base, expect, ops, report)
    except httpx.HTTPError as exc:
        report.fail(f"transport error: {type(exc).__name__}: {exc}")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as exc:
        report.fail(f"unexpected response shape: {type(exc).__name__}: {exc}")
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        expect = build_expect(args)
    except ValueError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2
    base = args.url.rstrip("/")
    if urlsplit(base).scheme not in {"http", "https"}:
        print("usage error: url must look like https://host", file=sys.stderr)
        return 2
    print(f"INFO  target {base}; advertised origin {expect.base_url}; payees {expect.accepts}")
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(
        timeout=args.timeout,
        headers=headers,
        follow_redirects=False,
        transport=PatientTransport(retries=2),  # retries= covers connect errors only
    ) as client:
        report = run(client, base, args, expect)
    report.write_github_summary(base)
    print(f"\n{report.passes} passed, {len(report.failures)} failed, {report.skips} skipped")
    for failure in report.failures:
        print(f"  - {failure}")
    if not report.failures:
        return 0
    return 2 if report.passes == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
