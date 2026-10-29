"""Saved cue sheets, reuse-first scoring, voice lines, and editor deliverables."""

import json
import math
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from . import score_export, score_media
from .agents import Agent, load_agent, safe_message
from .catalogue import append_sound, read_catalogue
from .context import find, hot_card, hot_values, taste_slice, words
from .generators import Generator, SFX_CREDITS_PER_SECOND, load_generator
from .lab import Lab, atomic_write, read_text, write_json
from .projects import defaults
from .prompts import SFX_PROMPT_RULES
from .run import _bad_repeat, _json_array
from .ratings import latest, read_log
from .spec import Spec
from .taste import read_taste

Emit = Callable[[str], None]
SCORE_LOCK = threading.RLock()
EVENT_TYPES = ("cut", "whoosh-in", "impact", "ui-pop", "riser", "ambience-bed", "voice-line")
PROMPTS = {
    "cut": "A single dry air whoosh for a hard picture cut, close in a quiet edit suite, restrained and precise, fast attack with a short soft tail, isolated, no music or speech.",
    "whoosh-in": "A narrow air swipe entering from the left in a quiet studio, light and focused, smooth acceleration into a crisp stop, isolated, no music or speech.",
    "impact": "One tight muted rubber hit for a graphic landing on a clean digital canvas, confident and subtle, crisp attack with a short low tail, isolated, no music or speech.",
    "ui-pop": "One small dry plastic pop for a button appearing on a clean screen, playful but restrained, close microphone and a round quick release, isolated, no music or speech.",
    "riser": "A soft filtered air swell in an empty studio leading to a visual reveal, gently tense, steadily rising then stopping cleanly, isolated, no music or speech.",
    "ambience-bed": "A quiet airy room tone in a small calm interior, neutral and unobtrusive, even texture with no distinct transients, clean, no music or speech.",
}


def score_path(lab: Lab, ident: str) -> Path:
    if not isinstance(ident, str) or not re.fullmatch(r"score-[a-z0-9-]{1,80}", ident):
        raise ValueError("Invalid score id.")
    return lab.safe_path(f".sfx-lab/scores/{ident}.json")


def load_score(lab: Lab, ident: str | None = None) -> dict[str, Any]:
    if ident is None:
        scores = list_scores(lab)
        if not scores:
            raise ValueError("No edit has been scored in this project yet.")
        ident = scores[-1]["id"]
    path = score_path(lab, ident)
    if not path.is_file():
        raise ValueError("Score not found.")
    data = json.loads(read_text(path))
    if data.get("id") != ident or data.get("project") != lab.project:
        raise ValueError("Score belongs to another project or has an invalid id.")
    return data


def list_scores(lab: Lab) -> list[dict[str, Any]]:
    folder = lab.safe_path(".sfx-lab/scores")
    result = []
    for path in sorted(folder.glob("score-*.json")):
        data = json.loads(read_text(lab.safe_path(path.relative_to(lab.root))))
        if data.get("project") == lab.project:
            result.append({key: data[key] for key in ("id", "name", "project", "created", "state", "render_dirty")})
    return sorted(result, key=lambda s: (s["created"], s["id"]))


def cue_table(data: dict[str, Any]) -> str:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    lines = ["| Cue | Start (s) | End (s) | Event | Take / need | Prompt |", "|---|---:|---:|---|---|---|"]
    for cue in data["cues"]:
        lines.append(f"| {cue['id']} | {cue['start']:.3f} | {cue['end']:.3f} | {cue['event_type']} | "
                     f"{cell(cue.get('take_id') or cue['status'])} | {cell(cue['prompt'])} |")
    return "\n".join(lines)


def save_score(lab: Lab, data: dict[str, Any]) -> None:
    write_json(score_path(lab, data["id"]), data)
    write_json(lab.safe_path(data["outputs"]["cues_json"]), data)
    atomic_write(lab.safe_path(data["outputs"]["cues_md"]), f"# {data['name']}\n\n" + cue_table(data) + "\n")


def fit_events(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Coalesce events closer than 0.5 s so the API minimum never crosses the next event."""
    fps = Fraction(analysis["fps"])
    last = math.floor(Fraction(str(analysis["duration"])) * fps)
    minimum = math.ceil(fps / 2)
    candidates = [(0, "ambience-bed")]
    candidates += [(score_export.frames(t, str(fps)), "cut") for t in analysis["cuts"]]
    candidates += [(score_export.frames(t, str(fps)), "impact") for t in analysis["motion_hits"]]
    selected: list[tuple[int, str]] = []
    for frame, kind in sorted(set(candidates), key=lambda e: (e[0], e[1] != "cut")):
        if frame < 0 or last - frame < minimum:
            continue
        if selected and frame - selected[-1][0] < minimum:
            continue
        selected.append((frame, kind))
    # Long quiet gaps use independent <=30 s ambience windows.
    expanded = []
    for n, (start, kind) in enumerate(selected):
        end = selected[n + 1][0] if n + 1 < len(selected) else last
        expanded.append((start, kind))
        cursor = start + math.floor(30 * fps)
        while cursor + minimum <= end:
            expanded.append((cursor, "ambience-bed"))
            cursor += math.floor(30 * fps)
    result = []
    for n, (start, kind) in enumerate(expanded):
        end = expanded[n + 1][0] if n + 1 < len(expanded) else last
        result.append({"id": f"cue-{n + 1:03d}", "start": float(start / fps), "gap_end": float(end / fps), "event_type": kind})
    if not result:
        raise ValueError("The edit is too short for a frame-aligned 0.5 second cue.")
    if len(result) > 999:
        raise ValueError("This edit has more than 999 cues; split it into shorter edits for CMX3600 export.")
    return result


def _cue_specs(lab: Lab, analysis: dict[str, Any], no_agent: bool, agent: Agent | None) -> list[dict[str, Any]]:
    events = fit_events(analysis)
    taste = read_taste(lab.taste_path)
    entries = []
    if not no_agent:
        request = (
            "Sound-design this edit. Return only a JSON array, one object per event, with id, event_type, prompt, "
            "duration_s, prompt_influence. Keep ids. Event types: cut, whoosh-in, impact, ui-pop, riser, ambience-bed. "
            "Do not invent dialogue. One specific ElevenLabs-style prompt per cue: action, setting, mood, performance, "
            "material, attack and tail. No music unless requested in the user's notes. Durations 0.5-30 s, within the "
            "gap to the next event. An ambience-bed is the exception: it runs under the hits until the next "
            "ambience-bed or the end of the edit (30 s max), so write its prompt and duration_s for that whole span, "
            "even, loopable, no distinct sounds. Visual detections are timing heuristics, not descriptions of the scene.\n"
            + SFX_PROMPT_RULES + "\n" + hot_card(lab) + "\n" + taste_slice(lab) + "\nUser notes:\n" + taste.notes
            + "\nEvents:\n" + json.dumps(events) + "\nTranscript:\n" + json.dumps(analysis.get("transcript", []))
        )
        agent = agent or load_agent(lab.config["agent"], lab.config["model"])
        entries = _json_array(agent.ask(request))
        if any(not isinstance(e, dict) for e in entries):
            raise ValueError("Score agent must return cue objects.")
        ids = [e.get("id") for e in entries]
        if len(set(ids)) != len(ids) or set(ids) != {e["id"] for e in events}:
            raise ValueError("Score agent must return every event id exactly once.")
    by_id = {e["id"]: e for e in entries}
    settings = defaults(lab)
    bad = [e for e in latest(read_log(lab.ratings_path), lab.project).values() if e.get("stars") and e["stars"] <= 2]
    fps = Fraction(analysis["fps"])
    cues = []
    for event in events:
        entry = by_id.get(event["id"], {})
        kind = entry.get("event_type", event["event_type"])
        if kind not in PROMPTS:
            raise ValueError("Score agent returned an unsupported event type.")
        prompt = entry.get("prompt", PROMPTS[kind])
        if no_agent and (likes := hot_values(taste.auto)["Likes"]):
            prompt += f" Preferred texture: {likes}."
        spec = Spec.from_dict({"prompt": prompt, "duration_s": entry.get("duration_s", 30 if kind == "ambience-bed" else .7),
                               "prompt_influence": entry.get("prompt_influence", .3), "kind": kind, "loop": kind == "ambience-bed"})
        if spec is None or _bad_repeat(spec.prompt, bad):
            raise ValueError("A cue prompt is invalid or repeats a sound rated badly. Revise the plan before spending.")
        gap = event["gap_end"] - event["start"]
        if kind == "ambience-bed":
            # A bed runs under the hits on its own lane, until the next bed or the end of the edit (30 s max).
            later = [e["start"] for e in events if e["start"] > event["start"]
                     and by_id.get(e["id"], {}).get("event_type", e["event_type"]) == "ambience-bed"]
            gap = (min(later) if later else analysis["duration"]) - event["start"]
        # A bed always fills its span; a shorter agent length would leave silence under the hits.
        duration = min(30, gap) if kind == "ambience-bed" else min(30, gap, spec.duration_s or gap)
        if settings["length"] is not None:
            lo, hi = settings["length"]
            duration = min(gap, hi, max(lo, duration))
        duration_frames = max(math.ceil(fps / 2), math.floor(Fraction(str(duration)) * fps + Fraction(1, 100000)))
        duration_frames = min(duration_frames, math.floor(30 * fps), score_export.frames(gap, str(fps)))
        end = event["start"] + float(duration_frames / fps)
        influence = min(settings["influence"][1], max(settings["influence"][0], spec.prompt_influence))
        cues.append({"id": event["id"], "start": event["start"], "end": end, "event_type": kind, "prompt": spec.prompt,
                     "prompt_influence": influence, "loop": kind == "ambience-bed", "take_id": None, "take_ids": [],
                     "status": "need", "error": ""})
    return cues


def cue_spec(cue: dict[str, Any]) -> dict[str, Any]:
    # Exact fitted duration is retained: rounding to tenths could overrun the next event. A frame-fitted
    # half second can land a hair under 0.5 s in float (15 frames at 30 fps) and the API rejects it, so
    # snap to the millisecond and never go below the 0.5 s floor; the mix still trims at the cue end.
    length = max(0.5, round(cue["end"] - cue["start"], 3))
    return {"prompt": cue["prompt"], "duration_s": length, "prompt_influence": cue["prompt_influence"],
            "kind": cue["event_type"], "loop": cue["loop"], "explore": False, "why": cue["id"], "from": None,
            **({"voice_id": cue["voice_id"]} if cue.get("voice_id") else {})}


def _take(lab: Lab, ident: str) -> dict[str, Any]:
    sound = next((s for s in read_catalogue(lab) if s["id"] == ident and not s.get("failed")), None)
    if sound is None or not sound.get("audio"):
        raise ValueError("Take not found or failed.")
    path = lab.safe_path(sound["audio"])
    if not path.is_relative_to(lab.audio_dir) or not path.is_file():
        raise ValueError("Take audio is missing or outside the library audio directory.")
    return sound


def _cue_matches(lab: Lab, cue: dict[str, Any], min_stars: int | None) -> list[str]:
    if cue["event_type"] == "voice-line":
        return []
    matches = find(lab, cue["prompt"], min_stars=min_stars, all_projects=True, limit=100)
    ids = []
    # Reuse the action requested, rather than an unrelated sound sharing only "quiet".
    actions = {"cut": {"whoosh", "swipe", "swoosh"}, "whoosh-in": {"whoosh", "swipe", "swoosh"},
               "impact": {"hit", "impact", "thud"}, "ui-pop": {"pop"}, "riser": {"riser", "swell", "rising"},
               "ambience-bed": {"ambience", "ambient", "bed", "tone"}}[cue["event_type"]]
    for match in matches:
        if match["spec"].get("kind") == "voice-line" or not actions & words(match["spec"]["prompt"]):
            continue
        try:
            _take(lab, match["id"])
        except ValueError:
            continue
        ids.append(match["id"])
    return ids


def reuse_cues(lab: Lab, cues: list[dict[str, Any]]) -> None:
    for cue in cues:
        ids = _cue_matches(lab, cue, 4)
        if ids:
            cue.update(take_id=ids[0], take_ids=ids, status="reused")


def estimate(lab: Lab, data: dict[str, Any], generator: Generator | None = None) -> dict[str, Any]:
    pending = [c for c in data["cues"] if not c.get("take_id")]
    generator = generator or load_generator(data["generator"])
    usd = sum(generator.estimate_usd(cue_spec(c)) for c in pending)
    if not math.isfinite(usd) or usd < 0:
        raise ValueError("Generator estimate must be finite and nonnegative.")
    seconds = sum(c["end"] - c["start"] for c in pending)
    credits = math.ceil(seconds * SFX_CREDITS_PER_SECOND) if data["generator"].startswith("elevenlabs") else None
    return {"need": len(pending), "reuse": len(data["cues"]) - len(pending), "credits": credits, "usd": usd,
            "over_limit": usd > defaults(lab)["max_usd"]}


def print_plan(data: dict[str, Any], emit: Emit) -> None:
    emit(cue_table(data))
    cost = data["estimate"]
    credit_text = f"{cost['credits']} credits" if cost["credits"] is not None else "credits set by adapter"
    emit(f"Reuse {cost['reuse']}; need {cost['need']}. Estimated generation: {credit_text}, ${cost['usd']:.4f}.")
    emit(f"Score: {data['id']}")
    for warning in data["analysis"].get("warnings", []):
        emit(warning)


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".source-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        os.replace(temp, destination)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def plan_score(lab: Lab, video: Path, no_agent: bool = False, agent: Agent | None = None,
               emit: Emit = print, sfx_model: str | None = None) -> dict[str, Any]:
    source = video.expanduser().resolve()
    if not source.is_file() or source.name.startswith("._"):
        raise ValueError("Choose an existing video file.")
    score_media.tool("ffmpeg")
    score_media.tool("ffprobe")
    generator = lab.config["generator"]
    if sfx_model:
        if sfx_model not in ("v2", "v3") or generator not in ("elevenlabs", "elevenlabs-v2", "elevenlabs-v3"):
            raise ValueError("--sfx-model v2/v3 requires the ElevenLabs generator.")
        generator = "elevenlabs-" + sfx_model
    ident = "score-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4)
    name = re.sub(r"[^\w .-]", "_", source.stem).strip(" .")[:80] or "edit"
    relative = f"scores/{lab.project}/{ident}"
    directory = lab.safe_path(relative)
    directory.mkdir(parents=True, exist_ok=True)
    copied = lab.safe_path(f"{relative}/source{source.suffix.lower()}")
    _copy(source, copied)
    analysis = score_media.analyse(copied, directory)
    cues = _cue_specs(lab, analysis, no_agent, agent)
    reuse_cues(lab, cues)
    outputs = {key: f"{relative}/{name}{suffix}" for key, suffix in (
        ("stem", ".sfx-stem.wav"), ("video", ".scored.mp4"), ("fcpxml", ".fcpxml"), ("edl", ".edl"),
        ("cues_json", ".cues.json"), ("cues_md", ".cues.md"))}
    data = {"version": 1, "id": ident, "project": lab.project, "name": name, "created": datetime.now().isoformat(),
            "source": str(copied.relative_to(lab.root)), "output_dir": relative, "generator": generator,
            "analysis": analysis, "cues": cues, "outputs": outputs, "state": "planned", "render_dirty": True}
    data["estimate"] = estimate(lab, data)
    save_score(lab, data)
    print_plan(data, emit)
    return data


def cost_gate(lab: Lab, usd: float, count: int, yes: bool, max_usd: float | None = None) -> None:
    cap = defaults(lab)["max_usd"] if max_usd is None else max_usd
    if not math.isfinite(cap) or cap < 0:
        raise ValueError("max-usd must be finite and nonnegative.")
    if not math.isfinite(usd) or usd < 0 or usd > cap:
        raise ValueError(f"Estimated cost ${usd:.4f} exceeds max-usd ${cap:.4f}.")
    if not count or yes:
        return
    if not sys.stdin.isatty():
        raise ValueError("Check the cue sheet and estimate, then add --yes or resume this score with --yes.")
    if input(f"Generate {count} takes for about ${usd:.4f}? [y/N] ").strip().lower() not in ("y", "yes"):
        raise ValueError("Generation cancelled. The saved cue sheet is ready to resume.")


def _recover(lab: Lab, data: dict[str, Any]) -> None:
    # A take may have landed just before the process exited. Never pay for it twice on resume.
    for cue in data["cues"]:
        if cue.get("take_id") or not cue.get("planned_take_id"):
            continue
        try:
            sound = _take(lab, cue["planned_take_id"])
        except ValueError:
            continue
        cue.update(take_id=sound["id"], take_ids=list(dict.fromkeys(cue["take_ids"] + [sound["id"]])), status="generated", error="")


def execute_score(lab: Lab, ident: str, yes: bool = False, max_usd: float | None = None,
                  emit: Emit = print, generator: Generator | None = None) -> dict[str, Any]:
    with SCORE_LOCK:
        data = load_score(lab, ident)
        _recover(lab, data)
        generator = generator or load_generator(data["generator"])
        data["estimate"] = estimate(lab, data, generator)
        save_score(lab, data)
        print_plan(data, emit)
        cost_gate(lab, data["estimate"]["usd"], data["estimate"]["need"], yes, max_usd)
        pending = [c for c in data["cues"] if not c.get("take_id")]
        if pending:
            lab.audio_dir.mkdir(parents=True, exist_ok=True)
            batch = {"batch": lab.next_batch_id(), "project": lab.project, "brief": f"Score {data['name']}", "kind": "score",
                     "generator": data["generator"], "created": datetime.now().isoformat(), "specs": [cue_spec(c) for c in pending],
                     "score_id": ident, "estimate_usd": data["estimate"]["usd"], "over_limit": False}
            write_json(lab.safe_path(f".sfx-lab/plans/{batch['batch']}.json"), batch)
            for n, cue in enumerate(pending, 1):
                cue["planned_take_id"] = f"{batch['batch']}-{n:02d}"
            data.update(state="generating", render_dirty=True)
            save_score(lab, data)
            for n, cue in enumerate(pending, 1):
                try:
                    result = generator.generate(batch["specs"][n - 1], lab.audio_dir / f"sfx-{batch['batch']}-{n:02d}")
                    if not isinstance(result, dict) or not result.get("path"):
                        raise ValueError("Generator returned no audio path.")
                    record = append_sound(lab, batch, n, result)
                    _take(lab, record["id"])
                    cue.update(take_id=record["id"], take_ids=list(dict.fromkeys(cue["take_ids"] + [record["id"]])), status="generated", error="")
                    emit(f"{cue['id']}: generated {record['id']}")
                except Exception as error:
                    cue.update(status="error", error=safe_message(str(error)))
                    append_sound(lab, batch, n, {"error": cue["error"]})
                    emit(f"{cue['id']}: {cue['error']}")
                save_score(lab, data)
        if any(not c.get("take_id") for c in data["cues"]):
            data["state"] = "error"
            save_score(lab, data)
            raise RuntimeError(f"Some takes failed. Successful takes are saved. Resume {ident} to retry only the misses.")
        data["state"] = "ready"
        save_score(lab, data)
        return render_score(lab, ident, emit)


def render_score(lab: Lab, ident: str, emit: Emit = print) -> dict[str, Any]:
    with SCORE_LOCK:
        data = load_score(lab, ident)
        if any(not c.get("take_id") for c in data["cues"]):
            raise ValueError("Generate or swap the missing takes before rendering.")
        audio = {c["take_id"]: lab.safe_path(_take(lab, c["take_id"])["audio"]) for c in data["cues"]}
        details = {ident: score_media.audio_info(path) for ident, path in audio.items()}
        for cue in data["cues"]:
            if cue["event_type"] == "voice-line":
                length = details[cue["take_id"]]["duration"]
                cue["end"] = min(data["analysis"]["duration"], cue["start"] + length)
        directory = lab.safe_path(data["output_dir"])
        with tempfile.TemporaryDirectory(prefix=".render-", dir=directory) as temp:
            stem = Path(temp) / "stem.wav"
            video = Path(temp) / "scored.mp4"
            data["mix"] = score_media.mix_stem(data["cues"], audio, data["analysis"]["duration"],
                                                data["analysis"]["voice_regions"], stem, directory)
            score_media.render_video(lab.safe_path(data["source"]), stem, data["analysis"], video)
            takes = []
            for cue in data["cues"]:
                take = _take(lab, cue["take_id"])
                takes.append({"id": take["id"], "audio": take["audio"], **details[take["id"]]})
            data["export_takes"] = takes
            xml, edl = Path(temp) / "edit.fcpxml", Path(temp) / "edit.edl"
            score_export.fcpxml(lab, data, xml)
            score_export.edl(data, edl)
            data.pop("export_takes")
            for key, path in (("stem", stem), ("video", video), ("fcpxml", xml), ("edl", edl)):
                os.replace(path, lab.safe_path(data["outputs"][key]))
        data.update(state="rendered", render_dirty=False, rendered_at=datetime.now().isoformat())
        data["estimate"] = estimate(lab, data)
        save_score(lab, data)
        emit(f"Scored video: {lab.safe_path(data['outputs']['video'])}\nSFX stem: {lab.safe_path(data['outputs']['stem'])}")
        return data


def cue_by_id(data: dict[str, Any], ident: str) -> dict[str, Any]:
    cue = next((c for c in data["cues"] if c["id"] == ident), None)
    if cue is None:
        raise ValueError("Cue not found.")
    return cue


def more_takes(lab: Lab, ident: str, cue_id: str) -> dict[str, Any]:
    with SCORE_LOCK:
        data = load_score(lab, ident)
        cue = cue_by_id(data, cue_id)
        existing = set(cue["take_ids"])
        new = []
        for take_id in _cue_matches(lab, cue, None):
            if take_id not in existing:
                new.append(take_id)
                existing.add(take_id)
        if new:
            cue["take_ids"].extend(new)
            save_score(lab, data)
        return {"added": len(new), "score": data}


def swap_take(lab: Lab, ident: str, cue_id: str, take_id: str) -> dict[str, Any]:
    with SCORE_LOCK:
        data = load_score(lab, ident)
        cue = cue_by_id(data, cue_id)
        sound = _take(lab, take_id)
        if (cue["event_type"] == "voice-line") != (sound["spec"].get("kind") == "voice-line"):
            raise ValueError("Swap voice lines with voice takes, and effects with effect takes.")
        cue.update(take_id=take_id, take_ids=list(dict.fromkeys(cue["take_ids"] + [take_id])), status="swapped", error="")
        data["render_dirty"] = True
        save_score(lab, data)
        return data


def add_line(lab: Lab, text: str, voice: str, at: str, ident: str | None = None, yes: bool = False,
             max_usd: float | None = None, emit: Emit = print, generator: Generator | None = None) -> dict[str, Any]:
    with SCORE_LOCK:
        data = load_score(lab, ident)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 10000:
            raise ValueError("Use 1–10000 characters of speech.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice):
            raise ValueError("Provide an existing ElevenLabs voice id.")
        if any(not c.get("take_id") for c in data["cues"]):
            raise ValueError("Finish the score's missing takes before adding a line.")
        if len(data["cues"]) >= 999:
            raise ValueError("CMX3600 supports at most 999 cues. Split this edit before adding another line.")
        start = score_export.parse_timecode(at, data["analysis"]["fps"])
        fps = Fraction(data["analysis"]["fps"])
        start = float(score_export.frames(start, str(fps)) / fps)
        if start >= data["analysis"]["duration"]:
            raise ValueError("The voice line must start inside the edit.")
        generator = generator or load_generator("elevenlabs-tts")
        cue = {"id": f"cue-{len(data['cues']) + 1:03d}", "start": start, "end": data["analysis"]["duration"],
               "event_type": "voice-line", "prompt": text.strip(), "voice_id": voice, "prompt_influence": .3,
               "loop": False, "take_ids": [], "take_id": None, "status": "need", "error": ""}
        spec = cue_spec(cue)
        cost = generator.estimate_usd(spec)
        emit(cue_table({"cues": [cue]}))
        emit(f"Estimated speech: {len(cue['prompt'])} text characters (credits depend on voice/plan), ${cost:.4f}.")
        cost_gate(lab, cost, 1, yes, max_usd)
        batch = {"batch": lab.next_batch_id(), "project": lab.project, "brief": f"Voice line for {data['name']}", "kind": "voice-line",
                 "generator": "elevenlabs-tts", "created": datetime.now().isoformat(), "specs": [spec], "score_id": data["id"],
                 "estimate_usd": cost, "over_limit": False}
        write_json(lab.safe_path(f".sfx-lab/plans/{batch['batch']}.json"), batch)
        try:
            result = generator.generate(spec, lab.audio_dir / f"sfx-{batch['batch']}-01")
            record = append_sound(lab, batch, 1, result)
        except Exception as error:
            append_sound(lab, batch, 1, {"error": safe_message(str(error))})
            raise
        path = lab.safe_path(_take(lab, record["id"])["audio"])
        cue.update(take_id=record["id"], take_ids=[record["id"]], status="generated")
        data["cues"].append(cue)
        data["cues"].sort(key=lambda c: (c["start"], c["id"]))
        data["render_dirty"] = True
        save_score(lab, data)
        duration = score_media.audio_info(path)["duration"]
        if start + duration > data["analysis"]["duration"] + 1e-6:
            emit("The voice line runs beyond the edit and is trimmed at picture end.")
        return render_score(lab, data["id"], emit)
