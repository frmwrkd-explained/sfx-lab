import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sfx_lab import run as workflow
from sfx_lab.cli import main
from sfx_lab.context import normalize_hot
from sfx_lab.lab import Lab, atomic_write, read_text, write_json
from sfx_lab.ratings import latest, read_log
from sfx_lab.sheet import parse_sheet
from sfx_lab.taste import END, agent_part, read_taste
from tests.fakes import FakeAgent, FakeGenerator

PROFILE = "## Summary\nDry close sounds work.\n\n## Likes\n- crisp clicks (tentative)\n\n## Dislikes\n- muddy tails (tentative)\n\n## Settings that work\n- short\n\n## Try next\n- softer attack?"
SPECS = json.dumps([{"prompt": "dry click", "duration_s": 0.5}, {"prompt": "muddy thud", "explore": True, "why": "longer tail"}])


def rate_sheet(path: Path, first: str = "love", second: str = "bad") -> None:
    text = read_text(path).replace("rating:: \nwhy:: ", f"rating:: {first}\nwhy:: crisp", 1)
    text = text.replace("rating:: \nwhy:: ", f"rating:: {second}\nwhy:: muddy", 1)
    atomic_write(path, text)


def test_full_cycle_settle_once_and_re_rating(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent([SPECS, PROFILE])
    data = workflow.plan(lab, "soft UI click", n=2, kind="ui", agent=agent)
    assert len(data["specs"]) == 2 and all(s["kind"] == "ui" for s in data["specs"])
    assert "Their own notes" in agent.prompts[0]
    with pytest.raises(ValueError, match="exceeds max-usd"):
        workflow.run(lab, yes=True, max_usd=0.001)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="Check the estimate, then add --yes"):
        workflow.run(lab)
    assert FakeGenerator.calls == []
    path = workflow.run(lab, yes=True)
    assert len(list(lab.audio_dir.glob("*.wav"))) == 2
    assert not workflow.pending_plans(lab)
    assert "2 unrated" in workflow.status(lab)
    rate_sheet(path)
    changes = workflow.learn(lab, agent=agent)
    assert len(changes) == 2
    assert FakeGenerator.settlements == [(f"sfx-{data['batch']}-01", True), (f"sfx-{data['batch']}-02", False)]
    assert "## Likes\n- crisp clicks" in read_taste(lab.taste_path).auto
    assert "state: learned" in read_text(path)
    assert "Total rated: 2" in workflow.status(lab)
    assert workflow.learn(lab, agent=agent) == []
    atomic_write(path, read_text(path).replace("rating:: bad", "rating:: good"))
    workflow.learn(lab, no_agent=True)
    assert len(FakeGenerator.settlements) == 2
    events = read_log(lab.ratings_path)
    assert len(events) == 3 and all(e["settled"] for e in events)
    assert latest(events)[(data["batch"], 2)]["rating"] == "4"
    assert agent_part(read_taste(lab.taste_path).auto) == normalize_hot(PROFILE)
    assert json.loads(read_text(lab.internal / "projects/general.json"))["distilled_events"] == 2
    retry = FakeAgent([PROFILE])
    assert workflow.learn(lab, agent=retry) == []
    new_ratings = retry.prompts[0].split("## New ratings\n", 1)[1].split("\n\nWrite the updated", 1)[0]
    assert new_ratings.count('- **') == 1
    assert '**4★** — "muddy thud"' in new_ratings
    assert json.loads(read_text(lab.internal / "projects/general.json"))["distilled_events"] == 3
    assert "[experiment: longer tail]" in agent.prompts[1]
    assert ('End every bullet in Likes, Dislikes and Settings that work with how many ratings '
            'support it, taken only from the ratings above — never estimate — e.g. "(4 ratings)". '
            'With fewer than 3, write "(tentative, 1 rating)" or "(tentative, 2 ratings)".') in agent.prompts[1]


def test_bad_agent_reply_keeps_old_taste_and_logged_ratings(lab: Lab) -> None:
    workflow.plan(lab, "test", agent=FakeAgent([SPECS]))
    path = workflow.run(lab, yes=True)
    rate_sheet(path)
    old = read_taste(lab.taste_path)
    with pytest.raises(ValueError, match="Ratings are logged; the next sfx-lab learn retries the taste"):
        workflow.learn(lab, agent=FakeAgent(["## Summary\nMissing required sections"]))
    refreshed = read_taste(lab.taste_path)
    assert agent_part(refreshed.auto) == agent_part(old.auto)
    assert refreshed.notes == old.notes
    assert "## Numbers\n| Setting" in refreshed.auto
    assert len(read_log(lab.ratings_path)) == 2
    assert len(FakeGenerator.settlements) == 2
    assert not (lab.internal / "state.json").exists()
    retry = FakeAgent([PROFILE])
    assert workflow.learn(lab, agent=retry) == []
    new_ratings = retry.prompts[0].split("## New ratings\n", 1)[1].split("\n\nWrite the updated", 1)[0]
    assert '**5★** — "dry click"' in new_ratings
    assert '**1★** — "muddy thud"' in new_ratings
    assert agent_part(read_taste(lab.taste_path).auto) == normalize_hot(PROFILE)
    assert json.loads(read_text(lab.internal / "projects/general.json"))["distilled_events"] == 2
    assert len(FakeGenerator.settlements) == 2


def test_agent_failure_retries_without_new_ratings(lab: Lab) -> None:
    workflow.plan(lab, "test", agent=FakeAgent([SPECS]))
    path = workflow.run(lab, yes=True)
    rate_sheet(path)
    old_profile = agent_part(read_taste(lab.taste_path).auto)

    class FailingAgent:
        def ask(self, prompt: str) -> str:
            raise TimeoutError("agent timed out")

    with pytest.raises(RuntimeError, match="agent timed out.*Ratings are logged; the next sfx-lab learn retries the taste"):
        workflow.learn(lab, agent=FailingAgent())
    refreshed = read_taste(lab.taste_path)
    assert agent_part(refreshed.auto) == old_profile
    assert "## Best examples\n- **5★**" in refreshed.auto
    assert "## Worst examples\n- **1★**" in refreshed.auto
    assert "## Numbers\n| Setting" in refreshed.auto
    assert not (lab.internal / "state.json").exists()
    retry = FakeAgent([PROFILE])
    messages: list[str] = []
    assert workflow.learn(lab, agent=retry, emit=messages.append) == []
    new_ratings = retry.prompts[0].split("## New ratings\n", 1)[1].split("\n\nWrite the updated", 1)[0]
    assert '**5★** — "dry click"' in new_ratings
    assert '**1★** — "muddy thud"' in new_ratings
    assert agent_part(read_taste(lab.taste_path).auto) == normalize_hot(PROFILE)
    assert json.loads(read_text(lab.internal / "projects/general.json"))["distilled_events"] == 2
    assert messages == ["Logged 0 ratings (0 new). Taste updated."]
    assert len(read_log(lab.ratings_path)) == len(FakeGenerator.settlements) == 2
    assert workflow.learn(lab, agent=FakeAgent([])) == []


def test_no_agent_defers_distillation_and_deduplicates_pending_ratings(lab: Lab) -> None:
    workflow.plan(lab, "test", agent=FakeAgent([SPECS]))
    path = workflow.run(lab, yes=True)
    rate_sheet(path)
    messages: list[str] = []
    workflow.learn(lab, no_agent=True, agent=FakeAgent([]), emit=messages.append)
    assert messages == ["Logged 2 ratings (2 new). Settled 2 (kept 1, discarded 1). Numbers refreshed; 2 ratings wait for the agent (run sfx-lab learn)."]
    assert not (lab.internal / "state.json").exists()
    atomic_write(path, read_text(path).replace("rating:: bad", "rating:: good"))
    workflow.learn(lab, no_agent=True, agent=FakeAgent([]))
    assert len(read_log(lab.ratings_path)) == 3
    assert not (lab.internal / "state.json").exists()
    retry = FakeAgent([PROFILE])
    assert workflow.learn(lab, agent=retry) == []
    new_ratings = retry.prompts[0].split("## New ratings\n", 1)[1].split("\n\nWrite the updated", 1)[0]
    assert new_ratings.count('- **') == 2
    assert '**5★** — "dry click"' in new_ratings
    assert '**4★** — "muddy thud"' in new_ratings
    assert '**1★**' not in new_ratings
    assert json.loads(read_text(lab.internal / "projects/general.json"))["distilled_events"] == 3
    assert len(FakeGenerator.settlements) == 2


def test_no_undistilled_events_does_not_load_agent(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Agent must not be loaded without undistilled events")

    monkeypatch.setattr(workflow, "load_agent", unexpected)
    assert workflow.learn(lab) == []


def test_checkpoint_waits_for_successful_taste_write(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow.plan(lab, "test", agent=FakeAgent([SPECS]))
    path = workflow.run(lab, yes=True)
    rate_sheet(path)
    workflow.learn(lab, agent=FakeAgent([PROFILE]))
    atomic_write(path, read_text(path).replace("rating:: bad", "rating:: good"))
    write_auto = workflow.write_auto

    def failed_write(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(workflow, "write_auto", failed_write)
    with pytest.raises(OSError, match="disk full"):
        workflow.learn(lab, agent=FakeAgent([PROFILE]))
    assert json.loads(read_text(lab.internal / "projects/general.json"))["distilled_events"] == 2
    monkeypatch.setattr(workflow, "write_auto", write_auto)
    retry = FakeAgent([PROFILE])
    workflow.learn(lab, agent=retry)
    new_ratings = retry.prompts[0].split("## New ratings\n", 1)[1].split("\n\nWrite the updated", 1)[0]
    assert new_ratings.count('- **') == 1
    assert '**4★** — "muddy thud"' in new_ratings
    assert json.loads(read_text(lab.internal / "projects/general.json"))["distilled_events"] == 3


def test_plan_retry_invalid_entries_limit_and_explicit_file(lab: Lab, tmp_path: Path) -> None:
    agent = FakeAgent(["not JSON", 'prose [null, {}, {"prompt":"one"}, {"prompt":"two"}] trailing'])
    data = workflow.plan(lab, "test", n=1, agent=agent)
    assert [s["prompt"] for s in data["specs"]] == ["one"]
    assert agent.prompts[1] == agent.prompts[0] + "\nReply with only the JSON array."
    file = tmp_path / "specs.json"
    atomic_write(file, SPECS)
    data2 = workflow.plan(lab, "from file", specs_file=file, agent=FakeAgent([]))
    assert data2["batch"] != data["batch"]
    assert len(data2["specs"]) == 2
    next_agent = FakeAgent([SPECS])
    workflow.plan(lab, "click", agent=next_agent)
    assert "- dry click" in next_agent.prompts[0]
    assert "- muddy thud" not in next_agent.prompts[0]
    assert "- one" not in next_agent.prompts[0]
    atomic_write(file, "{}")
    with pytest.raises(ValueError, match="JSON array"):
        workflow.plan(lab, "invalid", specs_file=file)
    with pytest.raises(ValueError, match="at least 1"):
        workflow.plan(lab, "invalid", n=0)


def test_partial_failure_unknown_rating_and_missing_generator(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    workflow.plan(lab, "partial", agent=FakeAgent(['[{"prompt":"dry"},{"prompt":"fail"}]']))
    path = workflow.run(lab, yes=True)
    assert "**Failed:** deliberate failure" in read_text(path)
    assert len(parse_sheet(path)) == 1
    atomic_write(path, read_text(path).replace("rating:: \n", "rating:: wonderful\n"))
    assert workflow.learn(lab, no_agent=True) == []
    assert "#1" in capsys.readouterr().err
    assert "state: open" in read_text(path)
    text = read_text(path).replace("rating:: wonderful", "rating:: ok")
    text = text.replace('"generator":"tests.fakes:FakeGenerator"', '"generator":"unavailable"')
    atomic_write(path, text)
    changes = workflow.learn(lab, no_agent=True)
    assert changes[0]["settled"] is False
    assert "could not settle" in capsys.readouterr().err
    assert "state: learned" in read_text(path)
    atomic_write(path, read_text(path).replace("rating:: ok", "rating:: love"))
    changes = workflow.learn(lab, no_agent=True)
    assert changes[0]["settled"] is False
    assert "could not settle" in capsys.readouterr().err


def test_interactive_confirmation(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow.plan(lab, "test", agent=FakeAgent([SPECS]))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with pytest.raises(ValueError, match="cancelled"):
        workflow.run(lab)
    monkeypatch.setattr("builtins.input", lambda prompt: "Y")
    assert workflow.run(lab).is_file()
    with pytest.raises(ValueError, match="already has a sheet"):
        workflow.run(lab, batch=lab.plans()[0].stem, yes=True)
    with pytest.raises(ValueError, match="No plans"):
        workflow.run(lab, yes=True)


def test_cli_end_to_end_with_fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "cli-lab"
    assert main(["init", str(root), "--generator", "tests.fakes:FakeGenerator", "--embed", "html"]) == 0
    config_lab = Lab.open(root)
    write_json(config_lab.internal / "config.json", config_lab.config | {"sheets": True})
    agent = FakeAgent([SPECS, PROFILE, json.dumps([{"prompt": "soft paper rustle"}])])
    monkeypatch.setattr(workflow, "load_agent", lambda name, model: agent)
    base = ["--lab", str(root)]
    assert main(base + ["plan", "test", "-n", "2"]) == 0
    assert main(base + ["run", "--yes"]) == 0
    lab = Lab.open(root)
    rate_sheet(lab.sheets()[0])
    assert main(base + ["learn"]) == 0
    assert main(base + ["status"]) == 0
    assert "Total rated: 2" in capsys.readouterr().out
    assert main(base + ["batch", "another", "--yes"]) == 0
    assert len(lab.sheets()) == 2
    assert main(["--help"]) == 0
    assert "init,plan,run,batch,learn,status" in capsys.readouterr().out
    assert main(["unknown-command"]) == 1


def test_cli_errors_and_no_key_leaks(lab: Lab, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "do-not-print-this-key")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bad do-not-print-this-key")

    monkeypatch.setattr(workflow, "load_agent", broken)
    args = ["--lab", str(lab.root), "plan", "test"]
    assert main(args) == 1
    output = capsys.readouterr()
    assert "[redacted]" in output.err and "Traceback" not in output.err
    assert "do-not-print-this-key" not in output.err
    monkeypatch.setenv("SFX_LAB_DEBUG", "1")
    assert main(args) == 1
    output = capsys.readouterr()
    assert "Traceback" in output.err and "do-not-print-this-key" not in output.err


def test_no_agent_preserves_notes_and_agent_part(lab: Lab) -> None:
    old = read_text(lab.taste_path)
    old = old.replace("Write anything", "Special note. Write anything")
    atomic_write(lab.taste_path, old)
    profile = agent_part(read_taste(lab.taste_path).auto)
    notes = old.split(END)[1]
    workflow.learn(lab, no_agent=True)
    assert agent_part(read_taste(lab.taste_path).auto) == profile
    assert read_text(lab.taste_path).split(END)[1] == notes


def test_installed_cli_subprocess_cycle(tmp_path: Path) -> None:
    binary = Path(sys.executable).with_name("sfx-lab")
    shim = tmp_path / "bin/claude"
    shim_text = ("#!/usr/bin/env python3\nimport json, sys\n"
                 f"result = {PROFILE!r} if sys.argv[2].startswith('You keep') else {SPECS!r}\n"
                 "print(json.dumps({'subtype': 'success', 'result': result}))\n")
    atomic_write(shim, shim_text)
    shim.chmod(0o755)
    env = os.environ | {"PATH": os.pathsep.join((str(shim.parent), str(binary.parent), os.environ["PATH"])),
                        "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    root = tmp_path / "subprocess-lab"

    def invoke(*args: str) -> str:
        result = subprocess.run([str(binary), *args], env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert "Taste:" in invoke("init", str(root), "--generator", "tests.fakes:FakeGenerator")
    config_lab = Lab.open(root)
    write_json(config_lab.internal / "config.json", config_lab.config | {"sheets": True})
    assert "Estimated cost: $0.020" in invoke("--lab", str(root), "batch", "click", "--yes")
    lab = Lab.open(root)
    rate_sheet(lab.sheets()[0])
    assert "Logged 2 ratings (2 new). Settled 2 (kept 1, discarded 1). Taste updated." in invoke("--lab", str(root), "learn")
    assert "Total rated: 2" in invoke("--lab", str(root), "status")
    assert "## Likes\n- crisp clicks" in read_taste(lab.taste_path).auto


def test_learn_with_nothing_rated_says_so(lab: Lab) -> None:
    workflow.plan(lab, "soft UI click", n=2, agent=FakeAgent([SPECS]))
    workflow.run(lab, yes=True)
    agent = FakeAgent([])
    messages: list[str] = []
    assert workflow.learn(lab, agent=agent, emit=messages.append) == []
    assert agent.prompts == []
    assert messages == ["No new ratings. 2 sounds are unrated: fill in rating:: in the batch sheet, "
                        "save it, then run sfx-lab learn again."]
