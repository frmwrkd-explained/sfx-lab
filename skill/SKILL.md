---
name: sfx-lab
description: Find, make, refine, and rate sound effects with SFX Lab, made new with ElevenLabs or found in the Epidemic Sound library. Sound-design this edit or score a video.
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
If Epidemic Sound is needed and not connected, run `sfx-lab open` and tell the user:
"Click the gear icon in SFX Studio, paste your Epidemic Sound API key, and press Save."
To see which keys are connected without reading them, check only whether the
`ELEVENLABS_API_KEY` or `EPIDEMIC_SOUND_API_KEY` line exists in the lab's `.env`.

## Sources

SFX Lab has two sources. Pick one per batch with `--source` on `plan`, `need` or `batch`.
- `elevenlabs`: makes new sounds from the prompt. It costs money per sound.
- `epidemic`: finds existing sounds in the Epidemic Sound library and downloads
  them. Nothing is made. It costs nothing per sound; the user's Epidemic Sound
  plan covers it. Each sound is a different library sound, never a repeat.
Without `--source`, the lab's default source is used (set in Studio Settings).
Use `epidemic` when the user says "Epidemic", "from the library", or "real" sounds.
Use `elevenlabs` when the user says "make", "generate", "new", or "ElevenLabs".
If the user does not say, use the default and say in one short sentence which source it is.
An Epidemic Sound key on the free plan is only for testing: tell the user once
that sounds from a free key may not be used in published videos.

## Work

1. First run `sfx-lab context`. When the user names a project or show,
   use `sfx-lab --project <slug> context`; run `sfx-lab project list` if unsure.
   Read nothing else yet. Keep the same project selected for later commands.
2. For each need, run `sfx-lab find "<need>"`. Offer good matches first
   with their ids. Find sounds before making more.
3. For missing sounds, run `sfx-lab need "<brief>" -n <count>` or
   `sfx-lab plan "<brief>" -n <count>`, adding `--source epidemic` or
   `--source elevenlabs` when the user chose one. Show the plan and price. Ask before
   spending, then run `sfx-lab run --yes`. Let `need` reuse good matches.
   For an Epidemic Sound plan, show the search words of each sound, say that it
   costs nothing per sound, and ask before downloading.
4. After any generation or download finishes, run `sfx-lab open`.
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

For Epidemic Sound, the planner also writes "search": 2 to 4 plain library words,
the sound type first ("whoosh cloth fast"), different for each sound. The prompt
still describes the sound fully, so the ratings keep teaching the taste profile.
With `--specs <file>`, add a "search" field to each spec for Epidemic Sound.

## Sound-design this edit

Scoring a video always uses ElevenLabs.
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
Add `--source epidemic` to find similar library sounds instead of making new ones.
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
