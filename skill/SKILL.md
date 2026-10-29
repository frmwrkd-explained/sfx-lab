---
name: sfx-lab
description: Find, make, refine, and rate sound effects with SFX Lab. Sound-design this edit or score a video.
---

Talk to the user in short plain sentences with no technical words.
Never ask the user to type commands. Run them yourself.
Never ask for keys in chat. Never print keys or read `.env` aloud.
Never rate for the user. Never play audio.

## Setup

If `sfx-lab` is not found, check `$HOME/.local/bin/sfx-lab` and use that full path.
If it is not installed, clone the repo the user named and run `sh install.sh`
from that repo (Windows: `./install.ps1`). It needs no input.
Use the installed full path if the command is still not found.
If ElevenLabs is not connected, run `sfx-lab open` and tell the user:
"Click the gear icon in SFX Studio, paste your ElevenLabs API key, and press Save."

## Work

1. First run `sfx-lab context`. When the user names a project or show,
   use `sfx-lab --project <slug> context`; run `sfx-lab project list` if unsure.
   Read nothing else yet. Keep the same project selected for later commands.
2. For each need, run `sfx-lab find "<need>"`. Offer good matches first
   with their ids. Find sounds before making more.
3. For missing sounds, run `sfx-lab need "<brief>" -n <count>` or
   `sfx-lab plan "<brief>" -n <count>`. Show the plan and price. Ask before
   spending, then run `sfx-lab run --yes`. Let `need` reuse good matches.
4. After any generation finishes, run `sfx-lab open`.
   Tell the user: "Listen to the sounds and give them stars in SFX Studio."
5. For "Open SFX Lab", listening, or rating, run `sfx-lab open`.
   Add `--project <slug>` to every `open` when a project is in use.
   Tell the user in one short plain sentence what to do there.
6. After the user rates, run `sfx-lab learn`, then `sfx-lab context`.
   Briefly tell them what was learned.

## How to write a great SFX prompt

Use these rules when you write or change a prompt, and when you explain a plan.
1. Say the use first: "Transition whoosh for a hard cut", "UI tap for a button".
2. Say the length in seconds. Use the same number as the length setting.
3. Name the source, its material and size: "thick leather", "small plastic key".
4. Say how it starts and ends: "crisp attack, short dry tail", "cut to silence".
5. Say where the weight sits when it matters: "sub", "low-mid body", "no highs".
6. One source per sound. For a build and a hit, give a short timeline in order.
7. End with "isolated, clean" and what to leave out: "no music, no voice, no reverb".
8. For beds and room tone, use loop. The word "loopable" does nothing.
9. Prompt influence 0.3 gives variety. Use 0.5 to 0.7 when every word must land.
10. Stay well under 450 characters.

Good: "Transition whoosh for a hard cut in a tech explainer, 1 second. Fast cloth swipe,
left to right, low-mid body, crisp attack, short dry tail. Isolated, no music, no voice."

## Sound-design this edit

For "Sound-design this edit", run `sfx-lab score "<video>" --plan-only`.
Keep the same lab and project. Show the cue sheet and price. Ask before spending.
After they agree, run `sfx-lab score --resume <score-id> --yes`.
Run `sfx-lab open`, adding `--project <slug>` when one is in use.
Tell the user: "Listen and give stars in SFX Studio."
The cue view is coming separately. The generated takes appear in the library now.

## Refine one sound

For feedback like "make 03 more crisp", "like 05 but shorter", or "more volume",
find the full sound id shown in Studio or by `sfx-lab find` / `sfx-lab show`.
Ask which sound only if the reference is unclear.
Run `sfx-lab plan --from <id> --feedback "<their words>" -n 3`.
Show the plan and price, ask before spending, then run `sfx-lab run --yes`.
Run `sfx-lab open`, adding `--project <slug>` when one is in use.
Tell the user: "Listen to the new sounds and give them stars in SFX Studio."

## More

Load deeper detail only when needed: `sfx-lab show <id>`, `sfx-lab taste`
(`--full` for examples and numbers), or `sfx-lab ratings` with filters.
Use `--lab <folder>` before the command if the wrong library is selected.
Create projects with `sfx-lab project new "<Name>" [--from <slug>]`.
Select a saved default with `sfx-lab use <slug>`.
Use `--project <slug>` before other commands when a project is in use.
