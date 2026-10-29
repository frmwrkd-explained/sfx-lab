"""The Cue sheet view's backend, without adding a visible UI."""

import json
import socket
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

from sfx_lab import score
from sfx_lab.lab import Lab
from sfx_lab.ratings import read_log
from sfx_lab.studio import StudioServer, create_server
from tests.fakes import FakeAgent, FakeGenerator
from tests.test_run import PROFILE
from tests.test_score import video, completed
from tests.test_studio import request, post, wait_job, CONNECT, CREATE_CONNECTION


@pytest.fixture
def scored_server(lab: Lab, video: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[StudioServer, dict[str, Any]]]:
    data = completed(lab, video)
    def local_connect(sock: socket.socket, address: tuple[str, int]) -> Any:
        assert address[0] == '127.0.0.1'
        return CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    monkeypatch.setattr(socket, 'create_connection', CREATE_CONNECTION)
    server = create_server(lab, 0, FakeAgent([PROFILE]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, data
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_cue_list_play_rate_learn_swap_render(scored_server: tuple[StudioServer, dict[str, Any]], lab: Lab) -> None:
    server, data = scored_server
    base = f'/api/scores/{data["id"]}'
    status, _, result = request(server, '/api/scores')
    assert status == 200 and result['scores'][0]['id'] == data['id']
    status, _, result = request(server, base + '/cues')
    assert status == 200 and len(result['cues']) == 4
    cue = result['cues'][1]
    assert cue['take']['id'] == cue['take_id']
    cue_url = base + f'/cues/{cue["id"]}'
    status, _, take = request(server, cue_url + '/take')
    assert status == 200 and take['audio_url'].startswith('/audio/')
    status, headers, content = request(server, take['audio_url'], headers={'Range': 'bytes=0-31'})
    assert status == 206 and len(content) == 32
    assert content[:4] == b'RIFF'
    before = len(FakeGenerator.calls)
    rated = post(server, cue_url + '/rate', {'stars': 5, 'note': 'Crisp and well timed'})
    assert rated['stars'] == 5 and not rated['settled']
    assert not FakeGenerator.settlements
    assert read_log(lab.ratings_path)[-1]['source'] == 'studio'
    learned = wait_job(server, post(server, '/api/learn', {}))
    assert learned['state'] == 'done', learned
    assert len(FakeGenerator.settlements) == 1 and FakeGenerator.settlements[0][1]
    replacement = result['cues'][2]['take_id']
    swapped = post(server, cue_url + '/swap', {'take_id': replacement})
    assert swapped['render_dirty'] and swapped['cue']['take_id'] == replacement
    # Historical take remains attached and can be rated independently.
    post(server, cue_url + '/rate', {'take_id': take['id'], 'stars': 4})
    assert len(FakeGenerator.settlements) == 1
    rendered = wait_job(server, post(server, base + '/render', {}))
    assert rendered['state'] == 'done', rendered
    assert not rendered['result']['render_dirty']
    assert len(FakeGenerator.calls) == before
    assert score.load_score(lab, data['id'])['cues'][1]['take_id'] == replacement


def test_score_endpoints_auth_validation_and_projects(scored_server: tuple[StudioServer, dict[str, Any]], lab: Lab) -> None:
    server, data = scored_server
    base = f'/api/scores/{data["id"]}'
    cue_url = base + '/cues/cue-001'
    assert request(server, '/api/scores', token=False)[0] == 403
    assert request(server, base, headers={'Host': 'evil.test'})[0] == 403
    assert request(server, '/api/scores/score-../../secret')[0] == 404
    assert request(server, base + '/cues/cue-999/take')[0] == 404
    assert request(server, cue_url + '/take?take_id=other')[0] == 404
    assert request(server, cue_url + '/rate', {}, 'POST')[0] == 400
    assert request(server, cue_url + '/rate', {'stars': 6}, 'POST')[0] == 400
    assert request(server, cue_url + '/rate', {'stars': True}, 'POST')[0] == 400
    assert request(server, cue_url + '/swap', {'take_id': '../outside'}, 'POST')[0] == 400
    project = post(server, '/api/projects', {'name': 'Other Show'})
    post(server, '/api/use', {'slug': project['slug']})
    assert request(server, base)[0] == 404
    status, _, empty = request(server, '/api/scores')
    assert status == 200 and empty['scores'] == []
    assert not read_log(lab.ratings_path)


def test_missing_takes_cannot_render(scored_server: tuple[StudioServer, dict[str, Any]], lab: Lab) -> None:
    server, data = scored_server
    data['cues'][0]['take_id'] = None
    score.save_score(lab, data)
    status, _, result = request(server, f'/api/scores/{data["id"]}/render', {}, 'POST')
    assert status == 409 and 'missing' in result['error']


def test_score_media_streams_the_edit_for_the_cue_sheet(scored_server: tuple[StudioServer, dict[str, Any]], lab: Lab) -> None:
    server, data = scored_server
    status, _, result = request(server, f'/api/scores/{data["id"]}')
    assert status == 200 and set(result['media']) == {'source', 'video', 'stem'}
    status, headers, content = request(server, result['media']['video'], headers={'Range': 'bytes=0-15'})
    assert status == 206 and len(content) == 16 and headers['Content-Type'] == 'video/mp4'
    status, _, content = request(server, result['media']['stem'], headers={'Range': 'bytes=0-3'})
    assert status == 206 and content == b'RIFF'
    assert request(server, result['media']['source'])[0] == 200
    assert request(server, f'/score-media/{data["id"]}/fcpxml')[0] == 404
    assert request(server, '/score-media/score-../../secret/video')[0] == 404
    assert request(server, result['media']['video'], headers={'Host': 'evil.test'})[0] == 403
    project = post(server, '/api/projects', {'name': 'Other Show'})
    post(server, '/api/use', {'slug': project['slug']})
    assert request(server, result['media']['video'])[0] == 404


def test_more_route_dispatch(lab: Lab, video: Path) -> None:
    from sfx_lab.studio import APIError, Studio
    data = completed(lab, video)
    studio = Studio(lab)
    cue = data['cues'][1]
    cue['take_ids'] = []
    cue['take_id'] = None
    score.save_score(lab, data)
    url = f'/api/scores/{data["id"]}/cues/{cue["id"]}/more'
    result = studio.dispatch('POST', url, {})
    assert result['added'] >= 1 and result['cue']['take_id'] is None
    assert result['render_dirty'] == data['render_dirty']
    assert studio.dispatch('POST', url, {})['added'] == 0
    for path in (url.replace(cue['id'], 'cue-999'), url.replace(data['id'], 'score-missing')):
        with pytest.raises(APIError) as error:
            studio.dispatch('POST', path, {})
        assert error.value.status == 404
    with pytest.raises(APIError) as error:
        studio.dispatch('POST', url.replace('/more', '/swap'), {})
    assert error.value.status == 400
