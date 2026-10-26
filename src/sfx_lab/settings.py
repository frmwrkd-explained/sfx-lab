"""Studio settings and write-only local credentials."""

import json
import os
import re
from typing import Any

from .agents import ClaudeCLI
from .lab import Lab, atomic_write, read_text, write_json
from .ratings import LOG_LOCK
from .projects import defaults, get_project, project_path, validate_defaults

KEYS = {"elevenlabs": "ELEVENLABS_API_KEY", "anthropic": "SFX_LAB_ANTHROPIC_KEY"}


def key_status() -> dict[str, str]:
    return {name: "connected" if os.environ.get(key) or (name == "anthropic" and os.environ.get("ANTHROPIC_API_KEY")) else "missing"
            for name, key in KEYS.items()}


def settings(lab: Lab) -> dict[str, Any]:
    try:
        ClaudeCLI().binary()
        claude = True
    except RuntimeError:
        claude = False
    return {"project": lab.project, "defaults": defaults(lab), "keys": key_status(),
            "agent": lab.config["agent"], "claude_available": claude}


def save_settings(lab: Lab, data: dict[str, Any]) -> None:
    values = validate_defaults(data.get("defaults", {}))
    agent = data.get("agent", lab.config["agent"])
    if agent not in ("claude-cli", "anthropic-api"):
        raise ValueError("Choose Claude Code login or Anthropic key.")
    with LOG_LOCK:
        project = get_project(lab)
        write_json(project_path(lab, lab.project), project | {"defaults": project["defaults"] | values})
        config = json.loads(read_text(lab.internal / "config.json"))
        write_json(lab.internal / "config.json", config | {"agent": agent})
        lab.config.update(values | {"agent": agent})


def save_keys(lab: Lab, data: dict[str, Any]) -> dict[str, str]:
    if set(data) - KEYS.keys() or any(not isinstance(v, str) or any(c in v for c in '\r\n\x00') for v in data.values()):
        raise ValueError("Keys must be single-line text in elevenlabs or anthropic fields.")
    path = lab.safe_path(".env")
    text = read_text(path) if path.exists() else ""
    for name, value in data.items():
        key = KEYS[name]
        # Strip duplicate assignments, preserving unrelated entries and comments.
        aliases = [key, "ANTHROPIC_API_KEY"] if name == "anthropic" else [key]
        for alias in aliases:
            text = re.sub(rf"(?m)^\s*(?:export\s+)?{alias}\s*=[^\n]*(?:\n|$)", "", text)
            os.environ.pop(alias, None)
        if value:
            text = text.rstrip("\n") + ("\n" if text else "") + key + "=" + json.dumps(value) + "\n"
        os.environ[key] = value
    atomic_write(path, text)
    path.chmod(0o600)
    return key_status()
