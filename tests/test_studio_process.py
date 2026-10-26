"""Offline lifecycle checks using a detached stand-in for Studio."""

import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from sfx_lab import studio, studio_process as process
from sfx_lab.cli import main
from sfx_lab.lab import Lab, write_json
from sfx_lab.projects import create_project, select_project


class Response(io.BytesIO):
    status = 200


@pytest.fixture
def fake_studio(tmp_path, monkeypatch):
    script = tmp_path / 'fake_studio.py'
    script.write_text('''import json, os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[sys.argv.index('--lab') + 1])
path = root / '.sfx-lab/studio.json'
path.write_text(json.dumps({'pid': os.getpid(), 'port': 12345, 'url': 'http://127.0.0.1:12345/', 'started': time.time()}))
def stop(*args):
    path.unlink(missing_ok=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print(json.dumps({'url': 'http://127.0.0.1:12345/'}), flush=True)
while True:
    time.sleep(1)
''')
    popen = subprocess.Popen
    calls, children = [], []

    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        child = popen([sys.executable, str(script), *argv[3:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(process.subprocess, 'Popen', launch)
    monkeypatch.setattr(process.urllib.request, 'urlopen', lambda *a, **kw: Response(b'<meta name="sfx-lab-token" content="fake">'))
    monkeypatch.setenv('SFX_LAB_NO_BROWSER', '1')
    monkeypatch.setattr(process.webbrowser, 'open', lambda *a: pytest.fail('Browser must stay closed'))
    yield calls
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_open_reuse_stop(lab, fake_studio, capsys):
    base = ['--lab', str(lab.root)]
    assert main(base + ['open']) == 0
    state = json.loads((lab.internal / 'studio.json').read_text())
    assert main(base + ['open']) == 0
    assert len(fake_studio) == 1
    argv, kwargs = fake_studio[0]
    assert argv == [sys.executable, '-m', 'sfx_lab.cli', *base, 'studio', '--no-open', '--port', '0', '--ready-json']
    assert kwargs['stdin'] == subprocess.DEVNULL
    if os.name != 'nt':
        assert kwargs['start_new_session'] is True
        assert os.getsid(state['pid']) == state['pid']
    assert (lab.internal / 'studio.log').is_file()
    assert main(base + ['stop']) == 0
    assert not (lab.internal / 'studio.json').exists()
    assert main(base + ['stop']) == 0
    assert capsys.readouterr().out.splitlines() == [
        'SFX Studio is open: http://127.0.0.1:12345/',
        'SFX Studio is open: http://127.0.0.1:12345/',
        'SFX Studio stopped.', 'SFX Studio is not running.']


@pytest.mark.parametrize('state', [{}, {'pid': -1}, {'pid': 99999999, 'url': 'http://127.0.0.1:1/'}])
def test_stale_state_replaced(lab, fake_studio, state):
    write_json(lab.internal / 'studio.json', state)
    assert process.open_studio(lab) == 'http://127.0.0.1:12345/'
    assert len(fake_studio) == 1
    assert json.loads((lab.internal / 'studio.json').read_text())['pid'] > 0


@pytest.mark.parametrize('status,body', [(200, b'<html>Another app</html>'), (503, b'<meta name="sfx-lab-token">')])
def test_live_pid_requires_healthy_studio(monkeypatch, status, body):
    response = Response(body)
    response.status = status
    monkeypatch.setattr(process.urllib.request, 'urlopen', lambda *a, **kw: response)
    assert not process._healthy({'pid': os.getpid(), 'url': 'http://127.0.0.1:12345/'})


def test_timeout_reports_last_ten_log_lines(lab, monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                             stdout=subprocess.PIPE, start_new_session=True)
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *a, **kw: child)
    monkeypatch.setattr(process, 'READY_TIMEOUT', 0.05)
    monkeypatch.setattr(process, 'STOP_TIMEOUT', 0.05)
    (lab.internal / 'studio.log').write_text('\n'.join(f'line {i:02}' for i in range(15)) + '\n')
    try:
        with pytest.raises(RuntimeError) as error:
            process.open_studio(lab)
        assert str(error.value).splitlines()[1:] == [f'line {i:02}' for i in range(5, 15)]
        assert child.poll() is not None
        assert not (lab.internal / 'studio.json').exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_stop_escalates_and_cleans_up(lab, monkeypatch):
    write_json(lab.internal / 'studio.json', {'pid': 42})
    calls = []
    monkeypatch.setattr(process, 'pid_alive', lambda pid: True)
    monkeypatch.setattr(process.os, 'kill', lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(process, 'STOP_TIMEOUT', 0)
    assert process.stop_studio(lab)
    assert calls == [(42, signal.SIGTERM), (42, signal.SIGKILL if os.name != 'nt' else signal.SIGTERM)]
    assert not (lab.internal / 'studio.json').exists()


def test_open_selects_project_and_running_studio_follows(lab, monkeypatch, capsys):
    create_project(lab, 'Other')
    backend = studio.Studio(select_project(lab))
    monkeypatch.setattr(process, 'open_studio', lambda lab: 'http://127.0.0.1:12345/')
    assert main(['--lab', str(lab.root), 'open', '--project', 'other']) == 0
    assert select_project(lab).project == 'other'
    assert backend.dispatch('GET', '/api/state', {})['project'] == 'other'
    assert main(['--lab', str(lab.root), 'open', '--project', 'missing']) == 1
    assert 'Unknown project' in capsys.readouterr().err


def test_serve_registers_after_bind_and_removes_on_exit(lab, monkeypatch):
    def finish(server):
        state = json.loads((lab.internal / 'studio.json').read_text())
        assert set(state) == {'pid', 'port', 'url', 'started'}
        assert state['pid'] == os.getpid() and state['port'] == server.server_port
        assert state['url'] == f'http://127.0.0.1:{server.server_port}/'
        assert state['started'] > 0
        raise KeyboardInterrupt
    monkeypatch.setattr(studio.StudioServer, 'serve_forever', finish)
    studio.serve(lab, 0, True, True)
    assert not (lab.internal / 'studio.json').exists()


def test_no_browser_env_applies_to_foreground_studio(lab, monkeypatch):
    monkeypatch.setenv('SFX_LAB_NO_BROWSER', '1')
    monkeypatch.setattr(process.webbrowser, 'open', lambda *a: pytest.fail('Browser launched'))
    monkeypatch.setattr(studio.StudioServer, 'serve_forever', lambda server: None)
    studio.serve(lab, 0)
