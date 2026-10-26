# sfx-lab

A small, standard-library-only Python CLI that learns which sound effects you
like. An agent proposes specs, a generator makes audio, you rate sounds in the local Studio, and the agent updates a short `taste.md` for the next batch.
The installer downloads Python for you.

## Install

Tell Claude Code: “Install the SFX Lab skill from <repo>, then open SFX Lab.”

Connect ElevenLabs: elevenlabs.io → profile → API Keys → Create → Copy →
SFX Studio gear → paste → Save. Keep the key out of chat. Blur or delete the
key in recordings. Then ask Claude Code for sounds, such as “Make me 6 whooshes.”

For a manual install:

```sh
git clone https://github.com/frmwrkd-explained/sfx-lab && cd sfx-lab && sh install.sh
```

On Windows, run `./install.ps1` in PowerShell (untested). The installer installs
uv when needed, downloads Python 3.12, and links the Claude skill. It uses your
existing library, asks for a folder in a terminal, or uses `~/SFX Lab` when run
by Claude Code. Pass `--lab DIR` to choose a folder (PowerShell: `-Lab DIR`).
Re-running is safe; an existing skill folder is backed up.
`sh install.sh --uninstall` (PowerShell: `./install.ps1 -Uninstall`) removes the
tool and installed skill, never your library.

Set `ELEVENLABS_API_KEY` in your environment or in the lab's `.env` file (add
`.env` to your own lab's gitignore). Never paste keys into chat. By default the
agent uses your installed Claude Code login and the `haiku` model; no Anthropic
API key is needed. To use the direct API, set `agent` to `anthropic-api` in
`.sfx-lab/config.json` and set `SFX_LAB_ANTHROPIC_KEY` or `ANTHROPIC_API_KEY`.
Its default model is `claude-haiku-5-5`; configure `model` to override it.

```sh
sfx-lab init ./my-sounds
sfx-lab --lab ./my-sounds batch "soft UI click for a tech explainer" -n 6
sfx-lab --lab ./my-sounds open
# Listen, rate 1–5 stars, add notes, then click Update taste.
sfx-lab --lab ./my-sounds learn
sfx-lab --lab ./my-sounds status
```

Generation asks before spending. In a script, check the plan's estimate, then
use `run --yes` (or `batch --yes`). The default cost cap is $0.50 per batch;
change `max_usd` in config or pass `--max-usd`. To inspect before generating:

```sh
sfx-lab plan "dry paper transitions" -n 6 --kind foley
sfx-lab run --yes
```

Without `--lab`, discovery checks `SFX_LAB_DIR`, walks up from the current
folder, then uses the last initialized lab recorded under
`$XDG_CONFIG_HOME/sfx-lab/config.json` (default `~/.config/sfx-lab/config.json`).
Run `sfx-lab --help` for all commands.

Rate 1–5 stars: 1 discards a sound; 2–5 keep it. Words `love` (5), `good` (4),
`ok`/`meh` (3), `bad` (1), emoji ❤️/👍/👎, and `4★`/`★★★★`/`4 stars` also work
in optional Markdown sheets. The old numeric aliases `0` and `-1` are removed;
`1` and `2` now mean one and two stars. Existing word-based logs migrate in
memory when read. Ratings and notes save immediately; settlement happens once,
on the next **Update taste** / `sfx-lab learn`, using the latest stars.

## Studio

```sh
sfx-lab open
sfx-lab open --project my-show
sfx-lab stop
sfx-lab studio
sfx-lab studio --no-open --port 8765
```

`open` starts Studio in the background or reuses it; `stop` closes the server.
Set `SFX_LAB_NO_BROWSER=1` to suppress browser launching.
Studio binds to `127.0.0.1` and opens your browser unless `--no-open` is set.
It runs until Ctrl-C; port `0` selects a free port. It serves local package
assets and lab audio, with a per-run token protecting every API call.

- Library list/grid, search, sorting, batch grouping, prompt tags and filters.
- One player with a waveform, loop playback, remembered level matching (on by default), and 1–5 star ratings and notes.
- Project switcher, New project, This project / All sounds, and Settings for project defaults and write-only keys.
- New batch planning with estimates and the configured cost cap, generation
  progress, and **More like this** with parent/child lineage.
- Map from features decoded in your browser, batch comparison and blind tests.
- Insights, real Sound taste sections, editable **My notes**, and **Update taste**.
- Keyboard: Space plays/pauses; arrows select; 1–5 rate and play the next unrated;
  0 clears; N writes a note; I toggles details; L/M/C/G switch views; ⌘K/Ctrl-K
  searches; ? shows shortcuts. Both system light/dark themes and reduced motion work.

New labs have `"sheets": false`. Set `"sheets": true` in `.sfx-lab/config.json`
to also write Markdown rating sheets. Existing configs without this key keep
sheets enabled. Studio updates the corresponding sheet fields; on learn, newer
sheet edits win and stale sheets cannot overwrite a Studio rating. Generated
sounds, including failures, live in the append-only `.sfx-lab/sounds.jsonl`
catalogue; older sheets are imported automatically. Cached browser features and
unrated notes stay inside `.sfx-lab/`. Jobs run locally; generation still uses the
configured generator and planning/learning still uses your configured agent.

All browser dependencies are vendored with licences under
`src/sfx_lab/studio/vendor/`; no runtime CDN or frontend build is needed:

- wavesurfer.js **7.8.6** — BSD-3-Clause (`wavesurfer.LICENSE`).
- @tanstack/table-core **8.20.5** UMD — MIT (`table-core.LICENSE`).
- Geist and Geist Mono variable WOFF2 from geist **1.3.1** — SIL OFL 1.1 (`geist.LICENSE`).

See [STUDIO_SPEC.md](STUDIO_SPEC.md) for the frozen Studio contract.

`taste.md` contains a generated profile, best/worst examples, and statistics.
Planning loads the project hot card and relevant taste, good/bad and recent-prompt slices. **My notes is
yours**: write permanent preferences there. Learning preserves your notes and
other text outside the auto markers, updating only the generated block and the
`modified`/`ratings` frontmatter values. `learn --no-agent` refreshes the
examples and numbers while retaining the previous profile.

For optional sheets in Obsidian, set `"sheets": true`, use the default `--embed obsidian` and open the lab folder as a
vault or put it inside one. Audio embeds reference filenames. For other Markdown
tools, `init --embed html` writes audio tags with relative paths. Taste, batches,
and audio paths are configurable; `frontmatter` in the lab config merges custom
metadata into newly written notes.

You can supply specs yourself without calling the agent:

```sh
sfx-lab plan "soft click" --specs specs.json
```

The JSON array accepts `prompt`, `duration_s` (null or 0.5–30),
`prompt_influence` (0–1), `loop`, `kind`, `explore`, `why`, and `from`
(a parent sound id, or null).
See [SPEC.md](SPEC.md) for the complete frozen behavior.

## Projects and context

```sh
sfx-lab project new "Tech Explainers" --description "My weekly show"
sfx-lab use tech-explainers
sfx-lab context
sfx-lab find "dry whoosh"
sfx-lab need "dry whoosh" -n 3
sfx-lab ratings --tag whoosh --stars 4-5
sfx-lab show 2026-10-23-01-01
sfx-lab taste --full
```

Every lab has `general`. Existing labs keep all files in place, including their
configured taste file; old records belong to `general`. New projects store taste
and a ≤1,400-character deterministic hot card in `projects/<slug>/`. Ratings and
notes are per project while audio stays shared. `project new --from <slug>` copies
the source's taste auto block and defaults. `project list` and `project show`
inspect projects. Selection: global `--project` → `SFX_LAB_PROJECT` → `use` → `general`.

`need` offers ≥4★ matches first and plans only the missing count. Use `--generate`
for a new batch. Planning uses compact context, relevant good/bad examples and the
last ten overlapping prompts in this project, with a bad-repeat guard and one
replacement round. `plan --verbose` prints prompt characters and approximate tokens.
`learn` updates only the active project's taste, including a bounded `## Hot`
section. Only ratings in a sound's originating project can settle that sound.

Studio Settings writes keys to `<lab>/.env` with mode 0600; APIs return only
connected/missing status. Choose Claude Code login (when installed) or an Anthropic
key. Project defaults override lab settings. `studio --ready-json --no-open --port 0`
prints one JSON line containing the bound URL, local API token and lab root for
local launchers.

## Claude Code skill

From the repo root, install the skill:

```sh
mkdir -p ~/.claude/skills && ln -s "$PWD/skill" ~/.claude/skills/sfx-lab
```

Alternatively, copy the `skill` folder to `~/.claude/skills/sfx-lab`. Then ask
Claude for sound effects.

## Custom generators

Adapters are duck typed and need no sfx-lab imports. Instantiate with no arguments:

```python
from pathlib import Path
from typing import Any, Protocol

class Generator(Protocol):
    name: str

    def estimate_usd(self, spec: dict[str, Any]) -> float: ...

    def generate(self, spec: dict[str, Any], dest: Path) -> dict[str, Any]:
        # Write atomically to dest.with_suffix(".wav") (or another extension).
        # Return {"path": str, "ref": str, "cost_usd": float | None}.
        # Raise an exception with a human-readable message on failure.
        ...

    def settle(self, ref: str, keep: bool) -> None:
        # Called once during learn after a rating; keep is false only for 1 star.
        ...
```

Register a class under the `sfx_lab.generators` entry point group, or set the
lab's `generator` to a dotted path such as `my_package.sounds:MyGenerator`.
Plans and sheet markers retain the generator name for generation and settlement.


The built-in ElevenLabs estimate uses $0.12 per minute ($0.002 per second),
with five seconds estimated for automatic duration. Agent usage runs on your
Claude Code login/subscription or your Anthropic API key and is separate from
the generation estimate. ElevenLabs output rights depend on your plan.

For local development and the offline test suite:

```sh
uv venv --python 3.12 .venv
uv pip install -p .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q
```

MIT licensed. Studio runs locally only; it does not install a background daemon.
