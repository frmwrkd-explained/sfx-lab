"""Rating aliases, event log, and deterministic taste evidence."""

import json
import re
import string
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from .lab import atomic_write, read_text

SCORES = {"love": 5, "good": 4, "ok": 3, "bad": 1}
ALIASES = {
    5: ("5", "love", "❤️", "❤", "♥", "++"),
    4: ("4", "good", "👍", "+", "yes"),
    3: ("3", "ok", "meh", "~"),
    2: ("2",),
    1: ("1", "bad", "👎", "-", "x", "no"),
}
LOG_LOCK = RLock()
Event = dict[str, Any]


def parse_rating(text: str) -> int | None:
    value = text.strip().lower()
    if not value:
        return None
    lookup = {alias: rating for rating, aliases in ALIASES.items() for alias in aliases}
    # Symbol aliases such as '+' and '-' are ratings themselves, not punctuation.
    while value and value not in lookup and value[-1] in string.punctuation + "。！？…":
        value = value[:-1].rstrip()
    if value in lookup:
        return lookup[value]
    if re.fullmatch(r"★{1,5}", value):
        return len(value)
    match = re.fullmatch(r"([1-5])(?:★|\s+stars?)", value)
    if match:
        return int(match[1])
    raise ValueError(f"Unknown rating: {text!r}")


def read_log(path: Path) -> list[Event]:
    if not path.exists():
        return []
    events = [json.loads(line) for line in read_text(path).splitlines() if line.strip()]
    for event in events:
        if "stars" not in event:
            event["stars"] = SCORES.get(event.get("rating"), None)
        event.setdefault("project", "general")
        event["score"] = event["stars"]
        event["rating"] = str(event["stars"]) if event["stars"] else ""
    return events


def append_events(path: Path, events: list[Event]) -> None:
    if events:
        with LOG_LOCK:
            previous = path.read_bytes() if path.exists() else b""
            separator = b"\n" if previous and not previous.endswith(b"\n") else b""
            added = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
            atomic_write(path, previous + separator + added.encode("utf-8"))


def latest(log: Iterable[Event], project: str = "general") -> dict[tuple[str, int], Event]:
    return {(item["batch"], item["n"]): item for item in log if item.get("project", "general") == project}


def is_new(previous: Event | None, rating: int | None, note: str) -> bool:
    return previous is None or previous.get("stars") != rating or previous["note"] != note


def _duration(value: float | None) -> str:
    if value is None:
        return "auto"
    if value < 0.5:
        return "<0.5 s"
    if value < 1:
        return "0.5–1 s"
    if value < 3:
        return "1–3 s"
    if value <= 8:
        return "3–8 s"
    return ">8 s"


def _influence(value: float) -> str:
    labels = ["0–0.2", "0.2–0.4", "0.4–0.6", "0.6–0.8", "0.8–1"]
    return labels[min(4, max(0, int(round(value * 100) // 20)))]


def stats(items: dict[tuple[str, int], Event] | Iterable[Event]) -> list[Event]:
    values = items.values() if isinstance(items, dict) else items
    buckets: dict[tuple[str, str], list[int]] = {}
    for item in values:
        if item.get("stars", item.get("score")) is None:
            continue
        spec = item["spec"]
        settings = {
            "duration": _duration(spec.get("duration_s")),
            "influence": _influence(spec.get("prompt_influence", 0.3)),
            "loop": "on" if spec.get("loop") else "off",
            "kind": spec.get("kind", "sfx"),
            "explore": "experiment" if spec.get("explore") else "follows taste",
        }
        for key in settings.items():
            buckets.setdefault(key, []).append(item["score"])
    order = {
        "duration": ["auto", "<0.5 s", "0.5–1 s", "1–3 s", "3–8 s", ">8 s"],
        "influence": ["0–0.2", "0.2–0.4", "0.4–0.6", "0.6–0.8", "0.8–1"],
        "loop": ["on", "off"],
        "kind": sorted(value for setting, value in buckets if setting == "kind"),
        "explore": ["follows taste", "experiment"],
    }
    rows: list[Event] = []
    for setting, labels in order.items():
        for value in labels:
            scores = buckets.get((setting, value))
            if scores:
                rows.append({"setting": setting, "value": value, "rated": len(scores),
                             "avg_score": round(sum(scores) / len(scores), 2)})
    return rows


def stats_markdown(items: dict[tuple[str, int], Event] | Iterable[Event]) -> str:
    rows = stats(items)
    if not rows:
        return ""
    return "| Setting | Value | Rated | Avg stars |\n| --- | --- | --- | --- |\n" + "\n".join(
        f"| {row['setting']} | {row['value']} | {row['rated']} | {row['avg_score']:.2f} |"
        for row in rows)


def settings_text(spec: Event) -> str:
    duration = spec.get("duration_s")
    length = "auto" if duration is None else f"{duration:g} s"
    return f"{length} · influence {spec.get('prompt_influence', 0.3):g} · " + (
        "loop" if spec.get("loop") else "no loop")


def example(item: Event, experiment: bool = False) -> str:
    spec = item["spec"]
    result = f"- **{item.get('stars', item['score'])}★** — \"{spec['prompt']}\" ({settings_text(spec)})"
    if experiment and spec.get("explore"):
        result += f" [experiment: {spec.get('why', '')}]"
    if item.get("note"):
        result += f" — {item['note']}"
    return result


def examples(items: dict[tuple[str, int], Event] | Iterable[Event]) -> tuple[str, str]:
    values = list(items.values() if isinstance(items, dict) else items)
    best = sorted((item for item in values if (item.get("stars", item["score"]) or 0) >= 4),
                  key=lambda item: (item["score"], item["ts"]), reverse=True)[:5]
    worst = sorted((item for item in values if 0 < (item.get("stars", item["score"]) or 0) <= 2),
                   key=lambda item: item["ts"], reverse=True)[:5]
    return "\n".join(map(example, best)), "\n".join(map(example, worst))
