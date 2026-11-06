"""Offline tests for the Epidemic Sound library source."""

import io
import json
import urllib.parse
from pathlib import Path

import pytest

from sfx_lab import generators
from sfx_lab.generators import EpidemicSound, _search_terms, load_generator
from sfx_lab.prompts import plan_prompt
from sfx_lab.spec import Spec
from sfx_lab.taste import Taste


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_api(monkeypatch, results):
    calls = []

    def urlopen(request, timeout=0):
        url = request.full_url
        headers = {k.lower(): v for k, v in request.header_items()}
        calls.append((url, headers))
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/v0/sound-effects/search":
            return _Response(json.dumps({"soundEffects": results, "pagination": {}, "links": {}}).encode())
        if parts.path.endswith("/download"):
            ident = parts.path.split("/")[3]
            return _Response(json.dumps({"url": f"https://cdn.example/{ident}.mp3", "expires": "x"}).encode())
        if parts.netloc == "cdn.example":
            return _Response(b"ID3" + parts.path.encode())
        raise AssertionError(url)

    monkeypatch.setattr(generators.urllib.request, "urlopen", urlopen)
    return calls


def test_spec_keeps_search_words_only_when_given():
    assert Spec.from_dict({"prompt": "x", "search": "  whoosh \n cloth  "}).to_dict()["search"] == "whoosh cloth"
    assert "search" not in Spec.from_dict({"prompt": "x"}).to_dict()


def test_search_terms_fall_back_to_prompt_words():
    terms = _search_terms({"prompt": "Transition whoosh for a hard cut, 1 second. Fast cloth swipe. Isolated, no music."})
    assert terms[0] == "whoosh fast cloth swipe"
    assert _search_terms({"search": "whoosh air", "prompt": "whoosh"})[0] == "whoosh air"


def test_load_and_prompt():
    assert load_generator("epidemic").name == "epidemic"
    assert load_generator("epidemic").estimate_usd({"prompt": "x"}) == 0.0
    text = plan_prompt(Taste("", ""), "", [], "whoosh", "sfx", 3, 0.3, source="epidemic")
    assert "Epidemic Sound library" in text and '"search"' in text
    assert "Epidemic" not in plan_prompt(Taste("", ""), "", [], "whoosh", "sfx", 3, 0.3)


def test_generate_downloads_unused_matches_without_leaking_key(tmp_path: Path, monkeypatch):
    lab = tmp_path / "lab"
    (lab / ".sfx-lab").mkdir(parents=True)
    (lab / "sfx library").mkdir()
    (lab / ".sfx-lab" / "sounds.jsonl").write_text(
        json.dumps({"id": "x", "generator": "epidemic", "ref": "a1", "failed": False}) + "\n", encoding="utf-8")
    calls = _fake_api(monkeypatch, [{"id": "a1", "title": "Used", "length": 1},
                                    {"id": "b2", "title": "Whoosh B", "length": 3},
                                    {"id": "c3", "title": "Whoosh C", "length": 1}])
    monkeypatch.setenv("EPIDEMIC_SOUND_API_KEY", "offline-test-credential")
    source = EpidemicSound()
    first = source.generate({"prompt": "whoosh", "search": "whoosh", "duration_s": 1.0}, lab / "sfx library" / "sfx-1")
    second = source.generate({"prompt": "whoosh", "search": "whoosh", "duration_s": 1.0}, lab / "sfx library" / "sfx-2")
    assert first["ref"] == "c3" and second["ref"] == "b2"  # a1 already in the lab; length preference then relevance
    assert Path(first["path"]).read_bytes().startswith(b"ID3") and first["cost_usd"] == 0.0
    api = [h for url, h in calls if "epidemicsound.com" in url]
    cdn = [h for url, h in calls if "cdn.example" in url]
    assert all(h["authorization"] == "Bearer offline-test-credential" for h in api)
    assert cdn and all("authorization" not in h for h in cdn)
    with pytest.raises(RuntimeError, match="no new sound"):
        source.generate({"prompt": "whoosh", "search": "whoosh"}, lab / "sfx library" / "sfx-3")


def test_generate_requires_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("EPIDEMIC_SOUND_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EPIDEMIC_SOUND_API_KEY"):
        EpidemicSound().generate({"prompt": "whoosh"}, tmp_path / "a")
