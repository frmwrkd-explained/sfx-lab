"""Built-in sound generation and import-based adapter discovery."""

import importlib
import importlib.metadata
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .agents import safe_message
from .lab import atomic_write

# ElevenLabs API pricing, checked 2026-10-06.
PRICE_PER_MINUTE = 0.12
AUTO_SECONDS = 5


class Generator(Protocol):
    name: str

    def estimate_usd(self, spec: dict[str, Any]) -> float: ...
    def generate(self, spec: dict[str, Any], dest: Path) -> dict[str, Any]: ...
    def settle(self, ref: str, keep: bool) -> None: ...


def _error_detail(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
        if detail is not None:
            return safe_message(str(detail))
    except ValueError:
        pass
    return safe_message(text)[:200]


class ElevenLabs:
    name = "elevenlabs"

    def estimate_usd(self, spec: dict[str, Any]) -> float:
        seconds = spec.get("duration_s")
        return PRICE_PER_MINUTE * (AUTO_SECONDS if seconds is None else seconds) / 60

    def generate(self, spec: dict[str, Any], dest: Path) -> dict[str, Any]:
        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("Set ELEVENLABS_API_KEY (environment or <lab>/.env).")
        body = {"text": spec["prompt"], "model_id": "eleven_text_to_sound_v2",
                "prompt_influence": spec["prompt_influence"], "loop": spec["loop"]}
        if spec.get("duration_s") is not None:
            body["duration_seconds"] = spec["duration_s"]
        request = urllib.request.Request(
            "https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128",
            data=json.dumps(body).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                audio = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"ElevenLabs {error.code}: {_error_detail(error.read())}") from error
        path = dest.with_suffix(".mp3")
        atomic_write(path, audio)
        return {"path": str(path), "ref": "", "cost_usd": self.estimate_usd(spec)}

    def settle(self, ref: str, keep: bool) -> None:
        pass


def load_generator(name: str) -> Generator:
    if name == "elevenlabs":
        return ElevenLabs()
    for entry in importlib.metadata.entry_points(group="sfx_lab.generators"):
        if entry.name == name:
            return entry.load()()
    if ":" in name:
        module, class_name = name.split(":", 1)
        return getattr(importlib.import_module(module), class_name)()
    raise ValueError(f"Unknown generator: {name}. Use elevenlabs, an entry point, or package.module:ClassName.")
