"""Deterministic prompt tags in vocabulary order; no model or audio analysis."""

import re

ACTION_TAGS = tuple((
    "click tap knock pop whoosh swipe slide hit thud buzz beep chime crack snap rustle drop "
    "impact riser glitch shutter typing scroll tick sparkle ambience room-tone"
).split())

VOCABULARY = tuple((
    "glass metal wood paper plastic rubber cloth ceramic stone water air cardboard "
    "click tap knock pop whoosh swipe slide hit thud buzz beep chime crack snap rustle drop "
    "impact riser glitch shutter typing scroll tick sparkle ambience room-tone "
    "dry wet close distant soft hard bright dark warm airy muted harsh crisp round tight "
    "short long subtle minimal heavy light "
    "roomy reverb tail echo sub-bass boom rumble cinematic synth digital organic"
).split())
VARIANTS = {"wooden": "wood", "metallic": "metal", "buzzer": "buzz", "reverberant": "reverb",
            "reverberation": "reverb", "glassy": "glass", "papery": "paper", "softly": "soft",
            "brightness": "bright", "crispness": "crisp", "synthesized": "synth",
            "synthetic": "synth", "rustling": "rustle", "sliding": "slide"}


def tags_for(prompt: str) -> list[str]:
    text = re.sub(r"\broom\s+tone\b", "room-tone", prompt.lower())
    words = re.findall(r"[a-z]+(?:-[a-z]+)*", text)
    normalized = set()
    negated_until = -1
    for index, word in enumerate(words):
        if word in {"no", "not", "without", "zero", "never", "minus"} or (
                word == "of" and index > 0 and words[index - 1] == "free"):
            negated_until = index + 3
            continue
        if index <= negated_until:
            continue
        word = VARIANTS.get(word, word)
        if word not in VOCABULARY:
            for suffix in ("es", "s", "ing", "ed"):
                if word.endswith(suffix) and word[:-len(suffix)] in VOCABULARY:
                    word = word[:-len(suffix)]
                    break
        normalized.add(word)
    return [tag for tag in VOCABULARY if tag in normalized]
