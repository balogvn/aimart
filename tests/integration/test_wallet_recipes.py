"""The wallet recipes (docs/recipes) pay the real app, cap their spend, and say why a call failed.

Each Python recipe is one wallet function (Coinbase's or Circle's account as an x402 signer) and a
payment part that takes any signer. Neither SDK is installed here: the payment part runs with a
local throwaway eth_account key against the in-process app and a fake facilitator, and the
Circle adapter runs with a stand-in for Circle's API that signs the JSON document it is sent.
The pages, the skill and the code must agree on prices, routes, URLs and pinned versions.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import yaml
from eth_account import Account
from eth_account.messages import encode_typed_data

import aimart_testkit as kit
from aimart_parquet.api.discovery import RECIPES_URL, llms_txt
from aimart_parquet.payments.pricing import OPERATIONS

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from eth_account.signers.local import LocalAccount

    from aimart_parquet.config import Settings

ROOT = Path(__file__).resolve().parents[2]
RECIPES = ROOT / "docs" / "recipes"
SKILL = ROOT / "skills" / "aimart-parquet" / "SKILL.md"
PAGES = sorted(RECIPES.glob("*.md"))
SCRIPTS = {"coinbase_cdp": "coinbase-cdp-python.md", "circle_dcw": "circle-python.md"}
TS_FOLDERS = {
    "coinbase": RECIPES / "typescript" / "coinbase",
    "circle": RECIPES / "typescript" / "circle",
}
CSV = b"region,units\nNorth,10\nSouth,20\nNorth,5\n"
SOURCE = {"content_base64": kit.b64(CSV), "filename": "sales.csv"}
SQL = "SELECT region, sum(units) AS units FROM data GROUP BY region ORDER BY region"
TX = kit.TX_HASH


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"recipe_{name}", RECIPES / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recovers_to_payer(payload: Any, requirements: Any) -> bool:
    """The EIP-3009 TransferWithAuthorization signature recovers to its own ``from``."""
    auth = payload.payload["authorization"]
    typed = {
        "types": {
            "EIP712Domain": load("circle_dcw").EIP712_DOMAIN,
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": requirements.extra["name"],
            "version": requirements.extra["version"],
            "chainId": int(requirements.network.split(":")[1]),
            "verifyingContract": requirements.asset,
        },
        "message": {
            "from": auth["from"],
            "to": auth["to"],
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": bytes.fromhex(auth["nonce"][2:]),
        },
    }
    signer = Account.recover_message(
        encode_typed_data(full_message=typed), signature=payload.payload["signature"]
    )
    return bool(
        signer.lower() == auth["from"].lower() and auth["to"].lower() == requirements.pay_to.lower()
    )


class FakeCircle:
    """Circle's sign-typed-data API: one EIP-712 JSON document in, a hex signature out."""

    def __init__(self, error: Exception | None = None) -> None:
        self.key: LocalAccount = Account.create()
        self.documents: list[dict[str, Any]] = []
        self.error = error

    def __call__(self, data: str) -> str:
        document = json.loads(data)  # exactly what Circle would receive
        self.documents.append(document)
        if self.error is not None:
            raise self.error
        typed = json.loads(data)
        typed["domain"]["chainId"] = int(typed["domain"]["chainId"])
        message = typed["message"]
        for field in typed["types"][typed["primaryType"]]:  # back from JSON strings
            if field["type"] == "uint256":
                message[field["name"]] = int(message[field["name"]])
            elif field["type"] == "bytes32":
                message[field["name"]] = bytes.fromhex(message[field["name"]].removeprefix("0x"))
        signed = self.key.sign_message(encode_typed_data(full_message=typed))
        return str(signed.signature.hex()).removeprefix("0x")  # Circle may answer without 0x


def coinbase_like(_: ModuleType) -> tuple[Any, Callable[[], int]]:
    """A local key in place of CDP's EvmLocalAccount: both are eth_account BaseAccounts."""
    return Account.create(), lambda: 0


def circle_like(
    recipe: ModuleType, circle: FakeCircle | None = None
) -> tuple[Any, Callable[[], int]]:
    circle = circle or FakeCircle()
    return recipe.CircleSigner(circle.key.address, circle), lambda: len(circle.documents)


WALLETS = [("coinbase_cdp", coinbase_like), ("circle_dcw", circle_like)]


async def pay(
    settings: Settings,
    recipe: ModuleType,
    signer: Any,
    facilitator: Any,
    *,
    max_usd: str = "0.02",
    sql: str = SQL,
    network: Callable[[httpx.AsyncBaseTransport], httpx.AsyncBaseTransport] = lambda t: t,
) -> dict[str, Any]:
    """Run the recipe's payment part against the app; a NotPaid is raised after the app stops
    (raised inside, the app's task group would wrap it in an ExceptionGroup)."""
    payer = recipe.make_payer(signer, network=kit.BASE_SEPOLIA, max_usd=Decimal(max_usd))
    stopped: Exception | None = None
    async with kit.running_app(settings, facilitators=[facilitator]) as h:
        try:
            result: dict[str, Any] = await recipe.convert_and_query(
                payer,
                base_url=kit.BASE_URL,
                source=SOURCE,
                sql=sql,
                transport=network(httpx.ASGITransport(app=h.app)),
            )
        except recipe.NotPaid as error:
            stopped = error
    if stopped is not None:
        raise stopped
    return result


# ------------------------------------------------------------------------------ paying
@pytest.mark.anyio
@pytest.mark.parametrize(("name", "wallet"), WALLETS)
async def test_a_recipe_converts_and_queries_for_two_valid_payments(
    name: str, wallet: Any, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = load(name)
    signer, signatures = wallet(recipe)
    facilitator = kit.FakeFacilitator()
    result = await pay(settings, recipe, signer, facilitator)

    assert result["query"]["rows"] == [["North", 15], ["South", 20]]
    assert [r.amount for _, r in facilitator.settled] == ["20000", "5000"]  # $0.02 + $0.005
    for payload, requirements in facilitator.settled:
        assert payload.x402_version == 2
        assert requirements.scheme == "exact"
        assert payload.payload["authorization"]["from"] == signer.address
        assert recovers_to_payer(payload, requirements)
    assert signatures() in (0, 2)  # Circle signs once per payment
    printed = capsys.readouterr().out
    assert f"paid 0.02 USDC, tx {TX} https://sepolia.basescan.org/tx/{TX}" in printed
    assert f"paid 0.005 USDC, tx {TX}" in printed
    assert f"artifact: {result['artifact_id']}" in printed


def test_circle_receives_one_complete_eip712_document() -> None:
    """What Circle's API is sent: EIP712Domain added, the nonce as hex, numbers as strings."""
    recipe = load("circle_dcw")
    circle = FakeCircle()
    from x402.mechanisms.evm import TypedDataDomain, TypedDataField

    domain = TypedDataDomain(
        name="USD Coin", version="2", chain_id=8453, verifying_contract=kit.USDC_BASE_MAINNET
    )
    types = {
        "TransferWithAuthorization": [
            TypedDataField(name="value", type="uint256"),
            TypedDataField(name="nonce", type="bytes32"),
        ]
    }
    signature = recipe.CircleSigner(circle.key.address, circle).sign_typed_data(
        domain, types, "TransferWithAuthorization", {"value": 20000, "nonce": b"\x01" * 32}
    )
    [document] = circle.documents
    assert document["types"]["EIP712Domain"] == recipe.EIP712_DOMAIN
    assert document["domain"] == {
        "name": "USD Coin",
        "version": "2",
        "chainId": "8453",
        "verifyingContract": kit.USDC_BASE_MAINNET,
    }
    assert document["message"] == {"value": "20000", "nonce": "0x" + "01" * 32}
    assert len(signature) == 65


# ------------------------------------------------------------------------------ the cap
@pytest.mark.anyio
@pytest.mark.parametrize(("name", "wallet"), WALLETS)
async def test_a_quote_above_the_cap_is_refused_before_signing(
    name: str, wallet: Any, settings: Settings
) -> None:
    recipe = load(name)
    signer, signatures = wallet(recipe)
    facilitator = kit.FakeFacilitator()
    with pytest.raises(recipe.NotPaid) as stopped:
        await pay(settings, recipe, signer, facilitator, max_usd="0.019")
    line = str(stopped.value)
    assert line.startswith("/v1/convert: no payment made: ")
    assert "filtered out by policies" in line
    assert facilitator.verified == []  # nothing reached the service's facilitator
    assert signatures() == 0  # Circle was never asked to sign


@pytest.mark.anyio
async def test_a_wallet_that_cannot_sign_pays_nothing(settings: Settings) -> None:
    recipe = load("circle_dcw")
    facilitator = kit.FakeFacilitator()
    signer, _ = circle_like(
        recipe, FakeCircle(error=RuntimeError("entity secret is not registered"))
    )
    with pytest.raises(recipe.NotPaid) as stopped:
        await pay(settings, recipe, signer, facilitator)
    assert str(stopped.value).startswith("/v1/convert: no payment made: ")
    assert "entity secret is not registered" in str(stopped.value)
    assert facilitator.verified == []


# ------------------------------------------------------------------------------ failures
@pytest.mark.anyio
@pytest.mark.parametrize("name", list(SCRIPTS))
@pytest.mark.parametrize(
    ("facilitator", "says"),
    [
        (
            lambda: kit.FakeFacilitator(valid=False),
            ["payment rejected, not charged: ", "insufficient_funds"],
        ),
        (
            lambda: kit.FakeFacilitator(settle_ok=False),
            ["payment not settled, result withheld: insufficient_funds"],
        ),
        (
            lambda: kit.wire_facilitator({"transaction": TX, "error": "reverted"}),
            ["payment not settled, result withheld: ", "reverted", TX, "before paying again"],
        ),
    ],
    ids=["refused-before-the-work", "settlement-failed", "settlement-named-a-transaction"],
)
async def test_a_refused_payment_is_one_line_that_says_whether_money_moved(
    name: str, facilitator: Callable[[], Any], says: list[str], settings: Settings
) -> None:
    recipe = load(name)
    with pytest.raises(recipe.NotPaid) as stopped:
        await pay(settings, recipe, Account.create(), facilitator())
    line = str(stopped.value)
    assert line.startswith("/v1/convert: ")
    assert "\n" not in line
    for text in says:
        assert text in line
    if "rejected" in line:
        assert "before paying again" not in line  # nothing was submitted


@pytest.mark.anyio
@pytest.mark.parametrize("name", list(SCRIPTS))
async def test_a_failed_call_is_not_charged_and_says_why(name: str, settings: Settings) -> None:
    recipe = load(name)
    facilitator = kit.FakeFacilitator()
    with pytest.raises(recipe.NotPaid) as stopped:
        await pay(settings, recipe, Account.create(), facilitator, sql="DELETE FROM data")
    line = str(stopped.value)
    assert re.match(r"/v1/query: HTTP 4\d\d, not charged: \S", line), line
    assert [r.amount for _, r in facilitator.settled] == ["20000"]  # only the convert


class LostAnswer(httpx.AsyncBaseTransport):
    """The network under the recipe: a request that carries a signed payment gets no answer,
    after the service has handled it (settled) or before it arrives."""

    def __init__(self, app: httpx.AsyncBaseTransport, *, delivered: bool) -> None:
        self.app = app
        self.delivered = delivered

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "PAYMENT-SIGNATURE" not in request.headers:
            return await self.app.handle_async_request(request)
        if not self.delivered:
            raise httpx.ReadTimeout("timed out", request=request)
        await self.app.handle_async_request(request)
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")


@pytest.mark.anyio
@pytest.mark.parametrize(("name", "wallet"), WALLETS)
@pytest.mark.parametrize("delivered", [True, False], ids=["settled-then-dropped", "timed-out"])
async def test_a_paid_call_whose_answer_is_lost_never_says_nothing_was_paid(
    name: str, wallet: Any, delivered: bool, settings: Settings
) -> None:
    """x402 wraps any error after the 402 in a PaymentError, the lost answer to the paid
    request included; the recipe must not call that "no payment made"."""
    recipe = load(name)
    signer, _ = wallet(recipe)
    facilitator = kit.FakeFacilitator()
    with pytest.raises(recipe.NotPaid) as stopped:
        await pay(
            settings,
            recipe,
            signer,
            facilitator,
            network=lambda app: LostAnswer(app, delivered=delivered),
        )
    line = str(stopped.value)
    assert line.startswith("/v1/convert: outcome unknown, a signed payment of 0.02 USDC was sent")
    assert f"look up https://sepolia.basescan.org/address/{signer.address} before paying" in line
    assert "no payment made" not in line
    assert "nothing paid" not in line
    assert "\n" not in line
    assert [r.amount for _, r in facilitator.settled] == (["20000"] if delivered else [])


class Unreachable(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed", request=request)


@pytest.mark.anyio
@pytest.mark.parametrize(("name", "wallet"), WALLETS)
async def test_a_service_that_does_not_answer_is_one_line_and_nothing_paid(
    name: str, wallet: Any, settings: Settings
) -> None:
    recipe = load(name)
    signer, signatures = wallet(recipe)
    facilitator = kit.FakeFacilitator()
    with pytest.raises(recipe.NotPaid) as stopped:
        await pay(settings, recipe, signer, facilitator, network=lambda _: Unreachable())
    assert str(stopped.value) == (
        "/v1/convert: no response, nothing paid: All connection attempts failed"
    )
    assert signatures() == 0
    assert facilitator.verified == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"PAYMENT-REQUIRED": "not base64 !!"},
        {"PAYMENT-RESPONSE": "bnVsbA=="},
        {"PAYMENT-RESPONSE": "W10="},
    ],
)
@pytest.mark.parametrize("name", list(SCRIPTS))
def test_a_402_with_no_readable_reason_is_still_one_line(
    name: str, headers: dict[str, str]
) -> None:
    line = load(name).failure(httpx.Response(402, headers=headers))
    assert "no reason given" in line
    assert "\n" not in line


# ------------------------------------------------------------------------------ command line
@pytest.mark.parametrize(
    ("name", "flag"), [("coinbase_cdp", "--account"), ("circle_dcw", "--wallet")]
)
def test_mainnet_needs_the_confirmation_sentence(
    name: str, flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = load(name)
    monkeypatch.setattr(sys, "argv", [f"{name}.py", flag, "0x" + "cd" * 20])
    with pytest.raises(SystemExit):
        recipe.main()
    assert "AIMART_MAINNET_CONFIRM=I_UNDERSTAND_THIS_SPENDS_REAL_USDC" in capsys.readouterr().err


def test_circle_names_the_missing_variables(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for variable in ("CIRCLE_API_KEY", "CIRCLE_ENTITY_SECRET"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(
        sys, "argv", ["circle_dcw.py", "--wallet", "0x" + "cd" * 20, "--network", "base-sepolia"]
    )
    with pytest.raises(SystemExit):
        load("circle_dcw").main()
    assert "set CIRCLE_API_KEY and CIRCLE_ENTITY_SECRET" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "wallet_part", "flag", "says"),
    [
        ("coinbase_cdp", "coinbase_signer", "--account", "Coinbase: cannot use account"),
        ("circle_dcw", "circle_signer", "--wallet", "Circle: cannot use wallet"),
    ],
)
def test_a_wallet_that_will_not_start_is_one_line(
    name: str, wallet_part: str, flag: str, says: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = load(name)

    def broken(*_: Any) -> Any:
        raise RuntimeError("401 unauthorized")

    async def broken_async(*_: Any) -> Any:
        broken()

    monkeypatch.setattr(recipe, wallet_part, broken_async if name == "coinbase_cdp" else broken)
    monkeypatch.setenv("CIRCLE_API_KEY", "placeholder")
    monkeypatch.setenv("CIRCLE_ENTITY_SECRET", "placeholder")
    monkeypatch.setattr(
        sys, "argv", [f"{name}.py", flag, "0x" + "cd" * 20, "--network", "base-sepolia"]
    )
    with pytest.raises(SystemExit) as stopped:
        recipe.main()
    assert stopped.value.code == f"{says} {'0x' + 'cd' * 20}: 401 unauthorized"


# ------------------------------------------------------------------------------ docs agree
def pep723_pins(script: Path) -> dict[str, str]:
    block = re.search(r"^# /// script\n(.*?)^# ///$", script.read_text(), re.M | re.S)
    assert block is not None, script
    dependencies = re.search(r"dependencies = (\[.*\])", block.group(1))
    assert dependencies is not None
    pins = {}
    for requirement in json.loads(dependencies.group(1)):
        package, version = requirement.split("==")  # exact pins only
        pins[package] = version
    return pins


def npm_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for folder in TS_FOLDERS.values():
        package = json.loads((folder / "package.json").read_text())
        for name, version in {**package["dependencies"], **package["devDependencies"]}.items():
            assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{name} {version}: pin exactly"
            assert pins.setdefault(name, version) == version, f"{name} differs between folders"
    return pins


def published_text() -> dict[str, str]:
    return {p.name: p.read_text() for p in [*PAGES, SKILL]}


def test_the_python_pins_on_every_page_are_the_scripts_pins() -> None:
    pins = pep723_pins(RECIPES / "coinbase_cdp.py") | pep723_pins(RECIPES / "circle_dcw.py")
    assert pins["x402[evm,httpx]"] == pep723_pins(RECIPES / "circle_dcw.py")["x402[evm,httpx]"]
    checked = set()
    for name, text in published_text().items():
        for package, version in re.findall(r"([\w\[\],.-]+)==(\d[\w.]*)", text):
            checked.add(package)
            assert pins.get(package) == version, (
                f"{name}: {package}=={version}, scripts pin {pins.get(package)}"
            )
    assert checked == set(pins)
    for script, page in SCRIPTS.items():
        for package, version in pep723_pins(RECIPES / f"{script}.py").items():
            assert f"{package}=={version}" in (RECIPES / page).read_text(), (page, package)


def test_the_npm_pins_on_every_page_are_the_package_json_pins() -> None:
    pins = npm_pins()
    tools = {"skills": "1.7.5", "@circle-fin/cli": "1.2.2"}  # awal: retired by Coinbase 2026-11-20
    checked = set()
    for name, text in published_text().items():
        for package, version in re.findall(
            r"(?<![\w/@.-])(@[\w-]+/[\w-]+|[a-z][\w-]*)@(\d+\.\d+\.\d+)", text
        ):
            checked.add(package)
            # The @x402 packages move together; the tools are CLIs the docs run, not dependencies.
            x402 = package.startswith("@x402/")
            expected = pins["@x402/core"] if x402 else pins.get(package) or tools.get(package)
            assert expected == version, f"{name}: {package}@{version}, pinned {expected}"
    assert {
        "@coinbase/cdp-sdk",
        "@circle-fin/developer-controlled-wallets",
        "@x402/fetch",
    } <= checked


CUTOFF = "2026-11-03"  # every locked package was published before it, 14 days before the recipes


@pytest.mark.parametrize("script", list(SCRIPTS))
def test_the_python_scripts_resolve_nothing_newer_than_the_cutoff(script: str) -> None:
    text = (RECIPES / f"{script}.py").read_text()
    block = re.search(r"^# /// script\n(.*?)^# ///$", text, re.M | re.S)
    assert block is not None
    assert f'# [tool.uv]\n# exclude-newer = "{CUTOFF}T00:00:00Z"' in block.group(1)
    page = (RECIPES / SCRIPTS[script]).read_text()
    assert f"--exclude-newer {CUTOFF}" in page
    assert f'# exclude-newer = "{CUTOFF}T00:00:00Z"' in page  # the short program on the page


@pytest.mark.parametrize("folder", list(TS_FOLDERS))
def test_each_typescript_folder_ships_the_lock_its_pages_install(folder: str) -> None:
    path = TS_FOLDERS[folder]
    package = json.loads((path / "package.json").read_text())
    lock = json.loads((path / "package-lock.json").read_text())
    pins = {**package["dependencies"], **package["devDependencies"]}
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]
    for name, version in pins.items():
        assert lock["packages"][f"node_modules/{name}"]["version"] == version, name
    for entry in lock["packages"].values():
        if "resolved" in entry:
            assert entry["resolved"].startswith("https://registry.npmjs.org/"), entry["resolved"]
    pages = [p for p in PAGES if f"typescript/{folder}" in p.read_text()]
    assert pages
    for page in pages:
        text = page.read_text()
        assert "\nnpm ci\n" in text, page.name
        assert "\nnpm install\n" not in text, page.name
        assert f"--before={CUTOFF}" in text, page.name


def test_the_typescript_payment_part_is_one_file() -> None:
    shared = {folder: (path / "aimart.ts").read_text() for folder, path in TS_FOLDERS.items()}
    assert shared["coinbase"] == shared["circle"]
    python = load("coinbase_cdp")
    for constant in ("AIMART_URL", "SAMPLE_CSV", "SQL"):
        assert f'export const {constant} = "{getattr(python, constant)}";' in shared["coinbase"]
    for folder in TS_FOLDERS.values():
        scripts = json.loads((folder / "package.json").read_text())["scripts"]
        for command in scripts.values():
            for file in re.findall(r"[\w-]+\.ts", command):
                assert (folder / file).is_file(), f"{folder.name}/package.json runs {file}"


def test_the_python_payment_part_is_one_text() -> None:
    def payment_part(name: str) -> str:
        text = (RECIPES / f"{name}.py").read_text()
        part = re.search(r"^# -+ payment part$(.*)^# -+ command line$", text, re.M | re.S)
        assert part is not None, name
        return part.group(1)

    assert payment_part("coinbase_cdp") == payment_part("circle_dcw")


def test_prices_and_routes_on_every_page_are_the_services(
    make_settings: Callable[..., Settings],
) -> None:
    from aimart_parquet.api.app import create_app

    settings = make_settings(enable_quantum=False)
    prices = {op.path: op.price(settings) for op in OPERATIONS if op.requires is None}
    routes = {getattr(r, "path", "") for r in create_app(settings, payments=False).routes}
    tools = {op.tool for op in OPERATIONS}
    for name, text in published_text().items():
        for path in re.findall(r"(?:POST|GET) (/[\w/.-]*\w)", text):
            assert path in routes, f"{name}: {path} is not a route"
        for tool in re.findall(r"`(\w+_parquet)`", text):
            assert tool in tools, f"{name}: no MCP tool {tool}"
    for table in (RECIPES / "README.md", SKILL):
        listed = dict(
            re.findall(r"^\| `POST (/v1/[\w/]+)`.*\| (\$[\d.]+) \|$", table.read_text(), re.M)
        )
        assert listed == prices, table.name
    total = Decimal(prices["/v1/convert"][1:]) + Decimal(prices["/v1/query"][1:])
    for page in PAGES:
        if page.name != "README.md":
            text = " ".join(page.read_text().split())
            assert f"**${total}**" in text, page.name
            assert f"convert {prices['/v1/convert']} plus query {prices['/v1/query']}" in text, (
                page.name
            )


def test_urls_on_every_page_are_live_and_in_the_repo() -> None:
    python = load("coinbase_cdp")
    assert load("circle_dcw").AIMART_URL == python.AIMART_URL
    raw = "https://raw.githubusercontent.com/balogvn/aimart/main/"
    assert python.SAMPLE_CSV.startswith(raw)
    for name, text in published_text().items():
        assert python.AIMART_URL in text, name
        for url in re.findall(r"https://[^\s`'\")<>]+", text):
            host = url.split("/")[2]
            if host == "balogvn--aimart-parquet.us-east.modal.run":
                assert url.startswith(python.AIMART_URL), (name, url)
            if url.startswith(raw):
                assert (ROOT / url.removeprefix(raw)).is_file(), (name, url)
            for prefix in (
                "https://github.com/balogvn/aimart/blob/main/",
                "https://github.com/balogvn/aimart/tree/main/",
            ):
                if url.startswith(prefix):
                    assert (ROOT / url.removeprefix(prefix).split("#")[0]).exists(), (name, url)
    for page in [*PAGES, SKILL]:
        for link in re.findall(r"\]\(([^)#:]+)(?:#[^)]*)?\)", page.read_text()):
            assert (page.parent / link).exists(), (page.name, link)


def test_llms_txt_points_to_the_recipes(settings: Settings) -> None:
    text = llms_txt(settings)
    [line] = [line for line in text.splitlines() if RECIPES_URL in line]
    assert "npx skills@1.7.5 add balogvn/aimart" in line
    assert RECIPES_URL == "https://github.com/balogvn/aimart/tree/main/docs/recipes"
    assert (ROOT / "docs" / "recipes" / "README.md").is_file()


# ------------------------------------------------------------------------------ the skill
def test_the_skill_has_the_frontmatter_the_skills_cli_requires() -> None:
    """vercel-labs/skills 1.7.5, as the docs pin it (parseSkillMd): the block between the first two --- lines is
    YAML; name and description must be non-empty strings; line breaks are folded into spaces.
    It searches <repo>/skills/<name>/SKILL.md among its first places."""
    text = SKILL.read_text()
    match = re.match(r"^﻿?---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$", text)
    assert match is not None
    data = yaml.safe_load(match.group(1))
    assert isinstance(data["name"], str)
    assert isinstance(data["description"], str)
    assert data["name"].strip()
    assert data["description"].strip()
    assert data["name"] == SKILL.parent.name == "aimart-parquet"
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", data["name"])  # what sanitizeName leaves alone
    assert "\n" not in data["description"]
    assert len(data["description"]) <= 1024
    assert not data.get("metadata", {}).get("internal")  # internal skills are not installed
    assert sorted(p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md")) == ["aimart-parquet"]
