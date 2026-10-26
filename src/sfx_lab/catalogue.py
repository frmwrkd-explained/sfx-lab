"""Append-only sound inventory, including imports from earlier sheet-based labs."""

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .lab import Lab, read_text
from .ratings import LOG_LOCK, append_events


def read_catalogue(lab: Lab) -> list[dict[str, Any]]:
    path = lab.safe_path(".sfx-lab/sounds.jsonl")
    if not path.exists():
        return []
    records = (json.loads(line) for line in read_text(path).splitlines() if line.strip())
    return list({record["id"]: {"project": "general"} | record for record in records}.values())


def append_sound(lab: Lab, plan: dict[str, Any], n: int,
                 result: dict[str, Any]) -> dict[str, Any]:
    audio = ""
    if result.get("path"):
        path = Path(result["path"]).resolve()
        if not path.is_relative_to(lab.audio_dir):
            raise ValueError("Generator audio must stay inside the lab audio directory.")
        audio = str(path.relative_to(lab.root))
    record = {"project": plan.get("project", "general"), "id": f"{plan['batch']}-{n:02d}", "batch": plan["batch"], "n": n,
              "brief": plan.get("brief", ""), "kind": plan.get("kind", "sfx"),
              "generator": plan["generator"], "ref": result.get("ref", ""),
              "audio": audio, "spec": plan["specs"][n - 1],
              "created": datetime.now().isoformat(), "cost_usd": result.get("cost_usd"),
              "failed": "error" in result, "error": result.get("error", "")}
    append_events(lab.safe_path(".sfx-lab/sounds.jsonl"), [record])
    return record


def sync_catalogue(lab: Lab) -> list[dict[str, Any]]:
    with LOG_LOCK:
        known = {record["id"] for record in read_catalogue(lab)}
        imported = []
        for path in lab.sheets():
            text = read_text(path)
            start = 0
            for match in re.finditer(r"<!-- sfx-lab (.*?) -->", text):
                section = text[start:match.start()]
                start = match.end()
                try:
                    meta = json.loads(match[1])
                    batch, n = meta["batch"], int(meta["n"])
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d+", batch) or n < 1:
                        continue
                    ident = f"{batch}-{n:02d}"
                    if ident in known:
                        continue
                    audio = ""
                    embed = re.search(r"!\[\[([^\]\n]+)\]\]", section)
                    source = re.search(r'<audio\b[^>]*\bsrc=[\'"]([^\'"]+)', section)
                    filename = embed[1] if embed else Path(unquote(html.unescape(source[1]))).name if source else ""
                    if filename and not meta.get("failed"):
                        candidate = lab.safe_path(lab.audio_dir.relative_to(lab.root) / filename)
                        if not candidate.is_relative_to(lab.audio_dir):
                            raise ValueError("Invalid audio path")
                        audio = str(candidate.relative_to(lab.root))
                    error = re.search(r"\*\*Failed:\*\* (.*)", section)
                    created = re.search(r'(?m)^created:\s*"?([0-9T:.,+\-]+)', text.split("---", 2)[1] if text.startswith("---") else "")
                    record = {"project": meta.get("project", "general"), "id": ident, "batch": batch, "n": n, "brief": meta.get("brief", ""),
                              "kind": meta["spec"].get("kind", "sfx"), "generator": meta["generator"],
                              "ref": meta.get("ref", ""), "audio": audio, "spec": meta["spec"],
                              "created": created[1] if created else datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                              "cost_usd": None, "failed": bool(meta.get("failed")),
                              "error": error[1] if error else ""}
                    imported.append(record)
                    known.add(ident)
                except (KeyError, ValueError, TypeError):
                    continue
        append_events(lab.safe_path(".sfx-lab/sounds.jsonl"), imported)
        return read_catalogue(lab)
