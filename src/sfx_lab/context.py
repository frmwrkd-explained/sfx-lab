"""Bounded, deterministic project context and shared-library discovery."""

import json
import re
from collections import Counter
from typing import Any

from .catalogue import sync_catalogue
from .lab import Lab, atomic_write, read_text
from .projects import defaults, get_project
from .ratings import latest, read_log, stats
from .tags import ACTION_TAGS, tags_for
from .taste import read_taste

MORE = 'More: sfx-lab find "<need>" · sfx-lab show <id> · sfx-lab taste'


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def cut(text: str, cap: int) -> str:
    text = " ".join(text.split())
    if len(text) <= cap:
        return text
    return text[:cap + 1].rsplit(" ", 1)[0] if " " in text[:cap + 1] else text[:cap]


def sections(auto: str) -> dict[str, str]:
    return {m[1].strip(): m[2].strip() for m in re.finditer(r"(?m)^## ([^\n]+)\n([\s\S]*?)(?=^## |\Z)", auto + "\n")}


def taste_slice(lab: Lab, full: bool = False) -> str:
    auto = read_taste(lab.taste_path).auto
    if full:
        return auto
    return "\n\n".join(f"## {name}\n{body}" for name, body in sections(auto).items()
                        if name in ("Likes", "Dislikes", "Settings that work", "Try next"))


def _fallback_phrases(body: str, cap: int, count: int) -> str:
    phrases: list[str] = []
    for line in body.splitlines():
        bullet = re.match(r"^\s*(?:[-*+] |\d+[.)] )\s*(.+)", line)
        if not bullet:
            continue
        phrase = bullet[1].strip()
        # A bold label may put its colon inside or outside the emphasis markers.
        phrase = re.sub(r"^(?:\*\*|__)[^:\n]+?(?::(?:\*\*|__)|(?:\*\*|__):)\s*", "", phrase)
        phrase = re.sub(r"[*_`]+", "", phrase)
        phrase = re.sub(r"^(?:likes?|dislikes?|avoid|open|question|try next|preference|material|texture|source|attack|tail|space|brightness|length|influence|loop):\s*", "", phrase, flags=re.IGNORECASE)
        phrase = re.split(r" — |—|\(|:|,", phrase, maxsplit=1)[0]
        phrase = " ".join(phrase.split()).strip().lower()
        if not phrase or phrase in phrases or len(phrase) > cap:
            continue
        if len(" · ".join(phrases + [phrase])) > cap:
            break
        phrases.append(phrase)
        if len(phrases) == count:
            break
    return " · ".join(phrases)


def hot_values(auto: str) -> dict[str, str]:
    profile_sections = sections(auto)
    hot = profile_sections.get("Hot", "")
    result = {}
    for name, heading, cap, count in (("Likes", "Likes", 90, 4),
                                      ("Avoid", "Dislikes", 90, 4),
                                      ("Open", "Try next", 110, 2)):
        match = re.search(rf"(?m)^{name}:[ \t]*([^\n]*)", hot)
        value = match[1] if match else ""
        value = " · ".join(p.strip() for p in re.split(r"[,·]", value)[:count] if p.strip())
        value = "" if len(value) > cap and " " not in value[:cap + 1] else cut(value, cap)
        result[name] = value or _fallback_phrases(profile_sections.get(heading, ""), cap, count)
    return result


def normalize_hot(profile: str) -> str:
    values = hot_values(profile)
    if "Hot" not in sections(profile) and not any(values.values()):
        return profile
    before = re.split(r"(?m)^## Hot\s*$", profile, maxsplit=1)[0].rstrip()
    return before + "\n\n## Hot\n" + "\n".join(f"{key}: {value}".rstrip() for key, value in values.items())


def hot_card(lab: Lab) -> str:
    project = get_project(lab)
    events = [e for e in latest(read_log(lab.ratings_path), lab.project).values() if e.get("stars")]
    settings = defaults(lab)
    length = "auto" if settings["length"] is None else "–".join(f"{v:g}" for v in settings["length"]) + " s"
    influence = "–".join(f"{v:g}" for v in settings["influence"])
    buckets = stats(events)
    has_evidence = False
    for setting in ("duration", "influence"):
        candidates = [b for b in buckets if b["setting"] == setting and b["rated"] >= 3]
        if candidates:
            has_evidence = True
            best = max(candidates, key=lambda b: (b["avg_score"], b["rated"]))["value"]
            if setting == "duration":
                length = best
            else:
                influence = best
    text = read_text(lab.taste_path)
    modified = re.search(r"(?m)^modified:\s*([^\n]+)", text)
    name = cut(project["name"], 100)
    header = f"# SFX · {name} · no ratings yet"
    if events:
        header = f"# SFX · {name} · {len(events)} rated · {sum(e['stars'] for e in events)/len(events):.1f}★ avg · taste {modified[1].strip().strip(chr(34)) if modified else 'pending'}"
    lines = [header]
    values = hot_values(read_taste(lab.taste_path).auto)
    if events:
        lines += [f"Likes: {values['Likes']}", f"Avoid: {values['Avoid']}"]
    settings_text = f"{length} · influence {influence}"
    if not has_evidence and settings["influence"] == [0, 1] and settings["length"] is None:
        settings_text = "no evidence yet"
    lines.append(f"Settings: {settings_text} · loop only for beds")
    if events:
        typed = [(e, set(tags_for(e["spec"]["prompt"])) & set(ACTION_TAGS)) for e in events]
        counts = Counter(tag for _, tags in typed for tag in tags)
        candidates = []
        for tag, count in counts.items():
            rated = [e for e, tags in typed if e["stars"] >= 4 and tag in tags]
            if rated:
                event = max(rated, key=lambda e: (e["stars"], e["ts"], e["batch"], e["n"]))
                candidates.append((tag, count, event))
        bests = []
        used_ids = set()
        for tag, _, event in sorted(candidates, key=lambda item: (-item[2]["stars"], -item[1], item[0])):
            ident = f"{event['batch']}-{event['n']:02d}"
            if ident in used_ids:
                continue
            used_ids.add(ident)
            bests.append(f"{tag} {ident} ({event['stars']}★)")
            if len(bests) == 6:
                break
        lines += ["Best: " + " · ".join(bests), f"Open: {values['Open']}"]
    lines.append(MORE)
    card = "\n".join(lines) + "\n"
    # Fixed caps above reserve ample space; bound adversarial metadata as well.
    if len(card) > 1400:
        card = cut("\n".join(lines[:-1]), 1400 - len(MORE) - 2) + "\n" + MORE + "\n"
    path = lab.safe_path(f"projects/{lab.project}/hot.md")
    if not path.exists() or read_text(path) != card:
        atomic_write(path, card)
    return card.rstrip()


def find(lab: Lab, need: str, min_stars: int | None = None, all_projects: bool = False,
         include_bad: bool = False, limit: int = 10) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100:
        raise ValueError("Limit must be 1–100.")
    events = latest(read_log(lab.ratings_path), lab.project)
    notes_path = lab.safe_path(".sfx-lab/notes.json")
    notes = json.loads(read_text(notes_path)) if notes_path.exists() else {}
    query_tags, query_words = set(tags_for(need)), words(need)
    found = []
    for sound in sync_catalogue(lab):
        event = events.get((sound["batch"], sound["n"]), {})
        stars = event.get("stars")
        note = event.get("note", notes.get(f"{lab.project}:{sound['id']}", notes.get(sound["id"], "") if lab.project == "general" else ""))
        if sound.get("failed") or (stars == 1 and not include_bad) or (min_stars is not None and (stars or 0) < min_stars):
            continue
        if not all_projects and sound.get("project", "general") != lab.project and not event:
            continue
        tags = tags_for(sound["spec"]["prompt"])
        overlap = len(query_tags & set(tags)) * 2 + len(query_words & words(" ".join((sound["spec"]["prompt"], sound.get("brief", ""), note))))
        score = overlap * {5: 1, 4: .85, None: .5, 3: .4, 2: .1, 1: .05}[stars]
        if score:
            found.append(sound | {"stars": stars, "note": note, "tags": tags, "match_score": score})
    return sorted(found, key=lambda s: (-s["match_score"], -(s["stars"] or 0), s["id"]))[:limit]


def row(sound: dict[str, Any]) -> str:
    spec = sound["spec"]
    duration = "auto" if spec.get("duration_s") is None else f"{spec['duration_s']:g}s"
    stars = f"{sound['stars']}★" if sound.get("stars") else "—★"
    return f'{sound.get("id", str(sound["batch"]) + "-" + format(sound["n"], "02d"))}  {stars}  {duration}  {sound.get("kind", spec.get("kind", "sfx"))}  "{cut(spec["prompt"], 60)}"  — {cut(sound.get("note", ""), 120)}  {sound.get("audio", "")}'


def show(lab: Lab, ident: str) -> dict[str, Any]:
    sounds = sync_catalogue(lab)
    sound = next((s for s in sounds if s["id"] == ident), None)
    if sound is None:
        raise ValueError("Unknown sound identifier.")
    log = read_log(lab.ratings_path)
    ratings = {e.get("project", "general"): e for e in log if (e["batch"], e["n"]) == (sound["batch"], sound["n"])}
    return sound | {"ratings": ratings, "parent": sound["spec"].get("from"),
                    "children": [s["id"] for s in sounds if s["spec"].get("from") == ident]}
