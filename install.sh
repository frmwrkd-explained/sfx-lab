#!/bin/sh
# Install the CLI and Claude Code skill from this checkout; never remove a lab.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skill_target="$HOME/.claude/skills/sfx-lab"
lab=''
uninstall=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --lab)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then echo 'Usage: ./install.sh [--lab DIR] [--uninstall]' >&2; exit 1; fi
      lab=$2; shift 2 ;;
    --uninstall) uninstall=true; shift ;;
    *) echo 'Usage: ./install.sh [--lab DIR] [--uninstall]' >&2; exit 1 ;;
  esac
done
uv=$(command -v uv || true)
if [ -z "$uv" ] && [ -x "$HOME/.local/bin/uv" ]; then uv="$HOME/.local/bin/uv"; fi
if [ "$uninstall" = true ]; then
  if [ -n "$uv" ]; then "$uv" tool uninstall sfx-lab || true; fi
  if [ -L "$skill_target" ]; then rm -- "$skill_target"; fi
  echo 'Removed the tool and skill link. Your lab is untouched.'
  exit 0
fi
if [ -z "$uv" ]; then
  curl -LsSf https://astral.sh/uv/0.11.7/install.sh | sh
  uv="$HOME/.local/bin/uv"
fi
"$uv" tool install --force --python 3.12 "$repo"
"$uv" tool update-shell || true
bin_dir=$("$uv" tool dir --bin) || bin_dir=''
sfx_lab="${bin_dir}/sfx-lab"
if [ -z "$bin_dir" ] || [ ! -x "$sfx_lab" ]; then sfx_lab="$HOME/.local/bin/sfx-lab"; fi
if [ -z "$lab" ]; then
  # Use uv's managed Python, never require Python on the user's PATH.
  python_bin=$("$uv" python find 3.12)
  lab=$("$python_bin" - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'sfx-lab/config.json'
try:
    lab = Path(json.loads(path.read_text()).get('default_lab', '')).expanduser()
    if (lab / '.sfx-lab/config.json').is_file():
        print(lab)
except (OSError, ValueError, TypeError):
    pass
PY
)
fi
if [ -z "$lab" ]; then
  if [ -t 0 ]; then
    printf 'Sound library folder [%s/SFX Lab]: ' "$HOME"
    IFS= read -r lab || lab=''
  fi
  lab=${lab:-"$HOME/SFX Lab"}
fi
case "$lab" in '~/'*) lab="$HOME/${lab#\~/}";; esac
"$sfx_lab" init "$lab"
mkdir -p -- "$(dirname -- "$skill_target")"
if [ -L "$skill_target" ]; then
  rm -- "$skill_target"
elif [ -e "$skill_target" ]; then
  backup="$skill_target.bak-$(date +%Y%m%d-%H%M%S)"
  while [ -e "$backup" ]; do backup="$backup-1"; done
  mv -- "$skill_target" "$backup"
fi
ln -s -- "$repo/skill" "$skill_target"
echo 'Done. In Claude Code, say: Open SFX Lab. Then click the gear icon, paste your ElevenLabs API key, and press Save.'
