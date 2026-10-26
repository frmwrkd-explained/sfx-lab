"""Offline HTTP integration tests. Audio is served/decoded by clients, never played."""

import http.client
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from sfx_lab import run as workflow
from sfx_lab.catalogue import read_catalogue, sync_catalogue
from sfx_lab.lab import Lab, atomic_write, init_lab, read_text, write_json
from sfx_lab.ratings import append_events, examples, latest, parse_rating, read_log
from sfx_lab.sheet import write_sheet
from sfx_lab.spec import Spec
from sfx_lab.studio import StudioServer, create_server
from sfx_lab.tags import VOCABULARY, tags_for
from sfx_lab.taste import END, START, read_taste
from tests.fakes import FakeAgent, FakeGenerator
from tests.test_run import PROFILE, SPECS

CONNECT = socket.socket.connect
CREATE_CONNECTION = socket.create_connection


@pytest.fixture
def studio_lab(tmp_path: Path) -> Lab:
    return init_lab(tmp_path / "studio-lab", generator="tests.fakes:FakeGenerator")


@pytest.fixture
def server(studio_lab: Lab, monkeypatch: pytest.MonkeyPatch) -> Iterator[StudioServer]:
    def local_connect(sock: socket.socket, address: tuple[str, int]) -> Any:
        assert address[0] == "127.0.0.1", "Only loopback HTTP is allowed"
        return CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket, "create_connection", CREATE_CONNECTION)
    server = create_server(studio_lab, 0, FakeAgent([SPECS, PROFILE, SPECS]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    assert not thread.is_alive()


def request(server: StudioServer, path: str, data: Any = None, method: str = "GET",
            token: bool = True, headers: dict[str, str] | None = None,
            raw: bytes | None = None) -> tuple[int, dict[str, str], Any]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    fields = {"X-SFX-Lab-Token": server.studio.token} if token else {}
    fields.update(headers or {})
    body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
    try:
        conn.request(method, path, body, fields)
        response = conn.getresponse()
        content, status, info = response.read(), response.status, dict(response.getheaders())
        if info.get("Content-Type", "").startswith("application/json"):
            content = json.loads(content)
        return status, info, content
    finally:
        conn.close()


def post(server: StudioServer, path: str, data: dict[str, Any]) -> Any:
    status, _, value = request(server, path, data, "POST")
    assert status == 200, value
    return value


def wait_job(server: StudioServer, job: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, _, value = request(server, f"/api/jobs/{job['id']}")
        assert status == 200
        if value["state"] in ("done", "error"):
            return value
        time.sleep(0.01)
    pytest.fail("Job did not finish")


def generated(lab: Lab) -> list[dict[str, Any]]:
    workflow.plan(lab, "dry clicks", agent=FakeAgent([SPECS]), emit=lambda _: None)
    workflow.run(lab, yes=True, emit=lambda _: None)
    return sorted(read_catalogue(lab), key=lambda s: s["n"])


@pytest.mark.parametrize(("text", "stars"), [("4★", 4), ("★★★★", 4), ("4 star", 4),
                                             ("4 stars", 4), ("★★★★★", 5), ("2", 2), ("1", 1)])
def test_star_forms(text: str, stars: int) -> None:
    assert parse_rating(text) == stars


def test_old_log_migrates_in_memory_only(studio_lab: Lab) -> None:
    events = [{"batch": "old", "n": n, "rating": word, "score": n - 1, "spec": {"prompt": word},
               "ts": n, "settled": True, "note": "old"}
              for n, word in enumerate(("love", "good", "ok", "bad"), 1)]
    append_events(studio_lab.ratings_path, events)
    before = studio_lab.ratings_path.read_bytes()
    migrated = read_log(studio_lab.ratings_path)
    assert [e["stars"] for e in migrated] == [5, 4, 3, 1]
    assert [e["score"] for e in migrated] == [5, 4, 3, 1]
    assert studio_lab.ratings_path.read_bytes() == before
    best, worst = examples(migrated)
    assert "**5★**" in best and "**4★**" in best and "**1★**" in worst
    assert "**3★**" not in best
    two = migrated[0] | {"stars": 2, "score": 2, "rating": "2"}
    assert "**2★**" in examples([two])[1]


def test_tags_order_normalization_and_word_boundaries() -> None:
    assert len(VOCABULARY) >= 60
    assert tags_for("Wooden metallic CLICKS clicks softly, paper rustling; sub-bass tails") == [
        "metal", "wood", "paper", "click", "rustle", "soft", "tail", "sub-bass"]
    assert tags_for("airplane glassware") == []
    assert len(tags_for("glass glasses glass")) == 1


@pytest.mark.parametrize("parent", ["../x", "/etc/x", "wrong", 3, "2026-10-23-01"])
def test_spec_invalid_parent(parent: Any) -> None:
    assert Spec.from_dict({"prompt": "click", "from": parent}) is None


def test_sheets_default_and_old_config(studio_lab: Lab) -> None:
    assert studio_lab.config["sheets"] is False
    records = generated(studio_lab)
    assert len(records) == 2 and not studio_lab.sheets()
    config = dict(studio_lab.config)
    config.pop("sheets")
    write_json(studio_lab.internal / "config.json", config)
    assert Lab.open(studio_lab.root).config["sheets"] is True
    assert not workflow.pending_plans(studio_lab)
    assert "Catalogue: 2 sounds" in workflow.status(studio_lab)


def test_catalogue_lands_failures_and_import_is_idempotent(studio_lab: Lab) -> None:
    plan = workflow.plan(studio_lab, "partial", agent=FakeAgent(['[{"prompt":"click"},{"prompt":"fail"}]']))
    progress = []
    workflow.run(studio_lab, yes=True, progress=lambda record: progress.append((record, read_catalogue(studio_lab))))
    assert len(progress) == 2 and [len(r) for _, r in progress] == [1, 2]
    records = sorted(read_catalogue(studio_lab), key=lambda s: s["n"])
    assert records[1]["failed"] and records[1]["audio"] == "" and records[1]["error"] == "deliberate failure"
    assert records[0]["audio"].startswith("audio/sfx-")
    write_sheet(studio_lab, plan, [{"path": str(studio_lab.root / records[0]["audio"])}, {"error": "deliberate failure"}])
    (studio_lab.internal / "sounds.jsonl").unlink()
    first = sync_catalogue(studio_lab)
    before = (studio_lab.internal / "sounds.jsonl").read_bytes()
    assert len(first) == 2 and first[0]["audio"] == records[0]["audio"]
    assert sync_catalogue(studio_lab) == first
    assert (studio_lab.internal / "sounds.jsonl").read_bytes() == before


def test_server_security_assets_and_state(server: StudioServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sensitive-key-test-only")
    assert server.server_address[0] == "127.0.0.1"
    status, headers, body = request(server, "/", token=False)
    assert status == 200 and b'<meta name="sfx-lab-token"' in body
    assert server.studio.token.encode() in body and b"unpkg" not in body
    assert headers["Cache-Control"] == "no-cache"
    assert request(server, "/api/state", token=False)[0] == 403
    assert request(server, "/api/state", headers={"X-SFX-Lab-Token": "wrong"})[0] == 403
    assert request(server, "/", headers={"Host": "evil.example:1234"})[0] == 403
    assert request(server, "/api/state", headers={"Host": f"localhost:{server.server_port}"})[0] == 200
    for path in ("/audio/../x", "/static/../x", "/static/%2e%2e/x", "/audio/%2fetc/passwd", "/static/._studio.js"):
        assert request(server, path)[0] in (403, 404)
    for path, mime in (("studio.js", "javascript"), ("studio.css", "css"), ("vendor/Geist-Variable.woff2", "font/woff2")):
        status, fields, _ = request(server, "/static/" + path)
        assert status == 200 and mime in fields["Content-Type"]
        assert not any("cors" in k.lower() or k.lower() == "access-control-allow-origin" for k in fields)
    status, _, state = request(server, "/api/state")
    assert status == 200 and set(state) == {"lab", "sounds", "batches", "taste", "pending_updates", "jobs", "project", "projects", "defaults"}
    assert state["sounds"] == state["batches"] == state["jobs"] == []
    assert set(state["lab"]) == {"name", "root", "generator", "agent", "model", "max_usd", "explore", "batch_size"}
    assert "sensitive-key-test-only" not in json.dumps(state)
    assert request(server, "/api/rate", raw=b"x" * 65537, method="POST")[0] == 413
    assert request(server, "/api/rate", raw=b"{bad}", method="POST")[0] == 400
    assert request(server, "/api/rate", [], method="POST")[0] == 400
    assert request(server, "/api/jobs/missing")[0] == 404


def test_job_plan_run_and_lineage(server: StudioServer, studio_lab: Lab) -> None:
    plan_job = post(server, "/api/plan", {"brief": "dry clicks", "n": 2, "kind": "ui"})
    plan = wait_job(server, plan_job)
    assert plan["kind"] == "plan" and plan["state"] == "done" and plan["progress"] == 1
    result = plan["result"]
    assert result["estimate_usd"] == 0.02 and result["over_limit"] is False
    run = wait_job(server, post(server, "/api/run", {"batch": result["batch"]}))
    assert run["state"] == "done" and len(run["sounds"]) == 2 and run["progress"] == 1
    state = request(server, "/api/state")[2]
    assert len(state["sounds"]) == 2 and state["batches"][0]["unrated"] == 2
    parent = next(s for s in state["sounds"] if s["n"] == 1)
    assert {"stars", "note", "settled", "rated_at", "tags", "audio_url", "children", "parent"} <= parent.keys()
    assert parent["audio_url"].startswith("/audio/") and "dry" in parent["tags"]
    assert request(server, "/api/run", {"batch": result["batch"]}, "POST")[0] == 409
    server.studio.agent = FakeAgent([SPECS])
    child_plan = wait_job(server, post(server, "/api/plan", {"brief": "like this", "from": parent["id"]}))["result"]
    assert all(s["from"] == parent["id"] for s in child_plan["specs"])
    assert "Build on this sound" in server.studio.agent.prompts[0]
    assert wait_job(server, post(server, "/api/run", {"batch": child_plan["batch"]}))["state"] == "done"
    state = request(server, "/api/state")[2]
    assert len(next(s for s in state["sounds"] if s["id"] == parent["id"])["children"]) == 2
    children = [s for s in state["sounds"] if s["parent"] == parent["id"]]
    assert len(children) == 2
    assert len(read_catalogue(studio_lab)) == 4


def test_over_limit_run_refused(server: StudioServer, studio_lab: Lab) -> None:
    assert request(server, "/api/settings", {"defaults": {"max_usd": 0.001}}, "PUT")[0] == 200
    plan = wait_job(server, post(server, "/api/plan", {"brief": "click"}))["result"]
    assert plan["over_limit"] is True
    assert request(server, "/api/run", {"batch": plan["batch"]}, "POST")[0] == 409
    assert FakeGenerator.calls == []


def test_rating_notes_sheet_sync_and_settle_on_learn(server: StudioServer, studio_lab: Lab) -> None:
    studio_lab.config["sheets"] = True
    sounds = generated(studio_lab)
    ident = sounds[0]["id"]
    post(server, "/api/note", {"id": ident, "note": "crisp before rating"})
    assert read_log(studio_lab.ratings_path) == []
    assert json.loads(read_text(studio_lab.internal / "notes.json"))[ident] == "crisp before rating"
    rated = post(server, "/api/rate", {"id": ident, "stars": 2})
    assert rated["note"] == "crisp before rating" and rated["stars"] == 2
    assert rated["settled"] is False and FakeGenerator.settlements == []
    assert "rating:: 2\nwhy:: crisp before rating" in read_text(studio_lab.sheets()[0])
    post(server, "/api/rate", {"id": ident, "stars": 1})
    assert FakeGenerator.settlements == []
    server.studio.agent = FakeAgent([PROFILE, PROFILE, PROFILE])
    learned = wait_job(server, post(server, "/api/learn", {}))
    assert learned["state"] == "done" and "Taste updated." in learned["result"]
    assert FakeGenerator.settlements == [(sounds[0]["ref"], False)]
    assert request(server, "/api/state")[2]["pending_updates"] == 0
    post(server, "/api/rate", {"id": ident, "stars": 5, "note": "changed mind"})
    assert wait_job(server, post(server, "/api/learn", {}))["state"] == "done"
    assert len(FakeGenerator.settlements) == 1
    assert server.studio.sound(ident)["settled"] is True
    event = latest(read_log(studio_lab.ratings_path))[(sounds[0]["batch"], 1)]
    assert event["source"] == "studio" and event["rating"] == "5" and event["stars"] == event["score"] == 5
    assert "**5★**" in read_taste(studio_lab.taste_path).auto
    for stars in (0, 6, True, 2.5, "4"):
        assert request(server, "/api/rate", {"id": ident, "stars": stars}, "POST")[0] == 400
    post(server, "/api/rate", {"id": ident, "stars": None})
    post(server, "/api/note", {"id": ident, "note": "unrated again"})
    assert server.studio.sound(ident)["stars"] is None
    assert "rating:: \nwhy:: unrated again" in read_text(studio_lab.sheets()[0])


def test_stale_sheet_loses_and_newer_sheet_wins(server: StudioServer, studio_lab: Lab) -> None:
    import os
    studio_lab.config["sheets"] = True
    sound = generated(studio_lab)[0]
    post(server, "/api/rate", {"id": sound["id"], "stars": 4, "note": "studio"})
    sheet = studio_lab.sheets()[0]
    text = read_text(sheet).replace("rating:: 4", "rating:: 1").replace("why:: studio", "why:: stale")
    atomic_write(sheet, text)
    os.utime(sheet, (1, 1))
    workflow.learn(studio_lab, no_agent=True)
    assert server.studio.sound(sound["id"])["stars"] == 4
    atomic_write(sheet, text.replace("why:: stale", "why:: fresh"))
    workflow.learn(studio_lab, no_agent=True)
    assert server.studio.sound(sound["id"])["stars"] == 1
    assert server.studio.sound(sound["id"])["note"] == "fresh"
    assert len(FakeGenerator.settlements) == 1


def test_taste_notes_put_preserves_auto_and_other_sections(server: StudioServer, studio_lab: Lab) -> None:
    atomic_write(studio_lab.taste_path, read_text(studio_lab.taste_path) + "\n## Extra\nKeep this.\n")
    before = read_text(studio_lab.taste_path)
    status, _, taste = request(server, "/api/taste/notes", {"markdown": "Dry sounds.\n- Always short."}, "PUT")
    assert status == 200 and taste["notes_markdown"] == "Dry sounds.\n- Always short."
    after = read_text(studio_lab.taste_path)
    assert before.split(END)[0] == after.split(END)[0]
    assert after.endswith("## Extra\nKeep this.\n")
    assert request(server, "/api/taste")[2] == taste
    assert START in after


def test_audio_ranges_and_feature_cache(server: StudioServer, studio_lab: Lab) -> None:
    sound = generated(studio_lab)[0]
    url = server.studio.sound(sound["id"])["audio_url"]
    complete = request(server, url)
    assert complete[0] == 200 and complete[1]["Content-Type"] == "audio/wav"
    status, fields, data = request(server, url, headers={"Range": "bytes=2-6"})
    assert status == 206 and data == complete[2][2:7]
    assert fields["Content-Range"] == f"bytes 2-6/{len(complete[2])}" and fields["Content-Length"] == "5"
    assert request(server, url, headers={"Range": "bytes=-4"})[2] == complete[2][-4:]
    assert request(server, url, headers={"Range": "bytes=5-"})[2] == complete[2][5:]
    for value in ("bytes=999999-", "bytes=4-2", "bytes=1-2,4-5", "bytes=-0"):
        assert request(server, url, headers={"Range": value})[0] == 416
    result = post(server, "/api/features", {"id": sound["id"], "peaks": [0, 0.5, 1], "bright": 200, "decay": 0.5, "body": 0.4, "duration": 0.1})
    assert result["peaks"] == [0, 0.5, 1] and result["features"]["decay"] == 0.5
    assert request(server, "/api/state")[2]["sounds"][0].get("features") or any(s.get("features") for s in request(server, "/api/state")[2]["sounds"])
    for peaks in ([0] * 201, [-1], [float("nan")], [True]):
        assert request(server, "/api/features", {"id": sound["id"], "peaks": peaks}, "POST")[0] == 400
    path = studio_lab.root / sound["audio"]
    atomic_write(path, path.read_bytes() + b"changed")
    assert "peaks" not in server.studio.sound(sound["id"])


def test_symlink_and_config_paths_cannot_escape(server: StudioServer, studio_lab: Lab, tmp_path: Path) -> None:
    outside = tmp_path / "outside.wav"
    atomic_write(outside, b"private")
    (studio_lab.audio_dir / "escape.wav").symlink_to(outside)
    assert request(server, "/audio/escape.wav")[0] in (400, 403, 404)
    for path in ("../outside", str(tmp_path)):
        studio_lab.config["audio_dir"] = path
        with pytest.raises(ValueError, match="inside the lab"):
            _ = studio_lab.audio_dir
    studio_lab.config["audio_dir"] = "audio"
    (studio_lab.internal / "features.json").symlink_to(outside)
    assert request(server, "/api/state")[0] == 400


def test_job_exclusion_and_agent_errors(server: StudioServer, studio_lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    class SlowAgent:
        def ask(self, prompt: str) -> str:
            entered.set()
            assert release.wait(4)
            return SPECS
    server.studio.agent = SlowAgent()
    job = post(server, "/api/plan", {"brief": "slow"})
    assert entered.wait(2)
    try:
        assert request(server, "/api/plan", {"brief": "busy"}, "POST")[0] == 409
        assert request(server, "/api/learn", {}, "POST")[0] == 409
    finally:
        release.set()
    plan = wait_job(server, job)["result"]
    entered.clear()
    release.clear()
    generate = FakeGenerator.generate
    def slow_generate(self: FakeGenerator, spec: dict[str, Any], dest: Path) -> dict[str, Any]:
        entered.set()
        assert release.wait(4)
        return generate(self, spec, dest)
    monkeypatch.setattr(FakeGenerator, "generate", slow_generate)
    job = post(server, "/api/run", {"batch": plan["batch"]})
    assert entered.wait(2)
    try:
        status, _, error = request(server, "/api/run", {"batch": plan["batch"]}, "POST")
        assert status == 409 and error["error"] == "A batch is already generating"
    finally:
        release.set()
    assert wait_job(server, job)["state"] == "done"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-fake-value")
    class BrokenAgent:
        def ask(self, prompt: str) -> str:
            raise RuntimeError("Not signed in secret-fake-value")
    server.studio.agent = BrokenAgent()
    job = wait_job(server, post(server, "/api/plan", {"brief": "error"}))
    assert job["state"] == "error" and "Not signed in" in job["error"]
    assert "secret-fake-value" not in job["error"] and "[redacted]" in job["error"]


def test_reveal_uses_only_catalogued_audio(server: StudioServer, studio_lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    sound = generated(studio_lab)[0]
    commands = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: commands.append(argv))
    assert post(server, "/api/reveal", {"id": sound["id"]}) == {"ok": True}
    assert len(commands) == 1 and str(studio_lab.audio_dir) in " ".join(commands[0])
    assert request(server, "/api/reveal", {"id": "../../outside"}, "POST")[0] == 404


def test_js_syntax_and_local_assets() -> None:
    root = Path(__file__).resolve().parents[1] / "src/sfx_lab/studio"
    assert all((root / "vendor" / name).is_file() for name in (
        "wavesurfer.min.js", "wavesurfer.LICENSE", "table-core.min.js", "table-core.LICENSE",
        "Geist-Variable.woff2", "GeistMono-Variable.woff2", "geist.LICENSE"))
    html = read_text(root / "index.html")
    assert "https://" not in html and "Mockup" not in html
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    result = subprocess.run([node, "--check", str(root / "studio.js")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_ratings_and_notes_during_learn_remain_pending(server: StudioServer, studio_lab: Lab) -> None:
    sound = generated(studio_lab)[0]
    ident = sound["id"]
    post(server, "/api/rate", {"id": ident, "stars": 4})
    entered, release = threading.Event(), threading.Event()
    class SlowLearn:
        def ask(self, prompt: str) -> str:
            entered.set()
            assert release.wait(4)
            return PROFILE
    server.studio.agent = SlowLearn()
    job = post(server, "/api/learn", {})
    assert entered.wait(2)
    try:
        post(server, "/api/rate", {"id": ident, "stars": 5, "note": "arrived during learn"})
        assert request(server, "/api/taste/notes", {"markdown": "Always preserve this edit."}, "PUT")[0] == 200
    finally:
        release.set()
    assert wait_job(server, job)["state"] == "done"
    state = request(server, "/api/state")[2]
    assert state["pending_updates"] == 1
    assert state["taste"]["notes_markdown"] == "Always preserve this edit."
    assert server.studio.sound(ident)["stars"] == 5
    assert FakeGenerator.settlements == [(sound["ref"], True)]
    server.studio.agent = FakeAgent([PROFILE])
    assert wait_job(server, post(server, "/api/learn", {}))["state"] == "done"
    assert request(server, "/api/state")[2]["pending_updates"] == 0
    assert len(FakeGenerator.settlements) == 1


def test_custom_audio_directory_import_and_batch_reservation(studio_lab: Lab) -> None:
    studio_lab.config.update(audio_dir="media/sounds", embed="html")
    studio_lab.audio_dir.mkdir(parents=True)
    data = workflow.plan(studio_lab, "custom", agent=FakeAgent([SPECS]))
    results = [{"path": str(studio_lab.audio_dir / "one.wav")}, {"path": str(studio_lab.audio_dir / "two.wav")}]
    write_sheet(studio_lab, data, results)
    records = sync_catalogue(studio_lab)
    assert [s["audio"] for s in records] == ["media/sounds/one.wav", "media/sounds/two.wav"]
    # The catalogue still reserves an id if its original plan and sheet are removed.
    for path in studio_lab.plans() + studio_lab.sheets():
        path.unlink()
    assert studio_lab.next_batch_id() != data["batch"]
