from typing import Any

import pytest

from sfx_lab.spec import Spec


@pytest.mark.parametrize("value", [None, [], "sound", 4, {}, {"prompt": "  "}, {"prompt": 3}])
def test_invalid_specs_dropped(value: Any) -> None:
    assert Spec.from_dict(value, "ui") is None


def test_clamping_and_serialization() -> None:
    spec = Spec.from_dict({"prompt": "  " + "a" * 1200, "duration_s": 50,
                           "prompt_influence": 1.4, "loop": 1, "explore": [], "why": "b" * 250}, "ui")
    assert spec is not None
    assert spec.to_dict() == {"prompt": "a" * 1000, "duration_s": 30.0,
                              "prompt_influence": 1.0, "loop": True, "kind": "ui",
                              "explore": False, "why": "b" * 200, "from": None}


@pytest.mark.parametrize(("duration", "influence", "expected_duration", "expected_influence"),
                         [(-3, -1, 0.5, 0.0), (1.24, 0.63, 1.2, 0.65),
                          (None, 0.3, None, 0.3), ("nan", "inf", None, 0.3),
                          ("oops", None, None, 0.3)])
def test_numeric_validation(duration: Any, influence: Any, expected_duration: float | None,
                            expected_influence: float) -> None:
    spec = Spec.from_dict({"prompt": " click ", "duration_s": duration, "prompt_influence": influence})
    assert spec is not None
    assert spec.prompt == "click"
    assert spec.duration_s == expected_duration
    assert spec.prompt_influence == expected_influence
