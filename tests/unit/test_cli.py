"""The command line, run in-process. Exit code 2 is the contract for "your input was bad"."""

from __future__ import annotations

import json
import runpy
import sys
from typing import TYPE_CHECKING, Any

import pytest

import aimart_testkit as kit
from aimart_parquet import __version__
from aimart_parquet.cli import main

if TYPE_CHECKING:
    from pathlib import Path

CSV = "id,amount,when\n001,1.5,2025-01-02\n002,2.5,2025-01-03\n"


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No .env, no forkserver, artifacts in tmp: the CLI reads Settings from the environment."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIMART_ISOLATE_JOBS", "false")
    monkeypatch.setenv("AIMART_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    return tmp_path


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"aimart-parquet {__version__}"


def test_no_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2
    assert "usage" in capsys.readouterr().err


def test_python_dash_m_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["aimart-parquet", "--version"])
    monkeypatch.delitem(sys.modules, "aimart_parquet.__main__", raising=False)
    with pytest.raises(SystemExit) as info:
        runpy.run_module("aimart_parquet", run_name="__main__")
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_convert_and_inspect(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = kit.write(cli_env / "in" / "sales.csv", CSV)
    out = cli_env / "out" / "clean.parquet"
    assert main(["convert", str(src), "-o", str(out), "--options", '{"sort_by": ["amount"]}']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["report"]["rows"] == 2
    assert report["artifact"]["path"] == str(out)
    columns = {c["name"]: c["type"] for c in report["report"]["columns"]}
    assert columns["id"] == "string"  # leading zeros kept
    assert out.is_file()

    assert main(["inspect", str(out)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["rows"] == 2


def test_convert_defaults_to_a_file_next_to_the_input(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = kit.write(cli_env / "data" / "sales.csv", CSV)
    assert main(["convert", str(src)]) == 0
    assert json.loads(capsys.readouterr().out)["artifact"]["path"] == str(
        cli_env / "data" / "sales.parquet"
    )


@pytest.mark.parametrize(
    ("make_input", "code"),
    [
        (lambda d: str(d / "missing.csv"), "invalid_request"),
        (lambda d: str(kit.write(d / "garbage.bin", b"\x00\x9f\x92\x96" * 64)), None),
        (lambda d: str(kit.write(d / "empty.csv", b"")), None),
    ],
    ids=["missing", "binary-garbage", "empty"],
)
def test_convert_bad_input_exits_2_with_a_json_error(
    make_input: Any, code: str | None, cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["convert", make_input(cli_env)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["message"]
    assert error["hint"]
    if code:
        assert error["code"] == code


def test_inspect_bad_input_exits_2(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = kit.write(cli_env / "not.parquet", b"definitely not parquet")
    assert main(["inspect", str(bad)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"]


def test_serve_builds_the_app_and_runs_uvicorn(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setenv("AIMART_PAY_TO_EVM", kit.PAY_TO_EVM)
    monkeypatch.setenv("PORT", "9123")
    assert main(["serve", "--host", "127.0.0.1"]) == 0
    app, kwargs = calls[0]
    assert app.state.payments is not None
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9123
    assert kwargs["proxy_headers"] is True

    from aimart_parquet.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AIMART_PAYMENTS_ENABLED", "false")
    assert main(["serve", "--port", "8001"]) == 0
    assert calls[1][1]["port"] == 8001
    assert calls[1][0].state.payments is None


def test_mcp_runs_the_local_server_over_stdio(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.server.mcpserver import MCPServer

    runs: list[tuple[str, set[str]]] = []

    def fake_run(self: MCPServer, transport: str, *_: Any, **__: Any) -> None:
        runs.append(
            (transport, set(self._tool_manager._tools) if hasattr(self, "_tool_manager") else set())
        )

    monkeypatch.setattr(MCPServer, "run", fake_run)
    assert main(["mcp"]) == 0
    assert runs[0][0] == "stdio"
