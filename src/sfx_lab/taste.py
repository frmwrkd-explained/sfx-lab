"""Preserve the user's prose while replacing generated taste evidence."""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .lab import atomic_write, frontmatter, note_fields, read_text

START = "<!-- sfx-lab:auto:start -->"
END = "<!-- sfx-lab:auto:end -->"
INITIAL = "## Summary\nNo ratings yet.\n\n## Likes\n\n## Dislikes\n\n## Settings that work\n\n## Try next"


@dataclass
class Taste:
    auto: str
    notes: str


def _auto_bounds(text: str) -> tuple[int, int] | None:
    start = text.find(START)
    if start < 0:
        return None
    end = text.find(END, start + len(START))
    if end < 0:
        return None
    return text.rfind(START, 0, end) + len(START), end


def starter(extra: dict[str, Any] | None = None) -> str:
    fields = note_fields("What sound effects I like — learned by sfx-lab from my ratings", "active")
    fields.update({"aliases": ["sound taste"], "ratings": 0})
    fields.update(extra or {})
    return (frontmatter(fields) + "# Sound taste\n\n" + START + "\n" + INITIAL +
            "\n\n## Best examples\n\n## Worst examples\n\n## Numbers\n" + END +
            "\n\n## My notes\nWrite anything the agent should always know. sfx-lab never changes this section.\n")


def read_taste(path: Path) -> Taste:
    text = read_text(path)
    bounds = _auto_bounds(text)
    if bounds is not None:
        start, end = bounds
        return Taste(text[start:end].strip(), text[end + len(END):])
    position = text.find("## My notes")
    return Taste(INITIAL, text[position:] if position >= 0 else "")


def agent_part(auto: str) -> str:
    return re.split(r"(?m)^## Best examples\s*$", auto, maxsplit=1)[0].strip()


def update_frontmatter(text: str, updates: dict[str, str]) -> str:
    match = re.match(r"\A---\r?\n.*?\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if match is None:
        return text
    block = match.group()
    for key, value in updates.items():
        block = re.sub(rf"(?m)^{re.escape(key)}:[^\r\n]*", lambda _: f"{key}: {value}", block)
    return block + text[match.end():]


def clean_agent_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"\A```[^\n]*\n", "", text)
        text = re.sub(r"\n```\s*\Z", "", text)
    return re.sub(r"\A# [^\n]*(?:\n|$)", "", text).strip()


def write_auto(path: Path, agent_md: str, best_md: str, worst_md: str,
               numbers_md: str, ratings_count: int) -> None:
    text = read_text(path)
    block = ("\n" + agent_md.strip() + "\n\n## Best examples\n" + best_md +
             "\n\n## Worst examples\n" + worst_md + "\n\n## Numbers\n" + numbers_md + "\n")
    bounds = _auto_bounds(text)
    if bounds is not None:
        start, end = bounds
        text = text[:start] + block + text[end:]
    else:
        position = text.find("## My notes")
        if position < 0:
            position = len(text)
        text = text[:position] + START + block + END + "\n\n" + text[position:]
    text = update_frontmatter(text, {"modified": date.today().isoformat(), "ratings": str(ratings_count)})
    atomic_write(path, text)
