"""Run the Mac installer offline, without Python on PATH or a terminal."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DONE = 'Done. In Claude Code, say: Open SFX Lab. Then click the gear icon, paste your ElevenLabs API key, and press Save.'


@pytest.fixture
def installer(tmp_path):
    home, bins = tmp_path / 'fresh home', tmp_path / 'bin'
    home.mkdir()
    bins.mkdir()
    for name in ('sh', 'dirname', 'mkdir', 'rm', 'mv', 'ln', 'date'):
        (bins / name).symlink_to(shutil.which(name))
    tool_dir = home / 'tool bin'
    tool_dir.mkdir()
    tool = tool_dir / 'sfx-lab'
    tool.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -m sfx_lab.cli "$@"\n')
    tool.chmod(0o755)
    uv = home / 'fake-uv'
    uv.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/uv-calls"
case "$*" in
  "tool update-shell") exit 1;;
  "tool dir --bin") printf '%s\\n' "$HOME/tool bin";;
  "python find 3.12") printf '%s\\n' ''' + shlex.quote(sys.executable) + ''';;
esac
''')
    uv.chmod(0o755)
    curl = bins / 'curl'
    # The fake pinned installer installs fake uv, with no network access.
    curl.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/curl-calls"
printf '%s\\n' 'mkdir -p "$HOME/.local/bin"' '/bin/cp "$HOME/fake-uv" "$HOME/.local/bin/uv"' '/bin/chmod +x "$HOME/.local/bin/uv"'
''')
    curl.chmod(0o755)
    env = os.environ | {'HOME': str(home), 'PATH': str(bins),
        'XDG_CONFIG_HOME': str(home / '.config'), 'PYTHONPATH': str(ROOT / 'src')}
    env.pop('SFX_LAB_DIR', None)
    assert shutil.which('python3', path=str(bins)) is None

    def invoke(*args):
        return subprocess.run([str(bins / 'sh'), str(ROOT / 'install.sh'), *map(str, args)],
                              env=env, input='', capture_output=True, text=True, timeout=15)
    return home, bins, env, invoke


def test_fresh_install_downloads_uv_and_python_without_input(installer):
    home, _, _, invoke = installer
    result = invoke()
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == DONE
    assert 'Sound library folder [' not in result.stdout
    assert (home / 'curl-calls').read_text().strip() == '-LsSf https://astral.sh/uv/0.11.7/install.sh'
    calls = (home / 'uv-calls').read_text().splitlines()
    assert calls[:3] == [f'tool install --force --python 3.12 {ROOT}', 'tool update-shell', 'tool dir --bin']
    lab = home / 'SFX Lab'
    assert (lab / '.sfx-lab/config.json').is_file()
    assert (home / '.claude/skills/sfx-lab').is_symlink()
    # A second run finds installed uv and keeps the default library.
    assert invoke().returncode == 0
    assert len((home / 'curl-calls').read_text().splitlines()) == 1
    assert invoke('--uninstall').returncode == 0
    assert not (home / '.claude/skills/sfx-lab').is_symlink()
    assert (lab / '.sfx-lab/config.json').is_file()
    assert 'tool uninstall sfx-lab' in (home / 'uv-calls').read_text()


def test_explicit_folder_overrides_existing_default_and_backs_up_skill(installer, tmp_path):
    home, bins, _, invoke = installer
    (bins / 'uv').symlink_to(home / 'fake-uv')
    target = home / '.claude/skills/sfx-lab'
    target.mkdir(parents=True)
    (target / 'mine').write_text('preserve')
    chosen = tmp_path / 'chosen library'
    assert invoke('--lab', chosen).returncode == 0
    assert (chosen / '.sfx-lab/config.json').is_file()
    assert invoke().returncode == 0
    assert (next(target.parent.glob('sfx-lab.bak-*')) / 'mine').read_text() == 'preserve'
    assert len(list(target.parent.glob('sfx-lab.bak-*'))) == 1
    assert not (home / 'SFX Lab').exists()
    assert not (home / 'curl-calls').exists()
    assert json.loads((home / '.config/sfx-lab/config.json').read_text())['default_lab'] == str(chosen)
    other = tmp_path / 'other library'
    assert invoke('--lab', other).returncode == 0
    assert (other / '.sfx-lab/config.json').is_file()


def test_bin_fallback_and_uninstall_preserves_user_skill(installer):
    home, bins, _, invoke = installer
    uv = home / 'fake-uv'
    uv.write_text(uv.read_text().replace('"$HOME/tool bin"', '"$HOME/missing"'))
    local = home / '.local/bin'
    local.mkdir(parents=True)
    (local / 'sfx-lab').symlink_to(home / 'tool bin/sfx-lab')
    (bins / 'uv').symlink_to(uv)
    assert invoke().returncode == 0
    target = home / '.claude/skills/sfx-lab'
    target.unlink()
    target.mkdir()
    (target / 'mine').write_text('preserve')
    assert invoke('--uninstall').returncode == 0
    assert (target / 'mine').read_text() == 'preserve'


def test_uninstall_needs_neither_python_nor_uv(installer):
    home, _, _, invoke = installer
    assert invoke('--uninstall').returncode == 0
    assert not (home / 'curl-calls').exists()
