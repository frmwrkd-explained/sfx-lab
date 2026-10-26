"""Feedback plans include the chosen sound and preserve lineage, offline."""

import json

import pytest

from sfx_lab import run as workflow
from sfx_lab.catalogue import read_catalogue
from sfx_lab.cli import main
from sfx_lab.lab import write_json
from sfx_lab.projects import create_project, select_project
from sfx_lab.ratings import append_events
from tests.fakes import FakeAgent


@pytest.fixture
def parent(lab):
    workflow.plan(lab, 'paper whoosh', agent=FakeAgent(['[{"prompt":"dry paper sweep","duration_s":1.5,"prompt_influence":0.7}]']))
    workflow.run(lab, yes=True)
    return read_catalogue(lab)[0]


@pytest.mark.parametrize('command', ['plan', 'batch'])
def test_feedback_cli_prompt_and_lineage(lab, parent, command, monkeypatch):
    append_events(lab.ratings_path, [{'batch': parent['batch'], 'n': parent['n'],
        'ts': 1, 'stars': 5, 'note': 'keep the dry texture', 'project': 'general', 'spec': parent['spec']}])
    agent = FakeAgent(['[{"prompt":"crisp paper sweep"},{"prompt":"short paper sweep"},{"prompt":"close paper sweep"}]'])
    monkeypatch.setattr(workflow, 'load_agent', lambda *a: agent)
    args = ['--lab', str(lab.root), command, '--from', parent['id'], '--feedback', 'more volume', '-n', '3']
    if command == 'batch':
        args.append('--yes')
    assert main(args) == 0
    data = json.loads(lab.plans()[-1].read_text())
    assert data['brief'] == parent['brief']
    assert len(data['specs']) == 3 and all(s['from'] == parent['id'] for s in data['specs'])
    prompt = agent.prompts[0]
    section = prompt.split('## Build on this sound\n')[1]
    assert parent['spec']['prompt'] in section and '"stars": 5' in section
    assert 'keep the dry texture' in section
    assert '"duration_s": 1.5' in section and '"prompt_influence": 0.7' in section
    assert 'Feedback: more volume. Keep what works, change only what the feedback asks.' in section
    assert 'ElevenLabs has no volume control. Requests for louder/more volume mean closer mic, more punch, fuller body, more presence.' in prompt


def test_feedback_explicit_brief_and_unrated_project_note(lab, parent):
    create_project(lab, 'Other')
    other = select_project(lab, 'other')
    write_json(lab.internal / 'notes.json', {parent['id']: 'general note', f"other:{parent['id']}": 'keep the attack'})
    agent = FakeAgent(['[{"prompt":"short paper flutter"}]'])
    data = workflow.plan(other, 'shorter', from_id=parent['id'], feedback='like 05 but shorter', agent=agent)
    assert data['brief'] == 'shorter'
    assert '"stars": null' in agent.prompts[0] and 'keep the attack' in agent.prompts[0]
    assert 'general note' not in agent.prompts[0]


@pytest.mark.parametrize('args,message', [
    (['plan', '--from', 'missing', '--feedback', 'crisper'], 'Unknown sound identifier'),
    (['batch', '--from', 'missing'], 'Unknown sound identifier'),
    (['plan'], 'Provide a brief or use --from'),
    (['plan', 'click', '--feedback', 'crisper'], '--feedback requires --from'),
])
def test_feedback_errors_before_agent(lab, args, message, monkeypatch, capsys):
    monkeypatch.setattr(workflow, 'load_agent', lambda *a: pytest.fail('Must reject before agent'))
    assert main(['--lab', str(lab.root), *args]) == 1
    assert message in capsys.readouterr().err
