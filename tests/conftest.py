"""Isolated labs and a default-deny network fixture."""

import socket
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from sfx_lab.lab import Lab, init_lab
from tests.fakes import FakeGenerator


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("SFX_LAB_DIR", raising=False)
    monkeypatch.delenv("SFX_LAB_PROJECT", raising=False)
    monkeypatch.delenv("SFX_LAB_DEBUG", raising=False)
    FakeGenerator.settlements.clear()
    FakeGenerator.calls.clear()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network access is forbidden in tests")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return init_lab(tmp_path / "lab", generator="tests.fakes:FakeGenerator", sheets=True)
