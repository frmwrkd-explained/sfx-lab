"""Built-in sound generation and import-based adapter discovery."""

import importlib
import importlib.metadata
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .agents import safe_message
from .lab import atomic_write

# ElevenLabs API pricing, checked 2026-10-06.
PRICE_PER_MINUTE = 0.12
AUTO_SECONDS = 5
SFX_V3 = "eleven_text_to_sound_v3"
SFX_V2 = "eleven_text_to_sound_v2"
SFX_CREDITS_PER_SECOND = 40
# Published ElevenAPI pay-as-you-go v4 price, checked 2026-10-28.
TTS_USD_PER_1000_CHARS = 0.08


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

    def __init__(self, model_id: str = SFX_V3) -> None:
        if model_id not in (SFX_V3, SFX_V2):
            raise ValueError("Choose eleven_text_to_sound_v3 or eleven_text_to_sound_v2.")
        self.model_id = model_id

    def estimate_usd(self, spec: dict[str, Any]) -> float:
        seconds = spec.get("duration_s")
        return PRICE_PER_MINUTE * (AUTO_SECONDS if seconds is None else seconds) / 60

    def generate(self, spec: dict[str, Any], dest: Path) -> dict[str, Any]:
        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("Set ELEVENLABS_API_KEY (environment or <lab>/.env).")
        body = {"text": spec["prompt"], "model_id": self.model_id,
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


class ElevenLabsTTS(ElevenLabs):
    """Use an existing voice id. This adapter never calls a cloning endpoint."""

    name = "elevenlabs-tts"

    def estimate_usd(self, spec: dict[str, Any]) -> float:
        return len(spec["prompt"]) * TTS_USD_PER_1000_CHARS / 1000

    def generate(self, spec: dict[str, Any], dest: Path) -> dict[str, Any]:
        voice = spec.get("voice_id", "")
        if not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice):
            raise ValueError("Provide an existing ElevenLabs voice id.")
        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("Set ELEVENLABS_API_KEY (environment or <lab>/.env).")
        request = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
            data=json.dumps({"text": spec["prompt"], "model_id": "eleven_v4"}).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                audio = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"ElevenLabs {error.code}: {_error_detail(error.read())}") from error
        path = dest.with_suffix(".mp3")
        atomic_write(path, audio)
        return {"path": str(path), "ref": "", "cost_usd": self.estimate_usd(spec)}


EPIDEMIC_API = "https://api.epidemicsound.com"
_STOP_WORDS = {
    "a", "an", "and", "the", "for", "of", "in", "on", "to", "with", "at", "by", "from", "into", "past",
    "no", "not", "very", "is", "it", "its", "then", "than", "like", "about", "s", "sec", "second", "seconds",
    "isolated", "clean", "music", "voice", "reverb", "tail", "attack", "short", "long", "crisp", "dry",
    "body", "low", "mid", "high", "highs", "sub", "transition", "video", "cut", "hard", "soft", "ring",
    "silence", "mic", "microphone", "close", "room", "empty", "use",
}


def _search_terms(spec: dict[str, Any]) -> list[str]:
    """Search words to try in order: the planner's words, then words taken from the prompt."""
    terms = []
    if spec.get("search"):
        terms.append(str(spec["search"]))
    words = [w for w in re.findall(r"[a-z]+", str(spec.get("prompt", "")).lower())
             if w not in _STOP_WORDS and len(w) > 2]
    unique = list(dict.fromkeys(words))
    for size in (4, 2, 1):
        if unique[:size]:
            terms.append(" ".join(unique[:size]))
    return list(dict.fromkeys(terms))


def _lab_refs(dest: Path, generator: str) -> set[str]:
    """Ids this lab already downloaded from a library source, so a batch never repeats one."""
    for folder in (dest.parent, *dest.parent.parents):
        catalogue = folder / ".sfx-lab" / "sounds.jsonl"
        if catalogue.is_file():
            refs = set()
            for line in catalogue.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("generator") == generator and record.get("ref") and not record.get("failed"):
                    refs.add(record["ref"])
            return refs
    return set()


class EpidemicSound:
    """Search the Epidemic Sound sound-effects library and download the best unused match.

    Nothing is generated: each spec's "search" words (or words from its prompt) are searched,
    and the downloaded MP3 is billed under the user's Epidemic Sound plan, not per sound.
    """

    name = "epidemic"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._taken: set[str] = set()
        self._seeded = False

    def estimate_usd(self, spec: dict[str, Any]) -> float:
        return 0.0

    def _call(self, path: str, key: str) -> Any:
        base = os.environ.get("EPIDEMIC_SOUND_API_BASE", EPIDEMIC_API).rstrip("/")
        request = urllib.request.Request(base + path, headers={
            "Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "sfx-lab"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("message", body)
            except (ValueError, AttributeError):
                detail = body
            raise RuntimeError(f"Epidemic Sound {error.code}: {safe_message(str(detail))[:200]}") from error

    def _pick(self, results: list[dict[str, Any]], duration: float | None) -> dict[str, Any] | None:
        fresh = [r for r in results[:20] if r.get("id") and r["id"] not in self._taken]
        if not fresh:
            return None
        if duration is not None:
            def seconds(item: dict[str, Any]) -> float:
                length = item.get("length") or 0
                return length / 1000 if length > 600 else float(length)  # seconds; tolerate milliseconds
            # Keep relevance first: only reorder the best few matches by length.
            head = sorted(fresh[:5], key=lambda r: abs(seconds(r) - duration))
            fresh = head + fresh[5:]
        return fresh[0]

    def generate(self, spec: dict[str, Any], dest: Path) -> dict[str, Any]:
        key = os.environ.get("EPIDEMIC_SOUND_API_KEY")
        if not key:
            raise RuntimeError("Set EPIDEMIC_SOUND_API_KEY (Studio Settings or <lab>/.env).")
        choice = term = None
        for term in _search_terms(spec):
            query = urllib.parse.urlencode({"term": term, "limit": 30, "sort": "best-match"})
            results = self._call(f"/v0/sound-effects/search?{query}", key).get("soundEffects", [])
            with self._lock:
                if not self._seeded:
                    self._taken |= _lab_refs(dest, self.name)
                    self._seeded = True
                choice = self._pick(results, spec.get("duration_s"))
                if choice:
                    self._taken.add(choice["id"])
                    break
        if not choice:
            raise RuntimeError(f"Epidemic Sound found no new sound for: {term}")
        ident = urllib.parse.quote(choice["id"], safe="")
        link = self._call(f"/v0/sound-effects/{ident}/download?format=mp3&quality=high", key)["url"]
        if urllib.parse.urlsplit(link).scheme != "https":
            raise RuntimeError("Epidemic Sound returned an invalid download link.")
        # The signed download link needs no API key; never send the key to the file host.
        with urllib.request.urlopen(urllib.request.Request(link, headers={"User-Agent": "sfx-lab"}), timeout=180) as response:
            audio = response.read()
        path = dest.with_suffix(".mp3")
        atomic_write(path, audio)
        return {"path": str(path), "ref": choice["id"], "cost_usd": 0.0,
                "title": choice.get("title", ""), "search": term}

    def settle(self, ref: str, keep: bool) -> None:
        pass


def load_generator(name: str) -> Generator:
    if name in ("elevenlabs", "elevenlabs-v3", "elevenlabs-v2"):
        return ElevenLabs(SFX_V2 if name == "elevenlabs-v2" else SFX_V3)
    if name == "elevenlabs-tts":
        return ElevenLabsTTS()
    if name in ("epidemic", "epidemic-sound", "epidemicsound"):
        return EpidemicSound()
    for entry in importlib.metadata.entry_points(group="sfx_lab.generators"):
        if entry.name == name:
            return entry.load()()
    if ":" in name:
        module, class_name = name.split(":", 1)
        return getattr(importlib.import_module(module), class_name)()
    raise ValueError(f"Unknown generator: {name}. Use elevenlabs, epidemic, an entry point, or package.module:ClassName.")
