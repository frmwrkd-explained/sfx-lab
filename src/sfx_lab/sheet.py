"""Editable Markdown batch sheets with machine-readable item markers."""

import html
import json
import os
import re
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .lab import Lab, atomic_write, frontmatter, note_fields, read_text
from .ratings import parse_rating, settings_text
from .taste import update_frontmatter


@dataclass
class Item:
    metadata: dict[str, Any]
    rating: str = ""
    note: str = ""


def sheet_path(lab: Lab, batch: str, brief: str) -> Path:
    slug = re.sub(r'[/\\:*?"<>|#^\[\]]', "", brief[:40]).strip()
    return lab.batches_dir / f"SFX batch {batch} – {slug}.md"


def write_sheet(lab: Lab, plan: dict[str, Any], results: list[dict[str, Any]]) -> Path:
    path = sheet_path(lab, plan["batch"], plan["brief"])
    fields = note_fields(f"SFX batch {plan['batch']}: {plan['brief']}")
    fields["related"] = [f"[[{lab.taste_path.stem}]]"]
    fields.update(lab.config["frontmatter"])
    fields.update({key: plan[key] for key in ("batch", "brief", "kind", "generator")})
    fields.update(cost_usd=round(sum(result.get("cost_usd") or 0 for result in results), 6), state="open")
    header = frontmatter(fields).replace('state: "open"', "state: open")
    parts = [header + f"# {plan['brief']}\n\n",
             "Rate each sound 1–5 stars (type 1–5, or love/good/ok/bad). Add a reason after `why::` — reasons teach the most. Then run `sfx-lab learn`.\n"]
    for n, (spec, result) in enumerate(zip(plan["specs"], results), 1):
        meta = {"project": plan.get("project", "general"), "batch": plan["batch"], "n": n, "ref": result.get("ref", ""),
                "generator": plan["generator"], "brief": plan["brief"], "spec": spec}
        section = f"\n## {n:02d}\n> " + spec["prompt"].replace("\n", "\n> ") + "\n\n"
        if "error" in result:
            section += f"**Failed:** {result['error']}\n"
            meta["failed"] = True
        else:
            audio = Path(result["path"])
            if lab.config["embed"] == "html":
                relative = quote(os.path.relpath(audio, path.parent).replace(os.sep, "/"), safe="/.")
                section += f'<audio controls src="{html.escape(relative, quote=True)}"></audio>\n'
            else:
                section += f"![[{audio.name}]]\n"
        section += f"\n`{settings_text(spec)}`"
        if spec.get("explore"):
            section += f" · experiment: {spec.get('why', '')}"
        elif spec.get("why"):
            section += f" · {spec['why']}"
        section += "\n\n"
        if not meta.get("failed"):
            section += "rating:: \nwhy:: \n"
        marker = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).replace("-->", "--\\u003e")
        section += "<!-- sfx-lab " + marker + " -->\n"
        parts.append(section)
    atomic_write(path, "".join(parts))
    return path


def parse_sheet(path: Path) -> list[Item]:
    items: list[Item] = []
    rating, note = "", ""
    for line in read_text(path).splitlines():
        field = re.match(r"^\s*[-*]?\s*(rating|why)::\s*(.*)$", line, re.IGNORECASE)
        if field:
            if field[1].lower() == "rating":
                rating = field[2].strip()
            else:
                note = field[2].strip()
        marker = re.search(r"<!-- sfx-lab (.*?) -->", line)
        if marker:
            try:
                meta = json.loads(marker[1])
                if not isinstance(meta, dict) or not all(key in meta for key in ("batch", "n", "spec", "generator")):
                    raise ValueError("missing item fields")
                if not meta.get("failed"):
                    items.append(Item(meta, rating, note))
            except (ValueError, TypeError) as error:
                warnings.warn(f"{path.name}: invalid sfx-lab marker: {error}", stacklevel=2)
            rating, note = "", ""
    return items


def mark_learned(path: Path, items: list[Item]) -> bool:
    for item in items:
        try:
            if parse_rating(item.rating) is None:
                return False
        except ValueError:
            return False
    text = read_text(path)
    updated = update_frontmatter(text, {"state": "learned", "modified": date.today().isoformat()})
    if updated != text:
        atomic_write(path, updated)
    return True


def sync_rating(lab: Lab, batch: str, n: int, stars: int | None, note: str) -> None:
    """Replace only the two fields belonging to the matching marker."""
    for path in lab.sheets():
        lines = read_text(path).splitlines(keepends=True)
        start = 0
        changed = False
        for end, line in enumerate(lines):
            marker = re.search(r"<!-- sfx-lab (.*?) -->", line)
            if not marker:
                continue
            try:
                meta = json.loads(marker[1])
            except ValueError:
                start = end + 1
                continue
            if meta.get("batch") == batch and meta.get("n") == n:
                for i in range(start, end):
                    if re.match(r"^\s*[-*]?\s*rating::", lines[i], re.I):
                        lines[i] = f"rating:: {stars if stars else ''}\n"
                        changed = True
                    elif re.match(r"^\s*[-*]?\s*why::", lines[i], re.I):
                        lines[i] = "why:: " + " ".join(note.splitlines()) + "\n"
                        changed = True
            start = end + 1
        if changed:
            atomic_write(path, "".join(lines))
