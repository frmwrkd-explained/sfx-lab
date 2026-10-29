"""Part A regressions: projects, bounded context, reuse and installation, offline."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from sfx_lab import run as workflow
from sfx_lab.catalogue import read_catalogue
from sfx_lab.cli import main
from sfx_lab.context import find, hot_card, hot_values, normalize_hot, row, show, taste_slice
from sfx_lab.lab import Lab, atomic_write, init_lab, read_text, write_json
from sfx_lab.projects import (create_project, defaults, get_project, list_projects,
                              select_project, slug_for, use_project, validate_defaults)
from sfx_lab.ratings import latest, read_log
from sfx_lab.studio import Studio
from sfx_lab.taste import read_taste, write_auto
from tests.fakes import FakeAgent, FakeGenerator
from tests.test_run import PROFILE
from tests.test_studio import post, request, server, studio_lab, wait_job


def generate(lab: Lab, prompts: list[str]) -> list[dict[str, Any]]:
    workflow.plan(lab, "click whoosh", n=len(prompts),
                  agent=FakeAgent([json.dumps([{"prompt": p, "duration_s": .5, "prompt_influence": .7} for p in prompts])]),
                  emit=lambda _: None)
    workflow.run(lab, yes=True, emit=lambda _: None)
    return read_catalogue(lab)[-len(prompts):]


def test_slug_and_project_copy(lab: Lab) -> None:
    assert slug_for(" Tech / Explainers! ") == "tech-explainers"
    assert slug_for("Éditions à Paris") == "editions-a-paris"
    assert len(slug_for("x" * 80)) == 40
    with pytest.raises(ValueError):
        slug_for("! 🐈")
    for invalid in ("../x", "UPPER", "", "x" * 41, "x/y"):
        with pytest.raises(ValueError):
            select_project(lab, invalid)
    base = get_project(lab)
    write_json(lab.internal / "projects/general.json", base | {"defaults": {"batch_size": 2, "influence": [.6, .8], "length": [.5, .7]}})
    write_auto(lab.taste_path, normalize_hot(PROFILE), "EXAMPLE", "", "", 0)
    before = read_text(lab.taste_path)
    project = create_project(lab, "Tech Explainers", "general", "Weekly show")
    selected = select_project(lab, project["slug"])
    assert project["description"] == "Weekly show"
    assert selected.taste_path == lab.root / "projects/tech-explainers/taste.md"
    assert read_taste(selected.taste_path).auto == read_taste(lab.taste_path).auto
    assert defaults(selected)["batch_size"] == 2
    assert read_text(lab.taste_path) == before
    assert selected.safe_path("projects/tech-explainers/hot.md").is_file()
    with pytest.raises(ValueError, match="exists"):
        create_project(lab, "Tech Explainers")
    assert {p['slug'] for p in list_projects(lab)} == {"general", "tech-explainers"}


def test_migration_preserves_files_and_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "old"
    lab = init_lab(root, taste_file="notes/my taste.md", generator="tests.fakes:FakeGenerator", sheets=True)
    sound = generate(lab, ["dry paper click"])[0]
    Studio(lab).rate({"id": sound["id"], "stars": 4})
    # Simulate the pre-project on-disk format, including a pending plan.
    shutil.rmtree(lab.internal / "projects")
    shutil.rmtree(root / "projects")
    for path in (lab.internal / "sounds.jsonl", lab.ratings_path):
        records = [json.loads(line) for line in read_text(path).splitlines()]
        for record in records:
            record.pop("project", None)
        atomic_write(path, "".join(json.dumps(r) + "\n" for r in records))
    for path in lab.plans():
        data = json.loads(read_text(path)); data.pop("project", None); write_json(path, data)
    write_json(lab.internal / "state.json", {"distilled_events": 1, "last_learn_ts": 12})
    preserved = {p.relative_to(root): p.read_bytes() for p in root.rglob("*")
                 if p.is_file() and not p.name.startswith("._") and p.name != "state.json"}
    migrated = Lab.open(root)
    assert migrated.taste_path == root / "notes/my taste.md"
    assert get_project(migrated)["taste_file"] == "notes/my taste.md"
    assert get_project(migrated)["distilled_events"] == 1
    assert "distilled_events" not in json.loads(read_text(lab.internal / "state.json"))
    assert all((root / p).read_bytes() == content for p, content in preserved.items())
    assert latest(read_log(lab.ratings_path))[sound["batch"], sound["n"]]["stars"] == 4
    assert read_catalogue(migrated)[0]["project"] == "general"
    assert hot_card(migrated).startswith("# SFX · General · 1 rated")


def test_active_project_resolution(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("One", "Two", "Three"):
        create_project(lab, name)
    assert select_project(lab).project == "general"
    use_project(lab, "one")
    assert select_project(lab).project == "one"
    monkeypatch.setenv("SFX_LAB_PROJECT", "two")
    assert select_project(lab).project == "two"
    assert select_project(lab, "three").project == "three"
    assert select_project(select_project(lab, "three"), "general").taste_path == lab.taste_path


def test_cross_project_ratings_notes_and_settle_owner(lab: Lab) -> None:
    sound = generate(lab, ["dry paper click"])[0]
    original = Studio(lab)
    original.rate({"id": sound["id"], "note": "general note"}, note_only=True)
    create_project(lab, "Other")
    other = Studio(select_project(lab, "other"))
    assert other.sound(sound["id"])["note"] == ""
    other.rate({"id": sound["id"], "stars": 1, "note": "wrong for this show"})
    workflow.learn(other.lab, agent=FakeAgent([PROFILE]), emit=lambda _: None)
    assert not FakeGenerator.settlements
    assert latest(read_log(lab.ratings_path), "other")[sound["batch"], sound["n"]]["stars"] == 1
    assert not latest(read_log(lab.ratings_path))
    assert "rating:: 1" not in read_text(lab.sheets()[0])
    original.rate({"id": sound["id"], "stars": 5})
    workflow.learn(lab, agent=FakeAgent([PROFILE]), emit=lambda _: None)
    assert FakeGenerator.settlements == [(sound["ref"], True)]
    other.rate({"id": sound["id"], "stars": 2})
    workflow.learn(other.lab, no_agent=True, emit=lambda _: None)
    assert FakeGenerator.settlements == [(sound["ref"], True)]
    assert set(show(lab, sound["id"])["ratings"]) == {"general", "other"}
    assert read_taste(other.lab.taste_path).auto != read_taste(lab.taste_path).auto


def test_hot_card_refresh_caps_buckets_and_hot_truncation(lab: Lab) -> None:
    empty = hot_card(lab)
    assert empty.splitlines()[0] == "# SFX · General · no ratings yet"
    assert len(empty.splitlines()) == 3 and empty.endswith('sfx-lab taste')
    profile = PROFILE + "\n\n## Hot\nLikes: " + "dry close paper " * 20 + ", fifth, sixth, seventh, eighth\nAvoid: " + "long reverb " * 20 + "\nOpen: " + "does air help clicks? " * 20
    profile = normalize_hot(profile)
    values = hot_values(profile)
    assert len(values["Likes"]) <= 90 and len(values["Avoid"]) <= 90 and len(values["Open"]) <= 110
    assert not values['Likes'].endswith('pape')
    sounds = generate(lab, ["dry paper click", "soft paper click", "close paper click"])
    for sound in sounds:
        Studio(lab).rate({"id": sound["id"], "stars": 5})
    workflow.learn(lab, agent=FakeAgent([profile]), emit=lambda _: None)
    card = hot_card(lab)
    assert re.search(r"# SFX · General · 3 rated · 5.0★ avg · taste \d{4}-\d{2}-\d{2}", card)
    assert len(card) <= 1400
    assert [line.split(":")[0] for line in card.splitlines()[1:]] == ["Likes", "Avoid", "Settings", "Best", "Open", "More"]
    assert 'Settings: 0.5–1 s · influence 0.6–0.8' in card
    assert "click " + sounds[-1]['id'] in card
    path = lab.root / "projects/general/hot.md"
    mtime = path.stat().st_mtime_ns
    assert hot_card(lab) == card and path.stat().st_mtime_ns == mtime
    Studio(lab).rate({"id": sounds[0]["id"], "stars": 1})
    assert "3.7★ avg" in hot_card(lab)
    assert path.stat().st_mtime_ns != mtime
    assert "## Hot" in read_taste(lab.taste_path).auto


def test_find_ranking_filters_rows_and_reuse(lab: Lab) -> None:
    sounds = generate(lab, ["dry glass click"] * 5)
    studio = Studio(lab)
    for sound, stars in zip(sounds[:4], (5, 4, 2, 1)):
        studio.rate({"id": sound["id"], "stars": stars, "note": "close and crisp"})
    matches = find(lab, "glass click")
    assert [s['stars'] for s in matches] == [5, 4, None, 2]
    assert len(find(lab, "glass click", include_bad=True)) == 5
    assert len(find(lab, "glass click", min_stars=4)) == 2
    assert re.match(r'\d{4}-\d{2}-\d{2}-\d+-\d+  5★  0.5s  sfx  "dry glass click"  — close and crisp  audio/', row(matches[0]))
    create_project(lab, "Fresh")
    fresh = select_project(lab, "fresh")
    assert not find(fresh, "glass click")
    assert len(find(fresh, "glass click", all_projects=True)) == 5
    Studio(fresh).rate({"id": sounds[0]['id'], "stars": 4})
    assert find(fresh, "glass click")[0]['stars'] == 4
    messages: list[str] = []
    assert workflow.need(lab, "glass click", n=2, emit=messages.append, agent=FakeAgent([])) is None
    assert messages[-1] == "Reusing 2 rated sounds. Add --generate for new ones."
    agent = FakeAgent([json.dumps([{"prompt": "soft rubber tap"}])])
    planned = workflow.need(lab, "glass click", n=3, emit=lambda _: None, agent=agent)
    assert planned and len(planned['specs']) == 1
    assert "Write exactly 1 sound specs" in agent.prompts[0]
    workflow.need(lab, "glass click", n=2, generate=True, emit=lambda _: None,
                  agent=FakeAgent([json.dumps([{"prompt": "paper snap"}, {"prompt": "wood tap"}])]))


def test_plan_slices_guard_defaults_and_single_replacement(lab: Lab) -> None:
    sounds = generate(lab, ["soft dry glass click close tiny tail", "harsh metallic click loud long echo"])
    studio = Studio(lab)
    studio.rate({"id": sounds[0]['id'], "stars": 5})
    studio.rate({"id": sounds[1]['id'], "stars": 1})
    write_auto(lab.taste_path, normalize_hot(PROFILE), "HUGE_EXAMPLE", "HUGE_WORST", "HUGE_NUMBERS", 2)
    create_project(lab, "Elsewhere")
    elsewhere = select_project(lab, "elsewhere")
    workflow.plan(elsewhere, "click", agent=FakeAgent(['[{"prompt":"other project click forbidden"}]']), emit=lambda _: None)
    meta = get_project(lab)
    write_json(lab.internal / "projects/general.json", meta | {"defaults": {"batch_size": 2, "kind": "ui", "influence": [.6, .8], "length": [.5, .7], "explore": .5, "max_usd": .001}})
    lab = select_project(lab, "general")
    agent = FakeAgent([json.dumps([{"prompt": "harsh metallic click loud long echo"}, {"prompt": "rubber pop", "duration_s": 20, "prompt_influence": .1}]),
                       json.dumps([{"prompt": "dry paper tap", "duration_s": None, "prompt_influence": 1}])])
    messages: list[str] = []
    result = workflow.plan(lab, "click", agent=agent, emit=messages.append, verbose=True)
    prompt = agent.prompts[0]
    assert hot_card(lab) in prompt
    assert 'Build on these' in prompt and sounds[0]['id'] in prompt
    assert 'Never produce anything like these' in prompt and sounds[1]['spec']['prompt'] in prompt
    assert not any(s in prompt for s in ('HUGE_EXAMPLE', 'HUGE_WORST', 'HUGE_NUMBERS', 'other project click forbidden'))
    assert 'Set `from` to the id you build on' in prompt
    assert messages[0] == f"Plan prompt: {len(prompt)} characters (~{len(prompt)/4:.0f} tokens)"
    assert len(agent.prompts) == 2 and 'Replace 1 rejected bad repeats' in agent.prompts[1]
    assert {s['prompt'] for s in result['specs']} == {'rubber pop', 'dry paper tap'}
    assert all(.6 <= s['prompt_influence'] <= .8 and .5 <= s['duration_s'] <= .7 for s in result['specs'])
    assert result['project'] == 'general' and result['kind'] == 'ui' and result['over_limit']
    assert workflow._bad_repeat('harsh metallic click loud long', [latest(read_log(lab.ratings_path))[sounds[1]['batch'], sounds[1]['n']]])
    # A second bad replacement is dropped, with no third agent call.
    bad = json.dumps([{'prompt': sounds[1]['spec']['prompt']}])
    retry = FakeAgent([bad, bad])
    with pytest.raises(ValueError, match='No valid'):
        workflow.plan(lab, 'click', n=1, agent=retry, emit=lambda _: None)
    assert len(retry.prompts) == 2


def test_l2_cli_and_context_project_option(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    sound = generate(lab, ['dry paper click'])[0]
    Studio(lab).rate({'id': sound['id'], 'stars': 4, 'note': 'close'})
    base = ['--lab', str(lab.root)]
    for args in (['find', 'paper', '--json'], ['show', sound['id'], '--json'], ['taste', '--json'], ['ratings', '--stars', '4-5', '--tag', 'paper', '--json']):
        assert main(base + args) == 0
        json.loads(capsys.readouterr().out)
    assert main(base + ['project', 'new', 'Show']) == 0
    capsys.readouterr()
    assert main(base + ['context', '--project', 'show']) == 0
    assert '# SFX · Show · no ratings yet' in capsys.readouterr().out
    assert main(base + ['ratings', '--stars', '9']) == 1
    assert 'Stars must' in capsys.readouterr().err
    assert 'Best examples' not in taste_slice(lab)
    assert 'Best examples' in taste_slice(lab, True)


@pytest.mark.parametrize('value', [{'influence': [1, 0]}, {'length': [-1, 40]}, {'batch_size': 0}, {'batch_size': True}, {'explore': 2}, {'max_usd': float('nan')}, {'kind': ''}, {'unknown': 1}])
def test_invalid_project_defaults(value: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_defaults(value)


def test_studio_project_settings_and_find_endpoints(server: Any, studio_lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ('ELEVENLABS_API_KEY', 'SFX_LAB_ANTHROPIC_KEY', 'ANTHROPIC_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    sound = generate(studio_lab, ['dry glass click'])[0]
    post(server, '/api/rate', {'id': sound['id'], 'stars': 4})
    created = post(server, '/api/projects', {'name': 'Tech Explainers', 'from': 'general', 'description': 'Weekly'})
    assert created['slug'] == 'tech-explainers'
    assert len(request(server, '/api/projects')[2]) == 2
    assert post(server, '/api/use', {'slug': created['slug']})['project'] == created['slug']
    assert request(server, '/api/state')[2]['sounds'][0]['stars'] is None
    post(server, '/api/rate', {'id': sound['id'], 'stars': 1})
    assert latest(read_log(studio_lab.ratings_path))[(sound['batch'], sound['n'])]['stars'] == 4
    status, _, result = request(server, '/api/settings', {'defaults': {'batch_size': 2, 'kind': 'foley', 'influence': [.6, .8], 'length': [.5, 1]}, 'agent': 'anthropic-api'}, 'PUT')
    assert status == 200 and result['defaults']['kind'] == 'foley'
    assert result['keys'] == {'elevenlabs': 'missing', 'anthropic': 'missing'}
    assert request(server, '/api/settings', {'defaults': {'batch_size': -1}}, 'PUT')[0] == 400
    status, _, connected = request(server, '/api/settings/keys', {'elevenlabs': 'offline-test-credential-a', 'anthropic': 'offline-test-credential-b'}, 'PUT')
    assert status == 200 and connected == {'elevenlabs': 'connected', 'anthropic': 'connected'}
    # exFAT ignores chmod; still assert the requested mode on filesystems that enforce it.
    probe = studio_lab.root / 'mode-probe'
    probe.touch()
    probe.chmod(0o600)
    if stat.S_IMODE(probe.stat().st_mode) == 0o600:
        assert stat.S_IMODE((studio_lab.root / '.env').stat().st_mode) == 0o600
    probe.unlink()
    for route in ('/api/settings', '/api/state', '/api/projects'):
        assert 'offline-test-credential-' not in json.dumps(request(server, route)[2])
    assert request(server, '/api/settings/keys', {'elevenlabs': 'one\ntwo'}, 'PUT')[0] == 400
    assert post(server, '/api/use', {'slug': 'general'})['defaults']['batch_size'] == 6
    result = request(server, '/api/find?q=glass&limit=1')[2]
    assert len(result) == 1 and result[0]['stars'] == 4
    assert request(server, '/api/find?q=glass&limit=0')[0] == 400


def test_job_keeps_origin_project_on_switch(server: Any, studio_lab: Lab) -> None:
    post(server, '/api/projects', {'name': 'Other'})
    entered, release = threading.Event(), threading.Event()
    class SlowAgent:
        def ask(self, prompt: str) -> str:
            entered.set()
            assert release.wait(4)
            return '[{"prompt":"dry paper click"}]'
    server.studio.agent = SlowAgent()
    job = post(server, '/api/plan', {'brief': 'click', 'n': 1})
    assert entered.wait(2)
    try:
        post(server, '/api/use', {'slug': 'other'})
    finally:
        release.set()
    done = wait_job(server, job)
    assert done['state'] == 'done' and done['result']['project'] == 'general'
    assert server.studio.lab.project == 'other'


def test_ready_json_exact_line(lab: Lab, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from sfx_lab import studio
    def stop(self: Any) -> None:
        raise KeyboardInterrupt
    monkeypatch.setattr(studio.StudioServer, 'serve_forever', stop)
    studio.serve(lab, 0, True, True)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    ready = json.loads(lines[0])
    assert set(ready) == {'url', 'token', 'lab'}
    assert re.fullmatch(r'http://127.0.0.1:\d+/', ready['url'])
    assert ready['lab'] == str(lab.root) and len(ready['token']) >= 32


def test_install_sh_fake_uv_idempotent_backup_and_uninstall(tmp_path: Path, lab: Lab) -> None:
    home, bins = tmp_path / 'home', tmp_path / 'bin'
    bins.mkdir(); home.mkdir()
    target = home / '.claude/skills/sfx-lab'
    target.mkdir(parents=True); (target / 'mine').write_text('preserve')
    uv = bins / 'uv'
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/uv-calls"\n'
                  'case "$*" in\n'
                  f'  "tool dir --bin") printf "%s\\n" "{bins}";;\n'
                  f'  "python find 3.12") printf "%s\\n" "{sys.executable}";;\n'
                  'esac\n')
    uv.chmod(0o755)
    tool = bins / 'sfx-lab'
    tool.write_text('#!/bin/sh\nprintf "0.1.0\\n"\n')
    tool.chmod(0o755)
    env = os.environ | {'HOME': str(home), 'PATH': str(bins) + os.pathsep + os.environ['PATH']}
    script = Path(__file__).resolve().parents[1] / 'install.sh'
    for _ in range(2):
        result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True, input='', timeout=15)
        assert result.returncode == 0, result.stderr
        assert target.is_symlink() and (target / 'SKILL.md').is_file()
    assert len(list(target.parent.glob('sfx-lab.bak-*'))) == 1
    assert (next(target.parent.glob('sfx-lab.bak-*')) / 'mine').read_text() == 'preserve'
    result = subprocess.run(['sh', str(script), '--uninstall'], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and not target.is_symlink()
    assert (lab.internal / 'config.json').is_file()
    assert 'tool install --force' in (home / 'uv-calls').read_text()
    assert 'tool uninstall sfx-lab' in (home / 'uv-calls').read_text()


def test_level_match_audio_graph_without_playing(tmp_path: Path) -> None:
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not on PATH')
    source = Path(__file__).resolve().parents[1] / 'src/sfx_lab/studio/studio.js'
    # Execute the actual graph controls against silent test doubles, never audio.
    script = tmp_path / 'gain.cjs'
    script.write_text('''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const controls = source.slice(source.indexOf('const media = new Audio();'), source.indexOf('function makeWS()'));
let sources = 0, resumes = 0, edges = [];
class AudioContext {
  constructor() { this.destination = 'output'; }
  createMediaElementSource() { sources++; return { connect: n => edges.push(n) }; }
  createGain() { return { gain: { value: 1 }, connect: n => edges.push(n) }; }
  resume() { resumes++; return Promise.resolve(); }
}
const context = vm.createContext({ Audio: class {}, AudioContext, localStorage: {getItem: () => null},
  $: () => ({}), audio: {feat: new Map([['quiet', {peak:.001}], ['normal', {peak:.5}], ['silent', {peak:0}]])}, toast: () => {} });
vm.runInContext(controls, context);
vm.runInContext("preparePlayback('quiet')", context);
assert.equal(vm.runInContext('playbackGain.gain.value', context), 30);
vm.runInContext("preparePlayback('normal')", context);
assert.equal(vm.runInContext('playbackGain.gain.value', context), 1.78);
vm.runInContext("levelMatch = false; applyLevelMatch('quiet')", context);
assert.equal(vm.runInContext('playbackGain.gain.value', context), 1);
vm.runInContext("levelMatch = true; applyLevelMatch('silent')", context);
assert.equal(vm.runInContext('playbackGain.gain.value', context), 1);
assert.equal(sources, 1); assert.equal(edges.length, 2); assert.equal(resumes, 2);
''')
    result = subprocess.run([node, str(script), str(source)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_last_ten_prompts_only_overlapping_project(lab: Lab) -> None:
    for index in range(12):
        workflow.plan(lab, 'click', agent=FakeAgent([json.dumps([{'prompt': f'paper click variant {index}'}])]), emit=lambda _: None)
    workflow.plan(lab, 'ambience', agent=FakeAgent(['[{"prompt":"water ambience unrelated"}]']), emit=lambda _: None)
    prompts = workflow._recent_prompts(lab, 'click')
    assert len(prompts) == 10 and prompts[0].endswith('11') and prompts[-1].endswith('2')
    assert not any('water' in p for p in prompts)


def test_feature_peak_cache(server: Any, studio_lab: Lab) -> None:
    sound = generate(studio_lab, ['soft click'])[0]
    result = post(server, '/api/features', {'id': sound['id'], 'peaks': [0, 1, .3], 'peak': .003,
                                          'bright': 1, 'decay': .2, 'body': .3, 'duration': .5})
    assert result['features']['peak'] == .003
    assert request(server, '/api/state')[2]['sounds'][0]['features']['peak'] == .003
    assert request(server, '/api/features', {'id': sound['id'], 'peaks': [1], 'peak': -1}, 'POST')[0] == 400


def test_fresh_defaults_notes_search_and_explore_cap(lab: Lab) -> None:
    meta = get_project(lab)
    write_json(lab.internal / 'projects/general.json', meta | {'defaults': {'batch_size': 1, 'explore': 0, 'kind': 'ui'}})
    general = select_project(lab, 'general')
    fresh = create_project(general, 'Fresh')
    copied = create_project(general, 'Copied', 'general')
    assert fresh['defaults']['batch_size'] == 6
    assert copied['defaults']['batch_size'] == 1
    data = workflow.plan(general, 'click', agent=FakeAgent(['[{"prompt":"paper click","explore":true}]']), emit=lambda _: None)
    assert not data['specs'][0]['explore']
    workflow.run(general, yes=True, emit=lambda _: None)
    sound = read_catalogue(general)[0]
    Studio(general).rate({'id': sound['id'], 'note': 'surprising velvet'}, note_only=True)
    assert find(general, 'velvet')[0]['id'] == sound['id']


def test_explicit_specs_also_pass_bad_guard(lab: Lab, tmp_path: Path) -> None:
    sound = generate(lab, ['harsh metal click long echo'])[0]
    Studio(lab).rate({'id': sound['id'], 'stars': 1})
    specs_file = tmp_path / 'supplied.json'
    specs_file.write_text('[{"prompt":"harsh metal click long echo"}]')
    agent = FakeAgent(['[{"prompt":"soft rubber tap"}]'])
    result = workflow.plan(lab, 'click', specs_file=specs_file, agent=agent, emit=lambda _: None)
    assert [s['prompt'] for s in result['specs']] == ['soft rubber tap']
    assert len(agent.prompts) == 1
