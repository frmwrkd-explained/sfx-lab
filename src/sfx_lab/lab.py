"""Lab discovery, configuration, and atomic file operations."""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "version": 1, "generator": "elevenlabs", "agent": "claude-cli", "model": "",
    "taste_file": "taste.md", "batches_dir": "batches", "audio_dir": "audio",
    "embed": "obsidian", "explore": 0.3, "max_usd": 0.5, "batch_size": 6,
    "frontmatter": {}, "sheets": True,
}


def atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def user_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "sfx-lab/config.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(read_text(path))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


@dataclass
class Lab:
    root: Path
    config: dict[str, Any]
    project: str = "general"

    @classmethod
    def open(cls, root: Path | str) -> "Lab":
        root = Path(root).expanduser().resolve()
        path = cls(root, {}).safe_path(".sfx-lab/config.json")
        if not path.is_file():
            raise ValueError(f"No lab at {root}. Run sfx-lab init.")
        lab = cls(root, DEFAULTS | _read_json(path))
        from .projects import ensure_general
        ensure_general(lab)
        return lab

    def safe_path(self, relative: str | Path) -> Path:
        value = Path(relative)
        if value.is_absolute() or ".." in value.parts or "\\" in str(relative):
            raise ValueError("Paths must stay inside the lab.")
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Paths must stay inside the lab.")
        return path

    @property
    def internal(self) -> Path:
        return self.safe_path(".sfx-lab")

    @property
    def plans_dir(self) -> Path:
        return self.safe_path(".sfx-lab/plans")

    @property
    def ratings_path(self) -> Path:
        return self.safe_path(".sfx-lab/ratings.jsonl")

    @property
    def taste_path(self) -> Path:
        return self.safe_path(self.config["taste_file"])

    @property
    def batches_dir(self) -> Path:
        return self.safe_path(self.config["batches_dir"])

    @property
    def audio_dir(self) -> Path:
        return self.safe_path(self.config["audio_dir"])

    def sheets(self) -> list[Path]:
        return sorted(self.safe_path(p.relative_to(self.root)) for p in self.batches_dir.glob("*.md")
                      if not p.name.startswith("._"))

    def plans(self) -> list[Path]:
        paths = [self.safe_path(p.relative_to(self.root)) for p in self.plans_dir.glob("*.json")
                 if not p.name.startswith("._") and re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d+", p.stem)]
        return sorted(paths, key=lambda p: (p.stem.rsplit("-", 1)[0],
                                           int(p.stem.rsplit("-", 1)[-1])))

    def next_batch_id(self) -> str:
        prefix = date.today().isoformat()
        used = {p.stem for p in self.plans()}
        for path in self.sheets():
            # A renamed sheet still reserves its batch identifier.
            used.update(re.findall(r'(?m)^batch:\s*"?(\d{4}-\d{2}-\d{2}-\d+)"?\s*$', read_text(path)))
            used.update(re.findall(r"\d{4}-\d{2}-\d{2}-\d+", path.name))
        from .catalogue import read_catalogue
        used.update(record["batch"] for record in read_catalogue(self))
        n = 1
        while f"{prefix}-{n:02d}" in used:
            n += 1
        return f"{prefix}-{n:02d}"


def locate_lab(directory: str | Path | None = None) -> Lab:
    selected = directory or os.environ.get("SFX_LAB_DIR")
    if selected:
        return Lab.open(selected)
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        if (parent / ".sfx-lab/config.json").is_file():
            return Lab.open(parent)
    default = _read_json(user_config_path()).get("default_lab")
    if default:
        return Lab.open(default)
    raise ValueError("No lab found. Run sfx-lab init.")


def load_env(lab: Lab) -> None:
    path = lab.safe_path(".env")
    if not path.exists():
        return
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            if value[0] == '"':
                try:
                    value = json.loads(value)
                except ValueError:
                    value = value[1:-1]
            else:
                value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def note_fields(summary: str, status: str = "draft") -> dict[str, Any]:
    today = date.today().isoformat()
    return {"created": today, "modified": today,
            "uid": datetime.now().strftime("%Y%m%d%H%M%S"), "type": "resource",
            "aliases": [], "summary": summary, "status": status, "tags": ["sfx-lab"]}


def frontmatter(fields: dict[str, Any]) -> str:
    lines = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---\n"


def init_lab(directory: str | Path = "./sfx-lab", **options: Any) -> Lab:
    root = Path(directory).expanduser().resolve()
    path = Lab(root, {}).safe_path(".sfx-lab/config.json")
    config = DEFAULTS | ({"sheets": False} if not path.exists() else {}) | _read_json(path)
    config.update({key: value for key, value in options.items() if value is not None})
    lab = Lab(root, config)
    for folder in (lab.plans_dir, lab.batches_dir, lab.audio_dir):
        folder.mkdir(parents=True, exist_ok=True)
    write_json(path, config)
    if not lab.ratings_path.exists():
        atomic_write(lab.ratings_path, "")
    if not lab.taste_path.exists():
        from .taste import starter
        atomic_write(lab.taste_path, starter(config["frontmatter"]))
    user_path = user_config_path()
    write_json(user_path, _read_json(user_path) | {"default_lab": str(root)})
    from .projects import ensure_general
    ensure_general(lab)
    if options.get("taste_file") is not None:
        from .projects import get_project, project_path
        write_json(project_path(lab, "general"), get_project(lab, "general") | {"taste_file": config["taste_file"]})
    return lab
