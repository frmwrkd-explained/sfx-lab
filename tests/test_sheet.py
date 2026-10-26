from pathlib import Path
from typing import Any

import pytest

from sfx_lab.lab import Lab, atomic_write, read_text
from sfx_lab.sheet import mark_learned, parse_sheet, write_sheet
from sfx_lab.spec import Spec


def make_sheet(lab: Lab) -> tuple[Path, dict[str, Any]]:
    data = {"batch": lab.next_batch_id(), "brief": "soft UI / click? [test]",
            "kind": "ui", "generator": "tests.fakes:FakeGenerator",
            "specs": [Spec("dry click", 0.5, 0.6, explore=True, why="crisp transient").to_dict(),
                      Spec("wet click").to_dict(), Spec("broken").to_dict()]}
    audio = lab.audio_dir / f"sfx-{data['batch']}-01.mp3"
    results = [{"path": str(audio), "ref": "one", "cost_usd": 0.01},
               {"path": str(audio.with_name("second.mp3")), "ref": "two", "cost_usd": 0.01},
               {"error": "service unavailable"}]
    return write_sheet(lab, data, results), data


def test_sheet_round_trip_failures_and_user_text(lab: Lab) -> None:
    path, data = make_sheet(lab)
    text = read_text(path)
    assert path.name == f"SFX batch {data['batch']} – soft UI  click test.md"
    assert "experiment: crisp transient" in text
    assert "**Failed:** service unavailable" in text
    assert text.count("rating::") == 2
    assert '![[sfx-' in text
    assert 'related: ["[[taste]]"]' in text
    text = text.replace("rating:: \nwhy:: ", "- RATING:: ❤️!\n* WHY:: crisper than #2", 1)
    text = text.replace("\n## 02", "\nExtra user text is ignored.\n\n## 02")
    atomic_write(path, text)
    items = parse_sheet(path)
    assert len(items) == 2
    assert items[0].rating == "❤️!" and items[0].note == "crisper than #2"
    assert items[0].metadata["spec"] == data["specs"][0]
    assert items[1].rating == "" and items[1].note == ""
    assert not mark_learned(path, items)
    atomic_write(path, text.replace("rating:: \n", "rating:: bad\n"))
    before = read_text(path).split("---", 2)[2]
    assert mark_learned(path, parse_sheet(path))
    assert "state: learned" in read_text(path)
    assert read_text(path).split("---", 2)[2] == before


def test_invalid_rating_does_not_complete_sheet(lab: Lab) -> None:
    path, _ = make_sheet(lab)
    atomic_write(path, read_text(path).replace("rating:: \n", "rating:: unknown\n"))
    assert not mark_learned(path, parse_sheet(path))


def test_html_relative_path_and_frontmatter_merge(lab: Lab) -> None:
    lab.config.update(embed="html", frontmatter={"bucket": "creative", "status": "custom", "batch": "wrong", "state": "wrong"})
    path, data = make_sheet(lab)
    text = read_text(path)
    assert f'<audio controls src="../audio/sfx-{data["batch"]}-01.mp3"></audio>' in text
    assert 'bucket: "creative"' in text and 'status: "custom"' in text
    assert f'batch: "{data["batch"]}"' in text
    assert "wrong" not in text
    assert "state: open" in text


def test_invalid_marker_warns_and_resets_pending(lab: Lab) -> None:
    path, _ = make_sheet(lab)
    atomic_write(path, 'rating:: love\nwhy:: discarded\n<!-- sfx-lab {oops} -->\n' + read_text(path))
    with pytest.warns(UserWarning, match="invalid sfx-lab marker"):
        items = parse_sheet(path)
    assert len(items) == 2 and items[0].rating == items[0].note == ""


def test_marker_survives_prompt_with_comment_terminator(lab: Lab) -> None:
    data = {"batch": lab.next_batch_id(), "brief": "test", "kind": "sfx", "generator": "fake",
            "specs": [Spec("click --> tail").to_dict()]}
    path = write_sheet(lab, data, [{"path": str(lab.audio_dir / "one.mp3")}])
    assert parse_sheet(path)[0].metadata["spec"]["prompt"] == "click --> tail"
