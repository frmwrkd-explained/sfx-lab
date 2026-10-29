"""Command-line entry point."""

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from . import __version__
from .agents import safe_message
from .lab import init_lab, load_env, locate_lab
from .run import learn, plan, run, status, need
from .projects import create_project, get_project, list_projects, select_project, use_project
from .context import find, hot_card, row, show, taste_slice
from .ratings import latest, read_log
from .tags import tags_for


def _planning_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("brief", metavar="BRIEF", nargs="?")
    parser.add_argument("--from", dest="from_id", metavar="ID")
    parser.add_argument("--feedback", metavar="TEXT")
    parser.add_argument("-n", type=int, metavar="N")
    parser.add_argument("--kind", metavar="KIND")
    parser.add_argument("--specs", type=Path, metavar="FILE")
    parser.add_argument("--verbose", action="store_true")


def _run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true", help="Accept the generation estimate")
    parser.add_argument("--max-usd", type=float, metavar="X")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="sfx-lab", description="Learn your sound-effect taste from Markdown ratings.")
    result.add_argument("--lab", metavar="DIR", help="Lab folder (otherwise discover it)")
    result.add_argument("--project", metavar="SLUG")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create or fill in a lab")
    init.add_argument("directory", nargs="?", default="./sfx-lab", metavar="DIR")
    init.add_argument("--generator", metavar="NAME")
    init.add_argument("--taste-file", metavar="NAME")
    init.add_argument("--embed", choices=("obsidian", "html"))
    init.add_argument("--python", metavar="PATH",
                      help="Interpreter the desktop app uses for this lab (one that can import its generator)")
    _planning_options(commands.add_parser("plan", help="Propose and save sound specs"))
    generate = commands.add_parser("run", help="Generate a saved plan")
    generate.add_argument("batch", nargs="?", metavar="BATCH")
    _run_options(generate)
    batch = commands.add_parser("batch", help="Plan and generate a batch")
    _planning_options(batch)
    _run_options(batch)
    learning = commands.add_parser("learn", help="Read ratings and update taste")
    learning.add_argument("--no-agent", action="store_true", help="Refresh examples and numbers only")
    commands.add_parser("status", help="Show plans, open sheets, and rating totals")
    studio = commands.add_parser("studio", help="Open the local sound Studio")
    studio.add_argument("--port", type=int, default=8765)
    studio.add_argument("--no-open", action="store_true")
    studio.add_argument("--ready-json", action="store_true")
    opening = commands.add_parser("open", help="Open or start SFX Studio in the background")
    opening.add_argument("--project", dest="open_project", metavar="SLUG")
    commands.add_parser("stop", help="Stop the background SFX Studio")
    projects = commands.add_parser("project").add_subparsers(dest="project_command", required=True)
    projects.add_parser("list")
    new = projects.add_parser("new")
    new.add_argument("name")
    new.add_argument("--from", dest="from_slug")
    new.add_argument("--description", default="")
    detail = projects.add_parser("show")
    detail.add_argument("slug", nargs="?")
    use = commands.add_parser("use")
    use.add_argument("slug")
    context = commands.add_parser("context")
    context.add_argument("--project", dest="context_project")
    search = commands.add_parser("find")
    search.add_argument("need")
    search.add_argument("--min-stars", type=int, choices=range(1, 6))
    search.add_argument("--all-projects", action="store_true")
    search.add_argument("--include-bad", action="store_true")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--json", action="store_true")
    detail = commands.add_parser("show")
    detail.add_argument("id")
    detail.add_argument("--json", action="store_true")
    taste = commands.add_parser("taste")
    taste.add_argument("--full", action="store_true")
    taste.add_argument("--json", action="store_true")
    ratings = commands.add_parser("ratings")
    ratings.add_argument("--tag")
    ratings.add_argument("--stars")
    ratings.add_argument("--limit", type=int, default=20)
    ratings.add_argument("--json", action="store_true")
    reuse = commands.add_parser("need")
    reuse.add_argument("brief")
    reuse.add_argument("-n", type=int, default=3)
    reuse.add_argument("--generate", action="store_true")
    score = commands.add_parser("score", help="Sound-design an edit and export a stem, video and cue markers")
    score.add_argument("video", type=Path, nargs="?")
    score.add_argument("--project", dest="score_project", metavar="SLUG")
    score.add_argument("--no-agent", action="store_true")
    score.add_argument("--plan-only", action="store_true", help="Save the cue sheet and estimate without generating")
    score.add_argument("--resume", metavar="SCORE_ID", help="Run the saved cue sheet without replanning")
    score.add_argument("--sfx-model", choices=("v3", "v2"))
    _run_options(score)
    line = commands.add_parser("line", help="Add speech from an existing ElevenLabs voice to the latest score")
    line.add_argument("text")
    line.add_argument("--voice", required=True, metavar="VOICE_ID")
    line.add_argument("--at", required=True, metavar="TIMECODE")
    line.add_argument("--project", dest="score_project", metavar="SLUG")
    line.add_argument("--score", dest="score_id", metavar="SCORE_ID")
    _run_options(line)
    return result


def _emit(message: str) -> None:
    print(safe_message(message))


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.command == "init":
            lab = init_lab(args.directory, generator=args.generator,
                           taste_file=args.taste_file, embed=args.embed,
                           python=str(Path(args.python).expanduser().absolute()) if args.python else None)
            _emit(f"Lab: {lab.root}\nConfig: {lab.internal / 'config.json'}\nTaste: {lab.taste_path}\nBatches: {lab.batches_dir}\nAudio: {lab.audio_dir}")
            return 0
        lab = locate_lab(args.lab)
        load_env(lab)
        lab = select_project(lab, getattr(args, "context_project", None) or getattr(args, "score_project", None) or args.project)
        if args.command in ("plan", "batch"):
            data = plan(lab, args.brief, args.n, args.kind, args.specs, emit=_emit,
                        verbose=args.verbose, from_id=args.from_id, feedback=args.feedback)
            if args.command == "batch":
                run(lab, data["batch"], args.yes, args.max_usd, emit=_emit)
        elif args.command == "run":
            run(lab, args.batch, args.yes, args.max_usd, emit=_emit)
        elif args.command == "learn":
            learn(lab, args.no_agent, emit=_emit)
        elif args.command == "studio":
            from .studio import serve
            serve(lab, args.port, args.no_open, args.ready_json)
        elif args.command == "open":
            from .studio_process import open_studio
            if slug := args.open_project or args.project:
                lab = use_project(lab, slug)
            _emit(f"SFX Studio is open: {open_studio(lab)}")
        elif args.command == "stop":
            from .studio_process import stop_studio
            _emit("SFX Studio stopped." if stop_studio(lab) else "SFX Studio is not running.")
        elif args.command == "project":
            if args.project_command == "new":
                data = create_project(lab, args.name, args.from_slug, args.description)
                _emit(f"Created project {data['slug']}: {data['name']}")
            elif args.project_command == "show":
                _emit(json.dumps(get_project(lab, args.slug), ensure_ascii=False, indent=2))
            else:
                for item in list_projects(lab):
                    avg = f"{item['avg_stars']:.2f}★" if item['avg_stars'] is not None else "—★"
                    _emit(f"{item['slug']}  {item['name']}  {item['rated']} rated  {avg}  last batch {item['last_batch'] or '—'}")
        elif args.command == "use":
            lab = use_project(lab, args.slug)
            _emit(f"Using {lab.project}")
        elif args.command == "context":
            _emit(hot_card(lab))
        elif args.command == "find":
            rows = find(lab, args.need, args.min_stars, args.all_projects, args.include_bad, args.limit)
            _emit(json.dumps(rows, ensure_ascii=False) if args.json else "\n".join(map(row, rows)) or "No matching sounds.")
        elif args.command == "show":
            sound = show(lab, args.id)
            if args.json:
                _emit(json.dumps(sound, ensure_ascii=False, indent=2))
            else:
                _emit(f"{sound['id']} · {sound['brief']}\nPrompt: {sound['spec']['prompt']}\nSettings: {json.dumps(sound['spec'], ensure_ascii=False)}\nFile: {sound['audio']}\nGenerator: {sound['generator']} · ref {sound['ref']}\nLineage: from {sound['parent']} · children {', '.join(sound['children']) or 'none'}")
                for slug, event in sound['ratings'].items():
                    _emit(f"{slug}: {event.get('stars') or 'unrated'}★ — {event.get('note', '')}")
        elif args.command == "taste":
            value = taste_slice(lab, args.full)
            _emit(json.dumps({"project": lab.project, "taste": value}) if args.json else value)
        elif args.command == "ratings":
            if not 1 <= args.limit <= 100:
                raise ValueError("Limit must be 1–100.")
            stars = set(range(1, 6))
            if args.stars:
                import re
                if not re.fullmatch(r"[1-5](?:-[1-5])?", args.stars):
                    raise ValueError("Stars must be 1–5 or a range such as 1-2.")
                ends = list(map(int, args.stars.split("-")))
                stars = set(range(ends[0], ends[-1] + 1))
            rows = [e for e in latest(read_log(lab.ratings_path), lab.project).values()
                    if e.get("stars") in stars and (not args.tag or args.tag in tags_for(e['spec']['prompt']))]
            rows = sorted(rows, key=lambda e: e['ts'], reverse=True)[:args.limit]
            _emit(json.dumps(rows, ensure_ascii=False) if args.json else "\n".join(map(row, rows)) or "No matching ratings.")
        elif args.command == "need":
            need(lab, args.brief, args.n, args.generate, emit=_emit)
        elif args.command == "status":
            _emit(status(lab))
        elif args.command == "score":
            from .score import execute_score, load_score, plan_score, print_plan
            if args.resume:
                if args.video or args.no_agent or args.sfx_model:
                    raise ValueError("--resume uses the saved video, cues and model; omit video, --no-agent and --sfx-model.")
                data = load_score(lab, args.resume)
                if args.plan_only:
                    print_plan(data, _emit)
            elif args.video:
                data = plan_score(lab, args.video, args.no_agent, emit=_emit, sfx_model=args.sfx_model)
            else:
                raise ValueError("Choose a video or use --resume <score-id>.")
            if not args.plan_only:
                execute_score(lab, data["id"], args.yes, args.max_usd, emit=_emit)
        elif args.command == "line":
            from .score import add_line
            add_line(lab, args.text, args.voice, args.at, args.score_id, args.yes, args.max_usd, emit=_emit)
        return 0
    except SystemExit as error:
        return 0 if error.code == 0 else 1
    except Exception as error:
        if os.environ.get("SFX_LAB_DEBUG") == "1":
            print(safe_message(traceback.format_exc()), file=sys.stderr)
        else:
            print(safe_message(f"Error: {error}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
