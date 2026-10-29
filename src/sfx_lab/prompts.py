"""The two bounded-context prompts used for planning and learning."""

from .ratings import Event, example
from .taste import Taste

# Shared by the plan and score agents. Learned 2026-10-28 from ElevenLabs Explore
# download counts, the official sound-effects docs (450-character prompt limit,
# prompt influence default 0.3) and our own P1-P4 prompt ladder.
SFX_PROMPT_RULES = """How to write each ElevenLabs prompt, in this order:
- The use first: what the sound is for (transition for a hard cut, UI tap, logo reveal, bed under a voiceover).
- The length in seconds, the same number as duration_s; leave numbers out when duration_s is null. Under 0.5 s, write e.g. "0.2 seconds then silence" and use 0.5.
- The source with its material and size, then the shape: attack (crisp, soft) and tail (short dry tail, cut to silence).
- Where the weight sits when it matters: sub, low-mid body, bright, no highs.
- One source per prompt. A sound with phases gets a short timeline in order, e.g. "0 to 3 s: air swell. At 3 s: one deep hit."
- End with "isolated, clean" and exclusions: no music (unless asked), no voice, plus anything unwanted the model tends to add (reverb, metallic ring, laser).
- Beds and ambiences: loop true, an even texture with no distinct hits; the word "loopable" does nothing.
- prompt_influence: 0.2 to 0.3 to explore variations; 0.5 to 0.7 for detailed, timed or UI sounds where every word must land.
- Stay well under the 450-character ElevenLabs limit."""


def plan_prompt(taste: Taste, stats_md: str, recent_prompts: list[str], brief: str,
                kind: str, n: int, explore: float, hot: str = "", good: str = "", bad: str = "") -> str:
    experiments = round(n * explore)
    recent = "\n".join(f"- {prompt}" for prompt in recent_prompts[:10]) or "Nothing yet."
    return f'''You design sound effects for a video creator who rates every sound. Learn from their taste and propose new sounds.

## Project hot card
{hot}

## Their taste (learned from ratings)
{taste.auto}

## Their own notes (always follow these)
{taste.notes}

## Build on these (set from to the sound id)
{good or "No good matches yet."}

## Never produce anything like these
{bad or "No overlapping bad ratings."}

## Already tried (do not repeat these prompts)
{recent}

## Task
Brief: {brief}
Kind: {kind}
Write exactly {n} sound specs for ElevenLabs Sound Effects (v3 by default).
- About {n - experiments} should follow their taste closely. The other {experiments} are experiments: each tests ONE open question from "Try next" or an untested setting. Set "explore": true on those and say in "why" what it tests.
- Between related specs, change one thing at a time (wording, length, influence) so the ratings teach something.
- prompt: a concrete physical description — source, material, action, space, mic distance, attack and tail. No brand names. No music unless the brief asks. Max 300 characters.
{SFX_PROMPT_RULES}
- duration_s: 0.5 to 30 seconds, or null to let the model choose.
- prompt_influence: 0 to 1 (0 = loose and creative, 1 = literal).
- Set `from` to the id you build on, if any.
- ElevenLabs has no volume control. Requests for louder/more volume mean closer mic, more punch, fuller body, more presence.
- loop: true only for beds or ambiences meant to repeat seamlessly.
Reply with ONLY a JSON array, no prose:
[{{"prompt": "...", "duration_s": 1.0, "prompt_influence": 0.3, "loop": false, "explore": false, "why": "..."}}]'''


def learn_prompt(taste: Taste, stats_md: str, new_items: list[Event],
                 best_md: str, worst_md: str) -> str:
    new = "\n".join(example(item, experiment=True) for item in new_items)
    return f'''You keep a short taste profile of the sound effects a video creator likes. Update it from their newest ratings.
Ratings are 1–5 stars; 1 star means discard. Their "why" notes matter most.

## Current profile
{taste.auto}

## Their own notes (context only; do not copy them)
{taste.notes}

## Numbers from all ratings
{stats_md}

## Best so far
{best_md}

## Worst so far
{worst_md}

## New ratings
{new}

Write the updated profile in Markdown with exactly these sections, in this order:
## Summary   (2–3 sentences)
## Likes
## Dislikes
## Settings that work   (length, prompt influence, loop — cite the numbers)
## Try next   (2–4 open questions the next batch should test)
## Hot
Likes: tight airy whooshes · organic paper rustle · round soft pops
Avoid: synth tones · long tails · musical chimes
Open: glass vs metal clicks? · longer whooshes at low influence?
The Hot lines above are a filled example: replace them with this creator's taste. Fill every line; never leave one empty.
Likes and Avoid: up to 4 short phrases each, at most 90 characters per line value. Open: up to 2 short questions, at most 110 characters.
Use short bullets with concrete sound words (material, attack, tail, space, brightness, length). Keep earlier conclusions unless new ratings contradict them. End every bullet in Likes, Dislikes and Settings that work with how many ratings support it, taken only from the ratings above — never estimate — e.g. "(4 ratings)". With fewer than 3, write "(tentative, 1 rating)" or "(tentative, 2 ratings)". Max 300 words. Reply with only the Markdown.'''
