"""Offline score contract, real tiny ffmpeg media, and measurable mix checks."""

import array
import json
import math
import shutil
import subprocess
import sys
import wave
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from sfx_lab import score, score_media, score_export
from sfx_lab.catalogue import append_sound, read_catalogue
from sfx_lab.cli import main, parser
from sfx_lab.lab import Lab, read_text, write_json
from sfx_lab.projects import create_project, get_project, project_path, select_project
from sfx_lab.ratings import append_events
from sfx_lab.run import learn
from sfx_lab.spec import Spec
from tests.fakes import FakeAgent, FakeGenerator, ScoreGenerator


@pytest.fixture
def video(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> Path:
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Optional ffmpeg/ffprobe not installed')
    # Four testsrc shots, three abrupt polarity changes. Nothing downloaded.
    path = lab.safe_path('tiny edit.mp4')
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc=duration=4:size=96x64:rate=25', '-f', 'lavfi', '-i',
                    'sine=frequency=250:duration=4', '-vf',
                    "negate=enable='gte(t,1)*lt(t,2)+gte(t,3)'", '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-y', str(path)], check=True, capture_output=True)
    original_which = shutil.which
    monkeypatch.setattr(score_media.shutil, 'which', lambda name: None if name == 'whisper' else original_which(name))
    lab.config['generator'] = 'tests.fakes:ScoreGenerator'
    return path


def planned(lab: Lab, video: Path) -> dict[str, Any]:
    return score.plan_score(lab, video, no_agent=True, emit=lambda _: None)


def completed(lab: Lab, video: Path) -> dict[str, Any]:
    data = planned(lab, video)
    return score.execute_score(lab, data['id'], yes=True, emit=lambda _: None)


def pcm(path: Path) -> tuple[int, int, array.array]:
    with wave.open(str(path), 'rb') as stream:
        data = array.array('h')
        data.frombytes(stream.readframes(stream.getnframes()))
        if sys.byteorder != 'little':
            data.byteswap()
        return stream.getframerate(), stream.getnchannels(), data


def rms(samples: array.array) -> float:
    return math.sqrt(sum(v * v for v in samples) / len(samples))


def test_analysis_cuts_and_offline_whisper(lab: Lab, video: Path) -> None:
    info = score_media.analyse(video, lab.root)
    assert info['duration'] == pytest.approx(4)
    assert info['fps'] == '25'
    assert info['width'] == 96 and info['height'] == 64
    assert len(info['audio_streams']) == 1
    assert info['cuts'] == pytest.approx([1, 2, 3], abs=1 / 25)
    assert info['voice_regions'] == []
    assert 'Whisper' in info['warnings'][0]


def test_motion_spikes_use_short_window(monkeypatch: pytest.MonkeyPatch) -> None:
    values = [1, 1, 1, 1, 40, 1, 1, 1, 1, 1, 1, 1, 40]
    result = ''.join(f'frame:{n} pts:{n} pts_time:{n / 25}\nlavfi.signalstats.YAVG={v}\n' for n, v in enumerate(values))
    monkeypatch.setattr(score_media, 'command', lambda *a, **k: subprocess.CompletedProcess([], 0, result, ''))
    assert score_media.motion_hits(Path('fake.mp4'), 25, []) == [.16]
    assert score_media.motion_hits(Path('fake.mp4'), 25, [.16]) == [.48]


def test_whisper_cached_model_only(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(score_media.shutil, 'which', lambda name: '/fake/whisper')
    monkeypatch.setenv('SFX_LAB_WHISPER_MODEL', str(lab.root / 'missing.pt'))
    monkeypatch.setattr(score_media, 'command', lambda *a, **k: pytest.fail('No implicit model downloads'))
    assert 'cached' in score_media.transcript(lab.root / 'source.mp4', lab.root, True)[1]
    model = lab.root / 'tiny.pt'
    model.touch()
    monkeypatch.setenv('SFX_LAB_WHISPER_MODEL', str(model))

    def command(name: str, args: list[str], **kwargs: Any) -> Any:
        if name == 'whisper':
            assert args[args.index('--model') + 1] == str(model)
            folder = Path(args[args.index('--output_dir') + 1])
            write_json(folder / 'speech.json', {'segments': [{'start': .4, 'end': 1.2, 'text': 'hello'},
                                                           {'start': 2, 'end': 3, 'no_speech_prob': .9}]})
    monkeypatch.setattr(score_media, 'command', command)
    segments, warning = score_media.transcript(lab.root / 'source.mp4', lab.root, True)
    assert segments == [{'start': .4, 'end': 1.2, 'text': 'hello'}] and not warning


@pytest.mark.parametrize('fps', ['25', '24000/1001', '30000/1001', '60000/1001'])
def test_cue_fitting_coalesces_tight_events(fps: str, lab: Lab) -> None:
    info = {'duration': 66, 'fps': fps, 'cuts': [.12, .6, .7, 1.3, 65.9], 'motion_hits': [.62], 'transcript': []}
    cues = score._cue_specs(lab, info, True, None)
    for n, cue in enumerate(cues):
        assert .5 <= cue['end'] - cue['start'] <= 30
        assert cue['end'] <= info['duration']
        assert float(Fraction(str(cue['start'])) * Fraction(fps)) == pytest.approx(round(cue['start'] * float(Fraction(fps))), abs=1e-6)
        if n + 1 < len(cues) and cue['event_type'] != 'ambience-bed':
            # Hits never overlap the next cue; beds deliberately run under the hits.
            assert cue['end'] <= cues[n + 1]['start'] + 1e-8


def test_agent_cue_contract_and_context(lab: Lab, video: Path) -> None:
    info = score_media.analyse(video, lab.root)
    events = score.fit_events(info)
    response = [{'id': e['id'], 'event_type': 'ui-pop', 'prompt': 'One close dry plastic pop in a small calm room, quick release.', 'duration_s': 50}
                for e in events]
    agent = FakeAgent([json.dumps(response)])
    data = score.plan_score(lab, video, agent=agent, emit=lambda _: None)
    assert 'User notes' in agent.prompts[0] and '# SFX' in agent.prompts[0]
    assert 'How to write each ElevenLabs prompt' in agent.prompts[0]
    assert all(c['event_type'] == 'ui-pop' for c in data['cues'])
    assert all(c['end'] - c['start'] <= 1.001 for c in data['cues'])
    assert not FakeGenerator.calls
    assert score.load_score(lab, data['id']) == data
    assert '| Cue |' in read_text(lab.safe_path(data['outputs']['cues_md']))
    with pytest.raises(ValueError, match='every event'):
        score.plan_score(lab, video, agent=FakeAgent(['[]']), emit=lambda _: None)


def test_reuse_split_and_cost_gate(lab: Lab, video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    batch = {'batch': '2026-10-01-01', 'project': lab.project, 'brief': 'whoosh', 'kind': 'cut',
             'generator': 'tests.fakes:ScoreGenerator', 'specs': [Spec('dry air whoosh', .7).to_dict()]}
    result = ScoreGenerator().generate(batch['specs'][0], lab.audio_dir / 'reuse')
    sound = append_sound(lab, batch, 1, result)
    append_events(lab.ratings_path, [{'project': lab.project, 'batch': batch['batch'], 'n': 1, 'ts': 1,
                                    'stars': 5, 'rating': '5', 'spec': batch['specs'][0], 'generator': batch['generator'], 'ref': '', 'settled': False}])
    FakeGenerator.calls.clear()
    data = planned(lab, video)
    assert data['estimate']['reuse'] == 3
    assert data['estimate']['need'] == 1
    assert [c['take_id'] for c in data['cues'][1:]] == [sound['id']] * 3
    project = get_project(lab)
    write_json(project_path(lab, lab.project), project | {'defaults': {'max_usd': 0}})
    with pytest.raises(ValueError, match='exceeds max-usd'):
        score.execute_score(lab, data['id'], yes=True, emit=lambda _: None)
    assert not FakeGenerator.calls
    write_json(project_path(lab, lab.project), project)
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    with pytest.raises(ValueError, match='--yes'):
        score.execute_score(lab, data['id'], emit=lambda _: None)
    assert not FakeGenerator.calls
    done = score.execute_score(lab, data['id'], yes=True, emit=lambda _: None)
    assert len(FakeGenerator.calls) == 1 and not done['render_dirty']
    score.execute_score(lab, data['id'], yes=True, emit=lambda _: None)
    assert len(FakeGenerator.calls) == 1


def test_capped_project_and_nan_cannot_spend(lab: Lab) -> None:
    project = get_project(lab)
    write_json(project_path(lab, lab.project), project | {'defaults': {'max_usd': .001}})
    for cost, cap in [(.01, None), (.01, 0), (math.nan, .5), (.01, math.nan), (.01, -1)]:
        with pytest.raises(ValueError):
            score.cost_gate(lab, cost, 1, True, cap)


def test_generation_failure_resume_and_crash_recovery(lab: Lab, video: Path) -> None:
    data = planned(lab, video)
    original = data['cues'][1]['prompt']
    data['cues'][1]['prompt'] = 'fail'
    score.save_score(lab, data)
    with pytest.raises(RuntimeError, match='Successful takes are saved'):
        score.execute_score(lab, data['id'], yes=True, emit=lambda _: None)
    failed = score.load_score(lab, data['id'])
    assert sum(bool(c['take_id']) for c in failed['cues']) == 3
    failed['cues'][1]['prompt'] = original
    # Simulate an exit between catalogue append and cue save for another take.
    failed['cues'][2]['take_id'] = None
    score.save_score(lab, failed)
    before = len(FakeGenerator.calls)
    done = score.execute_score(lab, data['id'], yes=True, emit=lambda _: None)
    assert len(FakeGenerator.calls) == before + 1
    assert all(c['take_id'] for c in done['cues'])
    assert len(read_catalogue(lab)) == 5


def test_stem_placement_ducking_peak_and_loop(lab: Lab, video: Path) -> None:
    path = lab.audio_dir / 'constant.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        values = array.array('h', [20000] * 24000)
        if sys.byteorder != 'little':
            values.byteswap()
        stream.writeframes(values.tobytes())
    cues = [{'id': 'cue-001', 'start': .4, 'end': 2.8, 'take_id': 'one', 'event_type': 'ambience-bed', 'loop': True}]
    stem = lab.root / 'stem.wav'
    score_media.mix_stem(cues, {'one': path}, 3, [[1.2, 2]], stem, lab.root)
    rate, channels, samples = pcm(stem)
    assert rate == 48000 and channels == 2 and len(samples) == 3 * rate * 2
    nonzero = [i // 2 for i, v in enumerate(samples) if v]
    assert min(nonzero) / rate == pytest.approx(.4, abs=1 / 25)
    assert max(nonzero) / rate == pytest.approx(2.8, abs=1 / 25)
    quiet = rms(samples[int(1.4 * rate) * 2:int(1.8 * rate) * 2])
    normal = rms(samples[int(.7 * rate) * 2:int(1 * rate) * 2])
    assert 20 * math.log10(quiet / normal) == pytest.approx(-9, abs=.01)
    assert samples[round(1.12 * rate) * 2] == pytest.approx(normal, abs=1)
    assert samples[round(1.16 * rate) * 2] / normal == pytest.approx((1 + score_media.DUCK) / 2, abs=.001)
    assert samples[round(2.04 * rate) * 2] / normal == pytest.approx((1 + score_media.DUCK) / 2, abs=.001)
    score_media.mix_stem(cues * 3, {'one': path}, 3, [], stem, lab.root)
    _, _, samples = pcm(stem)
    assert max(samples) / 32768 <= score_media.PEAK
    # A voice line ducks the effects but remains at its own level.
    line = dict(cues[0], event_type='voice-line', start=1.2, end=2)
    score_media.mix_stem(cues + [line], {'one': path}, 3, [], stem, lab.root)
    _, _, samples = pcm(stem)
    assert rms(samples[int(1.3 * rate) * 2:int(1.6 * rate) * 2]) > rms(samples[int(.7 * rate) * 2:int(1 * rate) * 2])


def test_render_exports_and_rerender_after_swap(lab: Lab, video: Path) -> None:
    done = completed(lab, video)
    for key in ('video', 'stem', 'fcpxml', 'edl', 'cues_json', 'cues_md'):
        assert lab.safe_path(done['outputs'][key]).stat().st_size > 0
    info = score_media.probe(lab.safe_path(done['outputs']['video']))
    assert info['duration'] == pytest.approx(4, abs=1 / 25)
    assert len(info['audio_streams']) == 1
    root = ET.parse(lab.safe_path(done['outputs']['fcpxml'])).getroot()
    assert root.tag == 'fcpxml' and root.attrib['version'] == '1.8'
    resources = {r.attrib['id'] for r in root.find('resources')}
    assert all(e.attrib['ref'] in resources for e in root.iter('asset-clip'))
    assert [m.attrib['value'] for m in root.iter('marker')] == [c['id'] for c in done['cues']]
    connected = root.find('.//spine/asset-clip/asset-clip')
    assert connected.attrib['lane'] == '-1' and connected.attrib['ref'] == 'r3'
    assert [c.attrib['name'] for c in root.iter('asset-clip') if c.attrib.get('enabled') == '0'] == [c['id'] for c in done['cues']]
    text = read_text(lab.safe_path(done['outputs']['edl']))
    assert text.startswith('TITLE:') and 'FCM: NON-DROP FRAME' in text
    for cue in done['cues']:
        assert f'|M:{cue["id"]} |D:' in text
        tc = score_export.timecode(score_export.frames(cue['start'], done['analysis']['fps']), done['analysis']['fps'])
        assert tc in text
    old = done['cues'][1]['take_id']
    new = done['cues'][2]['take_id']
    swapped = score.swap_take(lab, done['id'], 'cue-002', new)
    assert swapped['render_dirty'] and swapped['cues'][1]['take_id'] == new
    assert old in swapped['cues'][1]['take_ids']
    before = len(FakeGenerator.calls)
    assert not score.render_score(lab, done['id'], emit=lambda _: None)['render_dirty']
    assert len(FakeGenerator.calls) == before


def test_video_without_original_audio(lab: Lab, video: Path) -> None:
    silent = lab.root / 'silent.mp4'
    score_media.command('ffmpeg', ['-nostdin', '-v', 'error', '-i', str(video), '-map', '0:v:0', '-c', 'copy', str(silent)])
    done = completed(lab, silent)
    assert not done['analysis']['audio_streams']
    assert len(score_media.probe(lab.safe_path(done['outputs']['video']))['audio_streams']) == 1


def test_line_timecode_mix_and_no_cloning(lab: Lab, video: Path) -> None:
    done = completed(lab, video)
    calls = len(FakeGenerator.calls)
    out = []
    result = score.add_line(lab, 'Hello there', 'existing-voice', '00:00:01:10', done['id'], yes=True,
                            generator=ScoreGenerator(), emit=out.append)
    assert len(FakeGenerator.calls) == calls + 1
    line = next(c for c in result['cues'] if c['event_type'] == 'voice-line')
    assert line['start'] == 1.4 and line['voice_id'] == 'existing-voice'
    assert not result['render_dirty']
    assert any('trimmed' in message for message in out) is False
    with pytest.raises(ValueError, match='inside'):
        score.add_line(lab, 'late', 'existing', '5', yes=True, generator=ScoreGenerator())
    with pytest.raises(ValueError, match='existing'):
        score.add_line(lab, 'no', '../voice', '0', yes=True, generator=ScoreGenerator())
    with pytest.raises(ValueError, match='non-drop-frame'):
        score_export.parse_timecode('00:00:01:25', '25')
    assert score_export.parse_timecode('00:01:00:00', '30000/1001') == pytest.approx(60.06)


def test_cli_saved_plan_and_project_scope(lab: Lab, video: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Persist the offline generator for CLI discovery.
    config_path = lab.internal / 'config.json'
    config = json.loads(read_text(config_path))
    write_json(config_path, config | {'generator': 'tests.fakes:ScoreGenerator'})
    project = create_project(lab, 'My Film')
    assert main(['--lab', str(lab.root), 'score', str(video), '--no-agent', '--plan-only', '--project', project['slug']]) == 0
    other = select_project(lab, project['slug'])
    data = score.load_score(other)
    assert not FakeGenerator.calls
    assert '| Cue |' in capsys.readouterr().out
    assert main(['--lab', str(lab.root), 'score', '--resume', data['id'], '--yes', '--project', project['slug']]) == 0
    assert score.load_score(other)['state'] == 'rendered'
    with pytest.raises(ValueError, match='another project'):
        score.load_score(lab, data['id'])
    for invalid in ('../etc', '/abs', 'score-../../other'):
        with pytest.raises(ValueError):
            score.load_score(other, invalid)
    assert parser().parse_args(['line', 'hello', '--voice', 'existing', '--at', '0', '--project', 'my-film']).score_project == 'my-film'


def test_missing_tools_are_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(score_media.shutil, 'which', lambda _: None)
    with pytest.raises(RuntimeError, match='Install ffmpeg'):
        score_media.tool('ffmpeg')


def test_graphic_hit_from_real_frame_differences(lab: Lab, video: Path) -> None:
    graphics = lab.root / 'graphic.mp4'
    score_media.command('ffmpeg', ['-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:size=96x64:rate=25:duration=2',
                                  '-vf', "drawbox=x=0:y=0:w=48:h=32:color=gray:t=fill:enable='gte(t,0.8)*lt(t,0.84)'",
                                  '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(graphics)])
    cuts = score_media.scene_cuts(graphics, 2)
    hits = score_media.motion_hits(graphics, 25, cuts)
    assert not cuts
    assert hits == pytest.approx([.8], abs=1 / 25)


def test_credit_and_dollar_estimates_are_separate(lab: Lab) -> None:
    data = {'generator': 'elevenlabs', 'cues': [{'start': 0, 'end': 1.5, 'prompt': 'A dry hit', 'prompt_influence': .3,
                                               'event_type': 'impact', 'loop': False, 'id': 'cue-001', 'take_id': None}]}
    value = score.estimate(lab, data)
    assert value['credits'] == 60 and value['usd'] == pytest.approx(.003)
    data['cues'][0]['take_id'] = 'reused'
    value = score.estimate(lab, data)
    assert value['credits'] == 0 and value['usd'] == 0


def test_original_audio_offset_is_preserved(lab: Lab, video: Path) -> None:
    delayed = lab.root / 'delayed.mp4'
    score_media.command('ffmpeg', ['-nostdin', '-v', 'error', '-i', str(video), '-itsoffset', '1', '-f', 'lavfi', '-i',
                                  'sine=frequency=500:duration=2', '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', str(delayed)])
    info = score_media.probe(delayed)
    assert next(iter(info['audio_offsets'].values())) == pytest.approx(1, abs=.05)
    stem = lab.root / 'silent-stem.wav'
    with wave.open(str(stem), 'wb') as output:
        output.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\0' * 4 * 48000 * 4)
    review = lab.root / 'delayed.scored.mp4'
    score_media.render_video(delayed, stem, info, review)
    decoded = lab.root / 'decoded.wav'
    score_media.command('ffmpeg', ['-nostdin', '-v', 'error', '-i', str(review), '-vn', '-ar', '48000', '-ac', '2', str(decoded)])
    rate, _, samples = pcm(decoded)
    assert rms(samples[round(.2 * rate) * 2:round(.6 * rate) * 2]) < 2
    assert rms(samples[round(1.2 * rate) * 2:round(1.6 * rate) * 2]) > 500


def test_cue_spec_never_sends_a_frame_fitted_half_second_below_the_api_floor() -> None:
    # 15 frames at 30 fps is 0.5 s, but start/end arithmetic can land a hair under it.
    cue = {"prompt": "p", "start": 0.5333333333333333, "end": 1.0333333333333332, "prompt_influence": .3,
           "event_type": "cut", "loop": False, "id": "cue-002"}
    assert score.cue_spec(cue)["duration_s"] == 0.5
    cue["end"] = cue["start"] + 1.2345678
    assert score.cue_spec(cue)["duration_s"] == 1.235


def test_ambience_bed_runs_under_the_hits_until_the_end(lab: Lab) -> None:
    info = {'duration': 12, 'fps': '30', 'cuts': [.6, 1.2, 2.4, 6.0], 'motion_hits': [], 'transcript': []}
    cues = score._cue_specs(lab, info, True, None)
    beds = [c for c in cues if c['event_type'] == 'ambience-bed']
    assert beds and beds[0]['start'] == 0
    assert beds[0]['end'] == pytest.approx(12, abs=1 / 30)


def test_bed_fills_its_span_even_when_the_agent_asks_for_half_a_second(lab: Lab) -> None:
    info = {'duration': 12, 'fps': '30', 'cuts': [.6, 1.2, 2.4, 6.0], 'motion_hits': [], 'transcript': []}
    events = score.fit_events(info)
    reply = [{'id': e['id'], 'event_type': e['event_type'], 'prompt': 'Room tone bed under a voiceover, quiet even hum, isolated, no music.'
              if e['event_type'] == 'ambience-bed' else 'Transition tick for a hard cut, one small plastic click, isolated, no music.',
              'duration_s': .5} for e in events]
    cues = score._cue_specs(lab, info, False, FakeAgent([json.dumps(reply)]))
    bed = [c for c in cues if c['event_type'] == 'ambience-bed'][0]
    assert bed['end'] == pytest.approx(12, abs=1 / 30)


def test_more_takes_pools_ranked_valid_matches(lab: Lab, video: Path) -> None:
    data = planned(lab, video)
    cue = data['cues'][1]
    cue.update(prompt='dry air whoosh', event_type='cut', take_id='selected', take_ids=['selected'], status='generated')
    score.save_score(lab, data)
    ids = []
    for n, (prompt, stars) in enumerate([('dry air whoosh', 5), ('dry air whoosh', 3),
                                         ('dry air whoosh', None), ('dry air whoosh', 2),
                                         ('dry air whoosh', 1), ('dry air pop', 5)], 1):
        batch = {'batch': f'2026-10-01-{n:02d}', 'project': lab.project, 'brief': 'dry air', 'kind': 'cut',
                 'generator': 'tests.fakes:ScoreGenerator', 'specs': [Spec(prompt, .7).to_dict()]}
        sound = append_sound(lab, batch, 1, ScoreGenerator().generate(batch['specs'][0], lab.audio_dir / f'pool-{n}'))
        ids.append(sound['id'])
        if stars is not None:
            append_events(lab.ratings_path, [{'project': lab.project, 'batch': batch['batch'], 'n': 1, 'ts': 1,
                'stars': stars, 'rating': str(stars), 'spec': batch['specs'][0], 'generator': batch['generator'], 'ref': '', 'settled': False}])
    reuse = [cue.copy()]
    score.reuse_cues(lab, reuse)
    assert reuse[0]['take_ids'] == [ids[0]] and reuse[0]['status'] == 'reused'
    result = score.more_takes(lab, data['id'], cue['id'])
    assert result['added'] == 4
    updated = score.cue_by_id(result['score'], cue['id'])
    assert updated['take_ids'] == ['selected', ids[0], ids[2], ids[1], ids[3]]
    assert updated['take_id'] == 'selected' and updated['status'] == 'generated'
    assert score.more_takes(lab, data['id'], cue['id'])['added'] == 0
    updated['event_type'] = 'voice-line'
    score.save_score(lab, result['score'])
    assert score.more_takes(lab, data['id'], cue['id']) == {'added': 0, 'score': result['score']}
