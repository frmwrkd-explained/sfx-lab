"""Validated inputs to sound generators."""

import math
import re
from dataclasses import asdict, dataclass
from typing import Any


def _number(value: Any, default: float | None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError, OverflowError):
        return default


@dataclass
class Spec:
    prompt: str
    duration_s: float | None = None
    prompt_influence: float = 0.3
    loop: bool = False
    kind: str = "sfx"
    explore: bool = False
    why: str = ""
    from_: str | None = None

    @classmethod
    def from_dict(cls, d: Any, kind: str = "sfx") -> "Spec | None":
        if not isinstance(d, dict) or not isinstance(d.get("prompt"), str):
            return None
        prompt = d["prompt"].strip()[:1000]
        if not prompt:
            return None
        duration = _number(d.get("duration_s"), None)
        if duration is not None:
            duration = round(max(0.5, min(30.0, duration)), 1)
        influence = _number(d.get("prompt_influence", 0.3), 0.3)
        assert influence is not None
        influence = round(round(max(0.0, min(1.0, influence)) * 20) / 20, 2)
        parent = d.get("from")
        if parent is not None and (not isinstance(parent, str) or
                                   not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d+-\d{2,}", parent)):
            return None
        return cls(prompt, duration, influence, bool(d.get("loop", False)),
                   str(d.get("kind", kind)), bool(d.get("explore", False)),
                   str(d.get("why", ""))[:200], parent)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["from"] = data.pop("from_")
        return data
