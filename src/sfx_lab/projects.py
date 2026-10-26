"""Project metadata and selection; audio always belongs to the shared lab."""

import json
import math
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from .lab import DEFAULTS, Lab, atomic_write, read_text, write_json
from .taste import read_taste, starter

PROJECT_DEFAULTS: dict[str, Any] = {"kind": "sfx", "influence": [0, 1], "length": None}


def slug_for(name: str) -> str:
    value = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")[:40].rstrip("-")
    if not value:
        raise ValueError("Project name must contain a letter or number.")
    return value


def project_path(lab: Lab, slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{1,40}", slug):
        raise ValueError("Invalid project slug (use 1–40 lowercase letters, digits or hyphens).")
    return lab.safe_path(f".sfx-lab/projects/{slug}.json")


def ensure_general(lab: Lab) -> None:
    path = project_path(lab, "general")
    if path.exists():
        return
    state_path = lab.safe_path(".sfx-lab/state.json")
    state = json.loads(read_text(state_path)) if state_path.exists() else {}
    data = {"name": "General", "slug": "general", "created": datetime.now().isoformat(),
            "defaults": PROJECT_DEFAULTS | {key: lab.config[key] for key in ("batch_size", "explore", "max_usd")},
            "description": "", "taste_file": lab.config["taste_file"]}
    for key in ("distilled_events", "last_learn_ts"):
        if key in state:
            data[key] = state.pop(key)
    write_json(path, data)
    if state_path.exists():
        write_json(state_path, state)
    lab.safe_path("projects/general").mkdir(parents=True, exist_ok=True)
    if lab.taste_path.is_file():
        from .context import hot_card
        hot_card(lab)


def get_project(lab: Lab, slug: str | None = None) -> dict[str, Any]:
    ensure_general(lab)
    path = project_path(lab, lab.project if slug is None else slug)
    if not path.is_file():
        raise ValueError(f"Unknown project: {slug}")
    return json.loads(read_text(path))


def defaults(lab: Lab) -> dict[str, Any]:
    return PROJECT_DEFAULTS | {key: lab.config[key] for key in ("batch_size", "explore", "max_usd")} | get_project(lab)["defaults"]


def select_project(lab: Lab, slug: str | None = None) -> Lab:
    ensure_general(lab)
    state_path = lab.safe_path(".sfx-lab/state.json")
    state = json.loads(read_text(state_path)) if state_path.exists() else {}
    chosen = slug if slug is not None else os.environ.get("SFX_LAB_PROJECT") or state.get("active_project") or "general"
    data = get_project(lab, chosen)
    validate_defaults(data["defaults"])
    # Reload the lab config to prevent the previous project's defaults leaking across switches.
    config = DEFAULTS | json.loads(read_text(lab.internal / "config.json")) | data["defaults"]
    config["taste_file"] = data.get("taste_file", f"projects/{chosen}/taste.md")
    return Lab(lab.root, config, chosen)


def use_project(lab: Lab, slug: str) -> Lab:
    selected = select_project(lab, slug)
    path = lab.safe_path(".sfx-lab/state.json")
    state = json.loads(read_text(path)) if path.exists() else {}
    write_json(path, state | {"active_project": slug})
    return selected


def validate_defaults(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"kind", "batch_size", "explore", "max_usd", "influence", "length"}:
        raise ValueError("Invalid project defaults.")
    for key, item in value.items():
        if key == "kind":
            if not isinstance(item, str) or not item.strip():
                raise ValueError("Kind must be text.")
        elif key in ("length", "influence"):
            if item is None and key == "length":
                continue
            lo, hi = (0.5, 30) if key == "length" else (0, 1)
            if (not isinstance(item, list) or len(item) != 2 or
                    any(type(v) not in (int, float) or not math.isfinite(v) for v in item) or
                    not lo <= item[0] <= item[1] <= hi):
                raise ValueError(f"Invalid {key} range.")
        elif (type(item) not in (int, float) or not math.isfinite(item) or
              (key == "batch_size" and (type(item) is not int or not 1 <= item <= 100)) or
              (key == "explore" and not 0 <= item <= 1) or (key == "max_usd" and item < 0)):
            raise ValueError(f"Invalid {key}.")
    return value


def create_project(lab: Lab, name: str, from_slug: str | None = None, description: str = "") -> dict[str, Any]:
    ensure_general(lab)
    if not isinstance(name, str) or not isinstance(description, str):
        raise ValueError("Name and description must be text.")
    slug = slug_for(name)
    path = project_path(lab, slug)
    if path.exists():
        raise ValueError(f"Project already exists: {slug}")
    source = get_project(lab, from_slug) if from_slug else None
    base_config = DEFAULTS | json.loads(read_text(lab.internal / "config.json"))
    fresh_defaults = PROJECT_DEFAULTS | {key: base_config[key] for key in ("batch_size", "explore", "max_usd")}
    data = {"name": name.strip(), "slug": slug, "created": datetime.now().isoformat(),
            "defaults": defaults(select_project(lab, from_slug)) if source else fresh_defaults,
            "description": description, "taste_file": f"projects/{slug}/taste.md"}
    taste_path = lab.safe_path(data["taste_file"])
    atomic_write(taste_path, starter(lab.config["frontmatter"]))
    if source:
        taste = read_taste(select_project(lab, from_slug).taste_path)
        # Copy just the auto block, never the other project's personal notes.
        text = read_text(taste_path)
        from .taste import START, END
        atomic_write(taste_path, text[:text.index(START) + len(START)] + "\n" + taste.auto + "\n" + text[text.index(END):])
    write_json(path, data)
    from .context import hot_card
    hot_card(select_project(lab, slug))
    return data


def list_projects(lab: Lab) -> list[dict[str, Any]]:
    from .catalogue import read_catalogue
    from .ratings import latest, read_log
    ensure_general(lab)
    log, sounds = read_log(lab.ratings_path), read_catalogue(lab)
    result = []
    for path in sorted((lab.internal / "projects").glob("*.json")):
        if path.name.startswith("._"):
            continue
        data = get_project(lab, path.stem)
        events = latest(log, path.stem)
        stars = [e["stars"] for e in events.values() if e.get("stars")]
        own = [s for s in sounds if s.get("project", "general") == path.stem]
        result.append(data | {"rated": len(stars), "avg_stars": round(sum(stars) / len(stars), 2) if stars else None,
                              "unrated": sum(not s.get("failed") and not events.get((s["batch"], s["n"]), {}).get("stars") for s in own),
                              "last_batch": max((s["batch"] for s in own), default=None)})
    return result
