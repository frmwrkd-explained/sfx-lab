import json
from datetime import date
from pathlib import Path

import pytest

from sfx_lab.lab import (DEFAULTS, Lab, atomic_write, init_lab, load_env,
                         locate_lab, read_text, user_config_path, write_json)


def test_init_idempotent_and_options(lab: Lab) -> None:
    atomic_write(lab.taste_path, "user taste")
    config = lab.config | {"custom": "preserved", "max_usd": 0.123}
    write_json(lab.internal / "config.json", config)
    again = init_lab(lab.root, embed="html")
    assert read_text(again.taste_path) == "user taste"
    assert again.config["custom"] == "preserved"
    assert again.config["max_usd"] == 0.123
    assert again.config["embed"] == "html"
    assert json.loads(read_text(user_config_path()))["default_lab"] == str(lab.root)
    assert lab.audio_dir.is_dir() and lab.batches_dir.is_dir() and lab.plans_dir.is_dir()
    assert lab.ratings_path.is_file()


def test_defaults_when_config_sparse(lab: Lab) -> None:
    write_json(lab.internal / "config.json", {"generator": "custom"})
    assert Lab.open(lab.root).config == DEFAULTS | {"generator": "custom"}


def test_locating_precedence(lab: Lab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = init_lab(tmp_path / "other")
    nested = lab.root / "nested/deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.setenv("SFX_LAB_DIR", str(other.root))
    assert locate_lab(lab.root).root == lab.root
    assert locate_lab().root == other.root
    monkeypatch.delenv("SFX_LAB_DIR")
    assert locate_lab().root == lab.root
    monkeypatch.chdir(tmp_path)
    assert locate_lab().root == other.root
    user_config_path().unlink()
    with pytest.raises(ValueError, match="sfx-lab init"):
        locate_lab()
    with pytest.raises(ValueError, match="sfx-lab init"):
        locate_lab(tmp_path / "missing")


def test_dotenv_no_override(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    atomic_write(lab.root / ".env", '# comment\nexport SFX_TEST_ONE="quoted value"\nSFX_TEST_TWO=\'two\'\nSFX_TEST_THREE=three # comment\nSFX_TEST_KEEP=overwrite\ninvalid\n')
    monkeypatch.setenv("SFX_TEST_KEEP", "existing")
    for key in ("SFX_TEST_ONE", "SFX_TEST_TWO", "SFX_TEST_THREE"):
        monkeypatch.delenv(key, raising=False)
    load_env(lab)
    import os
    assert [os.environ[f"SFX_TEST_{key}"] for key in ("ONE", "TWO", "THREE", "KEEP")] == ["quoted value", "two", "three", "existing"]
    for key in ("SFX_TEST_ONE", "SFX_TEST_TWO", "SFX_TEST_THREE"):
        monkeypatch.delenv(key)


def test_batch_ids_across_plans_sheets_and_sidecars(lab: Lab) -> None:
    prefix = date.today().isoformat()
    assert lab.next_batch_id() == f"{prefix}-01"
    write_json(lab.plans_dir / f"{prefix}-01.json", {})
    atomic_write(lab.batches_dir / "renamed.md", f'---\nbatch: "{prefix}-02"\n---\n')
    atomic_write(lab.batches_dir / f"._SFX batch {prefix}-03.md", "sidecar")
    write_json(lab.plans_dir / f"._{prefix}-03.json", {})
    assert lab.next_batch_id() == f"{prefix}-03"


def test_plan_order_across_hundred_batches(lab: Lab) -> None:
    prefix = date.today().isoformat()
    for n in (99, 100, 98):
        write_json(lab.plans_dir / f"{prefix}-{n:02d}.json", {})
    assert [path.stem for path in lab.plans()] == [f"{prefix}-{n}" for n in (98, 99, 100)]


def test_init_records_the_python_for_the_desktop_app(tmp_path: Path) -> None:
    from sfx_lab.cli import main
    venv_python = tmp_path / "venv" / "bin" / "python"
    assert main(["init", str(tmp_path / "lab"), "--python", str(venv_python)]) == 0
    config = json.loads((tmp_path / "lab" / ".sfx-lab" / "config.json").read_text())
    assert config["python"] == str(venv_python)
