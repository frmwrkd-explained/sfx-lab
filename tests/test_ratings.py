from pathlib import Path
from typing import Any

import pytest

from sfx_lab.ratings import (ALIASES, SCORES, append_events, examples, is_new, latest,
                             parse_rating, read_log, stats, stats_markdown)
from sfx_lab.spec import Spec


@pytest.mark.parametrize(("alias", "expected"), [(a, r) for r, aliases in ALIASES.items() for a in aliases])
def test_every_alias(alias: str, expected: int) -> None:
    assert parse_rating(f"  {alias.upper()}  ") == expected
    assert parse_rating(f"{alias}!") == expected


@pytest.mark.parametrize("text", ["", " \t\n"])
def test_empty_rating(text: str) -> None:
    assert parse_rating(text) is None


@pytest.mark.parametrize("text", ["great", "0", "-1", "6", "!!!", "love good"])
def test_unknown_rating(text: str) -> None:
    with pytest.raises(ValueError):
        parse_rating(text)


def event(n: int, rating: str, duration: float | None = None,
          influence: float = 0.3) -> dict[str, Any]:
    return {"ts": float(n), "batch": "batch", "n": n, "rating": str(SCORES[rating]), "stars": SCORES[rating],
            "score": SCORES[rating], "note": "", "settled": True,
            "spec": Spec(f"sound {n}", duration, influence, loop=n % 2 == 0,
                         kind="ui" if n % 2 else "foley", explore=n % 2 == 1).to_dict()}


def test_latest_new_and_append_only_log(tmp_path: Path) -> None:
    path = tmp_path / "ratings.jsonl"
    old = event(1, "good")
    append_events(path, [old])
    prefix = path.read_bytes()
    changed = old | {"rating": "5", "stars": 5, "score": 5, "ts": 9.0}
    append_events(path, [changed, event(2, "bad")])
    assert path.read_bytes().startswith(prefix)
    current = latest(read_log(path))
    assert len(current) == 2
    assert current[("batch", 1)] == changed | {"project": "general"}
    assert not is_new(changed, 5, "")
    assert is_new(changed, 5, "new note")
    assert is_new(changed, 1, "")
    assert is_new(None, 5, "")


def test_stats_all_buckets_and_averages() -> None:
    durations = [None, 0.4, 0.5, 1.0, 3.0, 8.1]
    influences = [0, 0.2, 0.4, 0.6, 0.8, 1]
    events = [event(n, "love" if n % 2 else "bad", d, i)
              for n, (d, i) in enumerate(zip(durations, influences), 1)]
    rows = stats(latest(events))
    values = {setting: {r["value"] for r in rows if r["setting"] == setting}
              for setting in ("duration", "influence", "loop", "kind", "explore")}
    assert values == {
        "duration": {"auto", "<0.5 s", "0.5–1 s", "1–3 s", "3–8 s", ">8 s"},
        "influence": {"0–0.2", "0.2–0.4", "0.4–0.6", "0.6–0.8", "0.8–1"},
        "loop": {"on", "off"}, "kind": {"ui", "foley"},
        "explore": {"experiment", "follows taste"}}
    assert all(row["rated"] >= 1 for row in rows)
    assert "| Setting | Value | Rated | Avg stars |" in stats_markdown(events)
    combined = stats([event(1, "love"), event(2, "good"), event(3, "bad")])
    duration_row = next(row for row in combined if row["setting"] == "duration")
    assert duration_row["avg_score"] == 3.33
    assert stats([]) == []
    assert stats_markdown([]) == ""


def test_best_worst_order_and_format() -> None:
    events = [event(n, "love" if n < 7 else "bad", 0.4, 0.6) for n in range(1, 14)]
    events[5]["note"] = "crisper than #2"
    best, worst = examples(events)
    assert len(best.splitlines()) == len(worst.splitlines()) == 5
    assert best.splitlines()[0] == '- **5★** — "sound 6" (0.4 s · influence 0.6 · loop) — crisper than #2'
    assert "sound 13" in worst.splitlines()[0]
    assert "sound 9" in worst.splitlines()[-1]
    auto, _ = examples([event(1, "good")])
    assert "(auto · influence 0.3 · no loop)" in auto


def test_best_examples_only_include_positive_scores() -> None:
    best, worst = examples([event(1, "bad"), event(2, "ok"), event(3, "good"), event(4, "love")])
    assert [line.split('**')[1] for line in best.splitlines()] == ["5★", "4★"]
    assert '**1★**' in worst
    assert examples([event(1, "bad"), event(2, "ok")])[0] == ""


def test_stats_stable_setting_and_bucket_order() -> None:
    durations = [None, 0.4, 0.5, 1.0, 3.0, 8.1]
    influences = [0, 0.2, 0.4, 0.6, 0.8, 1]
    events = [event(n, "good", duration, influence)
              for n, (duration, influence) in enumerate(zip(durations, influences), 1)]
    expected = [(setting, value) for setting, values in [
        ("duration", ["auto", "<0.5 s", "0.5–1 s", "1–3 s", "3–8 s", ">8 s"]),
        ("influence", ["0–0.2", "0.2–0.4", "0.4–0.6", "0.6–0.8", "0.8–1"]),
        ("loop", ["on", "off"]),
        ("kind", ["foley", "ui"]),
        ("explore", ["follows taste", "experiment"]),
    ] for value in values]
    forward = stats(latest(events))
    backward = stats(latest(reversed(events)))
    assert [(row["setting"], row["value"]) for row in forward] == expected
    assert forward == backward
    assert stats_markdown(events) == stats_markdown(reversed(events))
