"""Plan, generate, and learn orchestration independent of argparse."""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .agents import Agent, load_agent, safe_message
from .catalogue import append_sound, read_catalogue, sync_catalogue
from .context import find, hot_card, normalize_hot, row, show, taste_slice, words
from .projects import defaults, get_project, project_path
from .tags import tags_for
from .taste import Taste
from .generators import load_generator
from .lab import Lab, read_text, write_json
from .prompts import learn_prompt, plan_prompt
from .ratings import (LOG_LOCK, Event, append_events, examples, is_new, latest,
                      parse_rating, read_log, stats_markdown)
from .sheet import Item, mark_learned, parse_sheet, write_sheet
from .spec import Spec
from .taste import agent_part, clean_agent_markdown, read_taste, write_auto

Emit = Callable[[str], None]


def _warn(message: str) -> None:
    print(safe_message("Warning: " + message), file=sys.stderr)


def _json_array(text: str) -> list[Any]:
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise ValueError("Agent must return a JSON array.")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array of specs.")
    return value


def _recent_prompts(lab: Lab, brief: str) -> list[str]:
    prompts: list[str] = []
    tags = set(tags_for(brief))
    for path in reversed(lab.plans()):
        data = json.loads(read_text(path))
        if data.get("project", "general") != lab.project:
            continue
        for spec in reversed(data["specs"]):
            if tags & set(tags_for(spec["prompt"])):
                prompts.append(spec["prompt"])
                if len(prompts) == 10:
                    return prompts
    return prompts


def _bad_repeat(prompt: str, bad: list[Event]) -> bool:
    proposed = words(prompt)
    return any(len(proposed & (other := words(e["spec"]["prompt"]))) / max(1, len(proposed | other)) >= .6
               for e in bad)


def plan(lab: Lab, brief: str | None = None, n: int | None = None, kind: str | None = None,
         specs_file: Path | None = None, agent: Agent | None = None,
         emit: Emit = print, from_id: str | None = None,
         progress: Callable[[dict[str, Any]], None] | None = None,
         verbose: bool = False, feedback: str | None = None) -> dict[str, Any]:
    parent = show(lab, from_id) if from_id else None
    if feedback is not None and parent is None:
        raise ValueError("--feedback requires --from <id>.")
    brief = brief or (parent["brief"] if parent else None)
    if not brief:
        raise ValueError("Provide a brief or use --from <id>.")
    settings = defaults(lab)
    kind = kind or settings["kind"]
    bad = [e for e in latest(read_log(lab.ratings_path), lab.project).values()
           if e.get("stars") and e["stars"] <= 2]
    count = settings["batch_size"] if n is None else n
    if count < 1:
        raise ValueError("Batch size must be at least 1.")
    taste = read_taste(lab.taste_path)
    good = find(lab, brief, min_stars=4, limit=5)
    overlapping_bad = [e for e in bad if set(tags_for(brief)) & set(tags_for(e["spec"]["prompt"]))][:5]
    prompt = plan_prompt(Taste(taste_slice(lab), taste.notes), "",
                         _recent_prompts(lab, brief), brief, kind, count, settings["explore"],
                         hot_card(lab), "\n".join(row(e) for e in good),
                         "\n".join(row(e) + "\nFull prompt: " + e["spec"]["prompt"] for e in overlapping_bad))
    prompt += "\nProject defaults: " + json.dumps(settings)
    if parent:
        rating = parent["ratings"].get(lab.project, {})
        notes_path = lab.safe_path(".sfx-lab/notes.json")
        notes = json.loads(read_text(notes_path)) if notes_path.exists() else {}
        note = rating.get("note", notes.get(f"{lab.project}:{from_id}",
                          notes.get(from_id, "") if lab.project == "general" else ""))
        prompt += ("\n\n## Build on this sound\n" + json.dumps({
            "id": from_id, "prompt": parent["spec"]["prompt"], "settings": parent["spec"],
            "stars": rating.get("stars"), "note": note}, ensure_ascii=False))
        if feedback is not None:
            prompt += f"\nFeedback: {feedback}. Keep what works, change only what the feedback asks."
    if specs_file is not None:
        entries = json.loads(read_text(specs_file))
        if not isinstance(entries, list):
            raise ValueError("Specs file must contain a JSON array.")
    else:
        agent = agent or load_agent(lab.config["agent"], lab.config["model"])
        if verbose:
            emit(f"Plan prompt: {len(prompt)} characters (~{len(prompt) / 4:.0f} tokens)")
        try:
            entries = _json_array(agent.ask(prompt))
        except ValueError:
            entries = _json_array(agent.ask(prompt + "\nReply with only the JSON array."))
    specs = [spec.to_dict() for entry in entries
             if (spec := Spec.from_dict(entry, kind)) is not None][:count]
    passing = [spec for spec in specs if not _bad_repeat(spec["prompt"], bad)]
    missing = len(specs) - len(passing)
    if missing:
        agent = agent or load_agent(lab.config["agent"], lab.config["model"])
        replacement_text = agent.ask(prompt + f"\nReplace {missing} rejected bad repeats. Reply with exactly {missing} new specs; avoid the bad prompts above and these rejected prompts: " + json.dumps([s["prompt"] for s in specs if _bad_repeat(s["prompt"], bad)]))
        try:
            replacements = _json_array(replacement_text)
        except ValueError:
            replacements = []
        passing.extend(spec.to_dict() for entry in replacements[:missing]
                       if (spec := Spec.from_dict(entry, kind)) is not None and not _bad_repeat(spec.prompt, bad))
    specs = passing[:count]
    experiments_left = round(len(specs) * settings["explore"])
    for spec in specs:
        if spec["explore"]:
            spec["explore"] = experiments_left > 0
            experiments_left = max(0, experiments_left - 1)
        lo, hi = settings["influence"]
        spec["prompt_influence"] = min(hi, max(lo, spec["prompt_influence"]))
        if settings["length"] is not None:
            lo, hi = settings["length"]
            spec["duration_s"] = min(hi, max(lo, spec["duration_s"] if spec["duration_s"] is not None else lo))
    if not specs:
        raise ValueError("No valid sound specs were returned.")
    if from_id:
        if Spec.from_dict({"prompt": "validate", "from": from_id}) is None:
            raise ValueError("Invalid parent sound identifier.")
        for spec in specs:
            spec["from"] = from_id
    generator = load_generator(lab.config["generator"])
    estimate = sum(generator.estimate_usd(spec) for spec in specs)
    data = {"project": lab.project, "batch": lab.next_batch_id(), "brief": brief, "kind": kind,
            "generator": lab.config["generator"], "created": datetime.now().isoformat(),
            "specs": specs, "estimate_usd": estimate, "over_limit": estimate > settings["max_usd"]}
    write_json(lab.safe_path(f".sfx-lab/plans/{data['batch']}.json"), data)
    for n, spec in enumerate(specs, 1):
        duration = spec["duration_s"]
        length = "auto" if duration is None else f"{duration:g} s"
        experiment = "  [exp]" if spec["explore"] else ""
        emit(f"{n:02d}  {length} · infl {spec['prompt_influence']:g} · loop "
             f"{'on' if spec['loop'] else 'off'}{experiment}  {spec['prompt']}")
    emit(f"Estimated cost: ${estimate:.3f} ({data['generator']})")
    if progress:
        progress(data)
    return data


def _sheet_batches(lab: Lab) -> set[str]:
    batches: set[str] = set()
    for path in lab.sheets():
        match = re.search(r'(?m)^batch:\s*"?([^"\r\n]+)"?\s*$', read_text(path))
        if match:
            batches.add(match[1].strip())
        batches.update(item.metadata["batch"] for item in parse_sheet(path))
    batches.update(record["batch"] for record in read_catalogue(lab))
    return batches


def pending_plans(lab: Lab) -> list[Path]:
    batches = _sheet_batches(lab)
    return [path for path in lab.plans() if path.stem not in batches and json.loads(read_text(path)).get("project", "general") == lab.project]


def _get_plan(lab: Lab, batch: str | None) -> dict[str, Any]:
    if batch:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d+", batch):
            raise ValueError("Invalid batch identifier.")
        if batch in _sheet_batches(lab):
            raise ValueError(f"Batch {batch} already has a sheet.")
        path = lab.safe_path(f".sfx-lab/plans/{batch}.json")
    else:
        paths = pending_plans(lab)
        if not paths:
            raise ValueError("No plans without sheets. Run sfx-lab plan first.")
        path = paths[-1]
    data = json.loads(read_text(path))
    if data.get("batch") != path.stem or not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d+", data.get("batch", "")):
        raise ValueError("Invalid batch identifier in saved plan.")
    if data.get("project", "general") != lab.project:
        raise ValueError("Saved plan belongs to another project.")
    return data


def run(lab: Lab, batch: str | None = None, yes: bool = False,
        max_usd: float | None = None, emit: Emit = print,
        progress: Callable[[dict[str, Any]], None] | None = None) -> Path:
    data = _get_plan(lab, batch)
    generator = load_generator(data["generator"])
    estimate = sum(generator.estimate_usd(spec) for spec in data["specs"])
    cap = defaults(lab)["max_usd"] if max_usd is None else max_usd
    if estimate > cap:
        raise ValueError(f"Estimated cost ${estimate:.3f} exceeds max-usd ${cap:.3f}.")
    if not yes:
        if not sys.stdin.isatty():
            raise ValueError("Check the estimate, then add --yes.")
        reply = input(f"Generate {len(data['specs'])} sounds for about ${estimate:.3f}? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            raise ValueError("Generation cancelled.")
    lab.audio_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = [{} for _ in data["specs"]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(generator.generate, spec,
                            lab.audio_dir / f"sfx-{data['batch']}-{n:02d}"): n
                for n, spec in enumerate(data["specs"], 1)}
        for job in as_completed(jobs):
            n = jobs[job]
            try:
                result = job.result()
                if not isinstance(result, dict) or not result.get("path"):
                    raise ValueError("Generator returned no audio path.")
                path = Path(result["path"]).resolve()
                if not path.is_relative_to(lab.audio_dir):
                    raise ValueError("Generator audio must stay inside the lab audio directory.")
                emit(f"{n:02d}  Generated {result['path']}")
            except Exception as error:
                result = {"error": safe_message(str(error))}
                emit(f"{n:02d}  Failed: {result['error']}")
            results[n - 1] = result
            record = append_sound(lab, data, n, result)
            if progress:
                progress(record)
    path = (write_sheet(lab, data, results) if lab.config["sheets"]
            else lab.safe_path(".sfx-lab/sounds.jsonl"))
    emit(str(path))
    return path


def _rating_event(item: Item, previous: Event | None, rating: int) -> Event:
    meta = item.metadata
    return {"project": meta.get("project", "general"), "ts": time.time(), "batch": meta["batch"], "n": meta["n"],
            "ref": meta.get("ref", ""), "generator": meta["generator"],
            "brief": meta.get("brief", ""), "spec": meta["spec"], "rating": str(rating),
            "stars": rating, "score": rating, "note": item.note,
            "settled": previous.get("settled", False) if previous else False, "source": "sheet"}


def _learn_profile(lab: Lab, prompt: str, agent: Agent | None) -> str:
    retry = "Ratings are logged; the next sfx-lab learn retries the taste."
    try:
        agent = agent or load_agent(lab.config["agent"], lab.config["model"])
        profile = clean_agent_markdown(agent.ask(prompt))
    except Exception as error:
        raise RuntimeError(f"{error}. {retry}") from error
    if not all(re.search(rf"(?m)^## {heading}\s*$", profile) for heading in ("Likes", "Dislikes")):
        raise ValueError(f"Agent reply must contain ## Likes and ## Dislikes. {retry}")
    return normalize_hot(profile)


def learn(lab: Lab, no_agent: bool = False, agent: Agent | None = None,
          emit: Emit = print,
          progress: Callable[[dict[str, Any]], None] | None = None) -> list[Event]:
    with LOG_LOCK:
        sync_catalogue(lab)
        log = read_log(lab.ratings_path)
        current = latest(log, lab.project)
        changes: list[Event] = []
        new_count = unrated = 0
        for path in lab.sheets():
            items = parse_sheet(path)
            if not any(item.metadata.get("project", "general") == lab.project for item in items):
                continue
            for item in items:
                meta = item.metadata
                if meta.get("project", "general") != lab.project:
                    continue
                try:
                    rating = parse_rating(item.rating)
                except ValueError as error:
                    _warn(f"{meta['batch']} #{meta['n']}: {error}; skipped")
                    continue
                if rating is None:
                    unrated += 1
                    continue
                key = (meta["batch"], meta["n"])
                previous = current.get(key)
                if is_new(previous, rating, item.note) and (previous is None or path.stat().st_mtime > previous["ts"]):
                    event = _rating_event(item, previous, rating)
                    changes.append(event)
                    new_count += previous is None
                    current[key] = event
            mark_learned(path, items)
        # Keep the latest rating stable while making the one-time settlement call.
        settlements = []
        settled_count = kept = 0
        changed_keys = {(e["batch"], e["n"]) for e in changes}
        owners = {(s["batch"], s["n"]): s.get("project", "general") for s in read_catalogue(lab)}
        for key, event in current.items():
            if owners.get(key, event.get("project", "general")) != lab.project:
                continue
            if event.get("stars") is None or event.get("settled"):
                continue
            try:
                load_generator(event["generator"]).settle(event.get("ref", ""), keep=event["stars"] > 1)
            except Exception as error:
                _warn(f"{event['batch']} #{event['n']}: could not settle: {error}")
                continue
            settled_count += 1
            kept += event["stars"] > 1
            if key in changed_keys:
                event["settled"] = True
            else:
                settled = event | {"settled": True}
                settlements.append(settled)
        append_events(lab.ratings_path, changes + settlements)
        log.extend(changes + settlements)
        current = latest(log, lab.project)
        snapshot_ts = time.time()
    state_path = project_path(lab, lab.project)
    state = get_project(lab)
    undistilled = [e for e in latest(log[state.get("distilled_events", 0):], lab.project).values() if e.get("stars")]
    rated = {key: event for key, event in current.items() if event.get("stars")}
    taste = read_taste(lab.taste_path)
    best, worst = examples(rated)
    numbers = stats_markdown(rated)
    profile = agent_part(taste.auto)
    if undistilled and not no_agent:
        try:
            profile = _learn_profile(lab, learn_prompt(taste, numbers, undistilled, best, worst), agent)
        except Exception:
            with LOG_LOCK:
                write_auto(lab.taste_path, normalize_hot(profile), best, worst, numbers, len(rated))
            raise
    with LOG_LOCK:
        write_auto(lab.taste_path, normalize_hot(profile), best, worst, numbers, len(rated))
    if undistilled and not no_agent:
        with LOG_LOCK:
            write_json(state_path, get_project(lab) | {"distilled_events": len(log), "last_learn_ts": snapshot_ts})
    hot_card(lab)
    if not changes and not undistilled and not settled_count:
        if not lab.config["sheets"]:
            unrated = sum(not e.get("failed") and not current.get((e["batch"], e["n"]), {}).get("stars")
                          for e in read_catalogue(lab))
            emit(f"No new ratings. {unrated} sounds are unrated: rate them in sfx-lab studio." if unrated else "No new ratings.")
            return changes
        # Nothing rated since the last learn: say so instead of claiming an update.
        emit(f"No new ratings. {unrated} sounds are unrated: fill in rating:: in the batch "
             "sheet, save it, then run sfx-lab learn again." if unrated else "No new ratings.")
        return changes
    result = (f"Numbers refreshed; {len(undistilled)} ratings wait for the agent (run sfx-lab learn)."
              if no_agent and undistilled else "Taste updated.")
    settlement_summary = (f" Settled {settled_count} (kept {kept}, discarded {settled_count - kept})."
                          if settled_count else "")
    emit(f"Logged {len(changes)} ratings ({new_count} new).{settlement_summary} {result}")
    if progress:
        progress({"progress": 1})
    return changes


def status(lab: Lab) -> str:
    catalogue = sync_catalogue(lab)
    model = lab.config["model"] or (
        "haiku" if lab.config["agent"] == "claude-cli" else "claude-haiku-5-5")
    pending = pending_plans(lab)
    lines = [f"Lab: {lab.root}", f"Taste: {lab.taste_path}",
             f"Generator: {lab.config['generator']}", f"Agent/model: {lab.config['agent']} / {model}",
             f"Plans without sheets: {len(pending)}"]
    lines.extend(f"  {path.stem}" for path in pending)
    lines.append("Open sheets:")
    opened = 0
    for path in lab.sheets():
        text = read_text(path)
        if not any(item.metadata.get("project", "general") == lab.project for item in parse_sheet(path)):
            continue
        if re.search(r'(?m)^state:\s*"?learned"?\s*$', text.split("---", 2)[1] if text.startswith("---") else ""):
            continue
        unrated = 0
        for item in parse_sheet(path):
            try:
                unrated += parse_rating(item.rating) is None
            except ValueError:
                unrated += 1
        lines.append(f"  {path.name}: {unrated} unrated")
        opened += 1
    if not opened:
        lines.append("  none")
    lines.append(f"Total rated: {sum(bool(e.get('stars')) for e in latest(read_log(lab.ratings_path), lab.project).values())}")
    lines.append(f"Catalogue: {len(catalogue)} sounds")
    return "\n".join(lines)


def need(lab: Lab, brief: str, n: int = 3, generate: bool = False,
         emit: Emit = print, agent: Agent | None = None) -> dict[str, Any] | None:
    if n < 1:
        raise ValueError("Count must be at least 1.")
    matches = find(lab, brief, min_stars=4, limit=min(100, n))
    reuse = len(matches) >= n and not generate
    for sound in matches if reuse else find(lab, brief):
        emit(row(sound))
    if reuse:
        emit(f"Reusing {n} rated sounds. Add --generate for new ones.")
        return None
    return plan(lab, brief, n if generate else n - len(matches), agent=agent, emit=emit)
