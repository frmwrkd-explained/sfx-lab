import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from sfx_lab.generators import ElevenLabs, load_generator
from sfx_lab.spec import Spec
from tests.fakes import FakeGenerator


@pytest.mark.parametrize("duration", [None, 1.5])
def test_elevenlabs_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, duration: float | None) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-secret")
    seen: dict[str, Any] = {}

    def urlopen(request: urllib.request.Request, timeout: int) -> io.BytesIO:
        seen.update(url=request.full_url, headers=dict(request.header_items()),
                    body=json.loads(request.data or b""), timeout=timeout, method=request.method)
        return io.BytesIO(b"fake mp3")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    spec = Spec("click", duration, 0.6, True).to_dict()
    result = ElevenLabs().generate(spec, tmp_path / "sound")
    assert Path(result["path"]).read_bytes() == b"fake mp3"
    assert Path(result["path"]).suffix == ".mp3" and result["ref"] == ""
    assert result["cost_usd"] == pytest.approx(0.12 * (duration or 5) / 60)
    body = {"text": "click", "model_id": "eleven_text_to_sound_v2", "prompt_influence": 0.6, "loop": True}
    if duration is not None:
        body["duration_seconds"] = duration
    assert seen == {"url": "https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128", "headers": {"Xi-api-key": "fake-secret", "Content-type": "application/json"}, "body": body, "timeout": 180, "method": "POST"}
    assert ElevenLabs().settle("", False) is None


@pytest.mark.parametrize(("body", "detail"), [(b'{"detail":{"message":"quota exhausted"}}', "quota exhausted"), (b'{"detail":"denied"}', "denied"), (b"unavailable", "unavailable"), (b"a" * 250, "a" * 200)])
def test_elevenlabs_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: bytes, detail: str) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "secret-key")

    def urlopen(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.HTTPError("https://fake", 429, "oops", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError) as error:
        ElevenLabs().generate(Spec("click").to_dict(), tmp_path / "sound")
    assert str(error.value) == f"ElevenLabs 429: {detail}"
    assert not (tmp_path / "sound.mp3").exists()


def test_missing_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match=r"Set ELEVENLABS_API_KEY \(environment or <lab>/\.env\)\."):
        ElevenLabs().generate(Spec("click").to_dict(), tmp_path / "sound")


def test_loading_and_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(load_generator("elevenlabs"), ElevenLabs)
    assert isinstance(load_generator("tests.fakes:FakeGenerator"), FakeGenerator)
    with pytest.raises(ValueError, match="Unknown generator"):
        load_generator("unknown")

    class Entry:
        name = "registered"

        def load(self) -> type[FakeGenerator]:
            return FakeGenerator

    def entries(*, group: str) -> list[Entry]:
        assert group == "sfx_lab.generators"
        return [Entry()]

    monkeypatch.setattr("importlib.metadata.entry_points", entries)
    assert isinstance(load_generator("registered"), FakeGenerator)
