"""The example asks for the paying wallet's key at a hidden prompt (review of 2026-11-26).

``deploy/README.md`` had the owner type ``export PAYER_PRIVATE_KEY=...``, and zsh keeps every
typed line, in plain text, in ``~/.zsh_history``; a test wallet's key had already leaked into a
transcript once. Run in a terminal without the variable, ``agent_pays.py`` now asks for the key
with ``getpass``: not shown, not saved by the shell. Without a terminal it says what to set.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE_KEY = "0x" + "11" * 32  # never used: the run stops at the mainnet switch


def load_example() -> Any:
    path = ROOT / "docs" / "examples" / "agent_pays.py"
    spec = importlib.util.spec_from_file_location("agent_pays_key_prompt", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Stdin:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    example = load_example()
    csv = tmp_path / "small.csv"
    csv.write_text("a,b\n1,2\n")
    monkeypatch.delenv("PAYER_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("AIMART_MAINNET_CONFIRM", raising=False)
    argv = ["agent_pays.py", str(csv), "--url", "https://service.test", "--network", "mainnet"]
    monkeypatch.setattr(sys, "argv", argv)

    def main(*, tty: bool) -> tuple[list[str], int | str | None]:
        asked: list[str] = []

        def getpass(prompt: str = "") -> str:
            asked.append(prompt)
            return f"  {FAKE_KEY}\n"

        monkeypatch.setattr(sys, "stdin", Stdin(tty))
        monkeypatch.setattr(example.getpass, "getpass", getpass)
        with pytest.raises(SystemExit) as stopped:
            example.main()
        return asked, stopped.value.code

    return main


def test_in_a_terminal_the_key_is_asked_for_at_a_hidden_prompt(
    run: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    asked, code = run(tty=True)
    assert code == 2
    assert len(asked) == 1
    assert "hidden" in asked[0]
    error = capsys.readouterr().err
    # Past the key, stopped by the mainnet switch: the prompt's answer was taken as the key.
    assert "AIMART_MAINNET_CONFIRM" in error
    assert "PAYER_PRIVATE_KEY" not in error
    assert FAKE_KEY not in error


def test_without_a_terminal_it_says_what_to_set_and_asks_nothing(
    run: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    asked, code = run(tty=False)
    assert code == 2
    assert asked == []
    assert "PAYER_PRIVATE_KEY" in capsys.readouterr().err
