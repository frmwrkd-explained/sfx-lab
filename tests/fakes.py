"""Importable generator adapter that needs no sfx_lab imports."""

import os
import wave
import tempfile
from pathlib import Path
from typing import Any


class FakeGenerator:
    name = "fake"
    settlements: list[tuple[str, bool]] = []
    calls: list[dict[str, Any]] = []

    def estimate_usd(self, spec: dict[str, Any]) -> float:
        return 0.01

    def generate(self, spec: dict[str, Any], dest: Path) -> dict[str, Any]:
        self.calls.append(spec)
        if spec["prompt"] == "fail":
            raise RuntimeError("deliberate failure")
        path = dest.with_suffix(".wav")
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            with wave.open(stream, "wb") as wav:
                wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x00" * 800)
        os.replace(temporary, path)
        return {"path": str(path), "ref": dest.name, "cost_usd": 0.01}

    def settle(self, ref: str, keep: bool) -> None:
        self.settlements.append((ref, keep))


class FakeAgent:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.prompts: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self.responses)
