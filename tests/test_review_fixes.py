"""Regressions from the real-lab Part A review; no agent service or playback."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sfx_lab import run as workflow
from sfx_lab.context import hot_card, hot_values
from sfx_lab.lab import Lab, read_text, write_json
from sfx_lab.projects import get_project
from sfx_lab.prompts import learn_prompt
from sfx_lab.studio import Studio
from sfx_lab.tags import tags_for
from sfx_lab.taste import read_taste, write_auto
from tests.fakes import FakeAgent, FakeGenerator
from tests.test_projects import generate
from tests.test_run import PROFILE
from tests.test_studio import post, server, studio_lab, wait_job

FALLBACK_PROFILE = """## Summary
Close physical sounds work.

## Likes
- **Texture:** **Organic paper rustle** — close and physical (4 ratings)
- **Tight airy whooshes** (3 ratings)
- Round soft pops: restrained attack
- short rubber taps, avoid harsh attacks

## Dislikes
- **Avoid:** Synth tones — too musical
- Long tails (4 ratings)
- Musical chimes, too distracting

## Settings that work
- short lengths

## Try next
- **Question:** Glass vs metal clicks? — compare material
- Longer whooshes at low influence? (tentative)

## Hot
Likes:
Avoid:
Open:
"""
EXPECTED_HOT = {
    'Likes': 'organic paper rustle · tight airy whooshes · round soft pops · short rubber taps',
    'Avoid': 'synth tones · long tails · musical chimes',
    'Open': 'glass vs metal clicks? · longer whooshes at low influence?',
}


def test_review_learn_prompt_has_filled_hot_example(lab: Lab) -> None:
    prompt = learn_prompt(read_taste(lab.taste_path), '', [], '', '')
    assert 'Fill every line; never leave one empty.' in prompt
    assert 'Likes: tight airy whooshes · organic paper rustle · round soft pops' in prompt
    assert 'Avoid: synth tones · long tails · musical chimes' in prompt
    assert 'Open: glass vs metal clicks? · longer whooshes at low influence?' in prompt


def test_review_learn_writes_fallback_hot(lab: Lab) -> None:
    sound = generate(lab, ['paper click'])[0]
    Studio(lab).rate({'id': sound['id'], 'stars': 4})
    workflow.learn(lab, agent=FakeAgent([FALLBACK_PROFILE]), emit=lambda _: None)
    stored = read_taste(lab.taste_path).auto
    assert hot_values(stored) == EXPECTED_HOT
    for key, value in EXPECTED_HOT.items():
        assert f'{key}: {value}' in stored


@pytest.mark.parametrize('hot', ['', '\n\n## Hot\nLikes:\nAvoid:\nOpen:'])
def test_review_context_renders_fallback_without_agent(lab: Lab, hot: str) -> None:
    sound = generate(lab, ['paper click'])[0]
    Studio(lab).rate({'id': sound['id'], 'stars': 4})
    profile = FALLBACK_PROFILE.split('\n\n## Hot')[0] + hot
    write_auto(lab.taste_path, profile, '', '', '', 1)
    before = read_text(lab.taste_path)
    card = hot_card(lab)
    for key, value in EXPECTED_HOT.items():
        assert f'{key}: {value}' in card
    assert read_text(lab.taste_path) == before


def test_review_fallback_cuts_whole_phrases_and_preserves_filled_lines() -> None:
    profile = FALLBACK_PROFILE.replace('Organic paper rustle', 'a' * 91)
    profile = profile.replace('Likes:\n', 'Likes: already approved\n')
    assert hot_values(profile)['Likes'] == 'already approved'
    profile = profile.replace('Likes: already approved\n', 'Likes:\n')
    assert hot_values(profile)['Likes'] == 'tight airy whooshes · round soft pops · short rubber taps'
    profile = profile.replace('Longer whooshes at low influence?', 'q' * 111)
    assert hot_values(profile)['Open'] == 'glass vs metal clicks?'


@pytest.mark.parametrize(('prompt', 'absent', 'present'), [
    ('dry pop, absolutely no reverb', {'reverb'}, {'dry', 'pop'}),
    ('no reverb', {'reverb'}, set()),
    ('short reverb tail', set(), {'short', 'reverb', 'tail'}),
    ('without low rumble', {'rumble'}, set()),
    ('not bright metallic chimes', {'bright', 'metal', 'chime'}, set()),
    ('zero long reverb tails', {'long', 'reverb', 'tail'}, set()),
    ('never harsh digital buzz', {'harsh', 'digital', 'buzz'}, set()),
    ('minus deep sub-bass rumble', {'sub-bass', 'rumble'}, set()),
    ('free of low reverb tails', {'reverb', 'tail'}, set()),
    ('minimal reverb little rumble', set(), {'minimal', 'reverb', 'rumble'}),
    ('no long dark tail bright click', {'long', 'dark', 'tail'}, {'bright', 'click'}),
    ('no reverb, a short reverb tail', set(), {'reverb', 'tail'}),
])
def test_review_negated_tags(prompt: str, absent: set[str], present: set[str]) -> None:
    tags = set(tags_for(prompt))
    assert not tags & absent
    assert tags >= present


def test_review_sound_type_vocabulary() -> None:
    expected = set('whoosh swipe click tap knock pop rustle slide hit impact thud riser buzz beep chime glitch shutter typing scroll tick sparkle ambience room-tone'.split())
    assert set(tags_for(' '.join(sorted(expected)))) >= expected


def test_review_best_types_unique_ids_stars_then_type_count(lab: Lab) -> None:
    prompts = ['dry bright click pop tail', 'airy whoosh', 'slow whoosh', 'metal tap',
               'soft tap', 'light tap', 'paper rustle', 'rubber knock', 'camera shutter',
               'electronic glitch', 'soft thud']
    sounds = sorted(generate(lab, prompts), key=lambda sound: sound["n"])
    stars = [5, 5, 3, 4, 2, 3, 4, 4, 4, 4, 4]
    for sound, rating in zip(sounds, stars):
        Studio(lab).rate({'id': sound['id'], 'stars': rating})
    best = next(line for line in hot_card(lab).splitlines() if line.startswith('Best:'))
    entries = best.removeprefix('Best: ').split(' · ')
    parsed = [re.fullmatch(r'([a-z-]+) (\d{4}-\d{2}-\d{2}-\d+-\d+) \(([45])★\)', e) for e in entries]
    assert all(parsed)
    types = [m[1] for m in parsed if m]
    ids = [m[2] for m in parsed if m]
    assert len(entries) == len(set(ids)) == 6
    assert types[:3] == ['whoosh', 'click', 'tap']
    assert types.count('pop') == 0
    assert not set(types) & {'dry', 'bright', 'tail', 'metal', 'soft'}
    assert [int(m[3]) for m in parsed if m] == [5, 5, 4, 4, 4, 4]


def test_review_studio_learn_summary_reports_eight_settlements(server: Any, studio_lab: Lab) -> None:
    sounds = generate(studio_lab, [f'paper click variant {i}' for i in range(8)])
    for index, sound in enumerate(sounds):
        post(server, '/api/rate', {'id': sound['id'], 'stars': 1 if index == 0 else 4})
    server.studio.agent = FakeAgent([PROFILE, PROFILE])
    learned = wait_job(server, post(server, '/api/learn', {}))
    assert learned['state'] == 'done'
    assert learned['result'] == 'Logged 0 ratings (0 new). Settled 8 (kept 7, discarded 1). Taste updated.'
    assert len(FakeGenerator.settlements) == 8
    post(server, '/api/rate', {'id': sounds[0]['id'], 'stars': 4})
    rerated = wait_job(server, post(server, '/api/learn', {}))
    assert rerated['result'] == 'Logged 0 ratings (0 new). Taste updated.'
    assert len(FakeGenerator.settlements) == 8


def test_review_settings_without_evidence(lab: Lab) -> None:
    assert 'Settings: no evidence yet · loop only for beds' in hot_card(lab)
    project = get_project(lab)
    write_json(lab.internal / 'projects/general.json', project | {'defaults': project['defaults'] | {'influence': [.6, .8]}})
    assert 'Settings: auto · influence 0.6–0.8 · loop only for beds' in hot_card(lab)
    write_json(lab.internal / 'projects/general.json', project | {'defaults': project['defaults'] | {'length': [.5, .7]}})
    assert 'Settings: 0.5–0.7 s · influence 0–1 · loop only for beds' in hot_card(lab)


def test_review_failed_settlements_are_not_counted(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    sounds = sorted(generate(lab, ['dry click', 'soft pop']), key=lambda sound: sound['n'])
    studio = Studio(lab)
    for sound in sounds:
        studio.rate({'id': sound['id'], 'stars': 4})
    settle = FakeGenerator.settle
    def fail_one(self: FakeGenerator, ref: str, keep: bool) -> None:
        if ref == sounds[0]['ref']:
            raise RuntimeError('settlement unavailable')
        settle(self, ref, keep)
    monkeypatch.setattr(FakeGenerator, 'settle', fail_one)
    messages: list[str] = []
    workflow.learn(lab, agent=FakeAgent([PROFILE]), emit=messages.append)
    assert messages[-1] == 'Logged 0 ratings (0 new). Settled 1 (kept 1, discarded 0). Taste updated.'
    monkeypatch.setattr(FakeGenerator, 'settle', settle)
    workflow.learn(lab, no_agent=True, emit=messages.append)
    assert messages[-1].startswith('Logged 0 ratings (0 new). Settled 1 (kept 1, discarded 0).')


def test_review_studio_toast_receives_complete_learn_summary(tmp_path: Path) -> None:
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not on PATH')
    source = Path(__file__).resolve().parents[1] / 'src/sfx_lab/studio/studio.js'
    script = tmp_path / 'learn-toast.cjs'
    script.write_text('''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const update = source.slice(source.indexOf('async function updateTaste()'), source.indexOf('let noteTimer'));
const summary = 'Logged 0 ratings (0 new). Settled 8 (kept 7, discarded 1). Taste updated.';
const toasts = [];
const context = vm.createContext({ learning:false, flushNote() {}, renderUpdate() {}, renderAll() {},
  mutationQueues:new Map(), api:async () => ({id:'learn'}), watchJob:async () => summary,
  st:{view:'library'}, refresh:async () => {}, toast:message => toasts.push(message) });
vm.runInContext(update, context);
vm.runInContext('updateTaste()', context).then(() => assert.deepEqual(toasts, [summary])).catch(error => {
  console.error(error); process.exitCode = 1;
});
''')
    result = subprocess.run([node, str(script), str(source)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
