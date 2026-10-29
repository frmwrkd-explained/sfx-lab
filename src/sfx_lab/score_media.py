"""Local edit analysis and sample-aligned stereo mixing through optional ffmpeg tools."""

import array
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from collections import OrderedDict, deque
from fractions import Fraction
from pathlib import Path
from typing import Any

from .agents import safe_message

RATE = 48000
PEAK = 10 ** (-3 / 20)
DUCK = 10 ** (-9 / 20)
RAMP = 0.080


def tool(name: str) -> str:
    binary = shutil.which(name)
    if not binary:
        raise RuntimeError(f"Install {name} to analyse and render edits.")
    return binary


def command(name: str, args: list[str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([tool(name), *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{name} failed: {safe_message(result.stderr[-1500:])}")
    return result


def probe(path: Path) -> dict[str, Any]:
    data = json.loads(command("ffprobe", ["-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]).stdout)
    video = next((s for s in data.get("streams", []) if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("Choose a video with a picture stream.")
    duration = float(video.get("duration") or data.get("format", {}).get("duration", 0))
    rate = video.get("avg_frame_rate", "0/0")
    if rate in ("0/0", "0"):
        rate = video.get("r_frame_rate", "0/0")
    try:
        fps = Fraction(rate)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError("Video has no usable frame rate.") from error
    if not math.isfinite(duration) or duration < .5 or not 0 < fps <= 240:
        raise ValueError("Use a video at least 0.5 seconds long, with a frame rate up to 240 fps.")
    return {"duration": duration, "fps": str(fps), "width": video["width"], "height": video["height"],
            "audio_streams": [s["index"] for s in data["streams"] if s["codec_type"] == "audio"],
            "audio_offsets": {str(s["index"]): float(s.get("start_time", 0)) - float(video.get("start_time", 0))
                              for s in data["streams"] if s["codec_type"] == "audio"},
            "audio_channels": sum(s.get("channels", 1) for s in data["streams"] if s["codec_type"] == "audio"),
            "audio_rate": next((s.get("sample_rate", "48000") for s in data["streams"] if s["codec_type"] == "audio"), "48000"),
            "video_stream": video["index"]}


def scene_cuts(path: Path, duration: float) -> list[float]:
    result = command("ffmpeg", ["-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0", "-vf",
                              "setpts=PTS-STARTPTS,select='gt(scene,0.3)',showinfo", "-an", "-f", "null", "-"])
    return sorted({float(t) for t in re.findall(r"pts_time:([0-9.]+)", result.stderr)
                   if 0 < float(t) < duration})


def motion_hits(path: Path, fps: float, cuts: list[float]) -> list[float]:
    # A 250 ms rolling baseline distinguishes brief graphic changes from sustained movement.
    result = command("ffmpeg", ["-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0", "-an", "-vf",
                              "setpts=PTS-STARTPTS,scale=64:36,tblend=all_mode=difference,signalstats,"
                              "metadata=mode=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"])
    history: deque[float] = deque(maxlen=max(3, round(fps * .25)))
    hits: list[float] = []
    pattern = r"pts_time:([0-9.]+)[^\n]*\n(?:[^\n]*\n)*?lavfi\.signalstats\.YAVG=([0-9.]+)"
    for match in re.finditer(pattern, result.stdout):
        at, difference = map(float, match.groups())
        baseline = sum(history) / len(history) if history else difference
        if (len(history) >= 3 and difference > max(12, baseline * 2.8) and
                not any(abs(at - cut) < .2 for cut in cuts) and (not hits or at - hits[-1] >= .5)):
            hits.append(at)
        history.append(difference)
    return hits


def original_audio_filters(info: dict[str, Any]) -> tuple[list[str], list[str]]:
    filters, labels = [], []
    for n, stream in enumerate(info["audio_streams"]):
        offset = info.get("audio_offsets", {}).get(str(stream), 0)
        filters.append(f"[0:{stream}]asetpts=PTS-STARTPTS+({offset})/TB,"
                       f"aresample={RATE}:async=1:first_pts=0,aformat=channel_layouts=stereo[a{n}]")
        labels.append(f"[a{n}]")
    return filters, labels


def transcript(path: Path, directory: Path, has_audio: bool,
               info: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str]:
    if not has_audio:
        return [], "No original audio; no voice regions."
    if not shutil.which("whisper"):
        return [], "Whisper is not installed; original voice ducking is unavailable."
    # Never let the optional CLI download a model implicitly, including during offline tests.
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "whisper/tiny.pt"
    model = Path(os.environ.get("SFX_LAB_WHISPER_MODEL", str(cache))).expanduser()
    if not model.is_file():
        return [], "Whisper has no cached tiny model; original voice ducking is unavailable."
    with tempfile.TemporaryDirectory(prefix=".transcript-", dir=directory) as temp:
        try:
            audio = Path(temp) / "speech.wav"
            args = ["-nostdin", "-v", "error", "-i", str(path)]
            if info:
                filters, labels = original_audio_filters(info)
                filters.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0[speech]")
                args += ["-filter_complex", ";".join(filters), "-map", "[speech]", "-t", str(info["duration"])]
            command("ffmpeg", [*args, "-vn", "-ac", "1", "-ar", "16000", str(audio)])
            command("whisper", [str(audio), "--model", str(model), "--output_format", "json", "--output_dir", temp,
                                "--device", "cpu", "--fp16", "False"], timeout=3600)
            data = json.loads((Path(temp) / "speech.json").read_text(encoding="utf-8"))
            segments = [{"start": float(s["start"]), "end": float(s["end"]), "text": str(s.get("text", ""))[:1000]}
                        for s in data.get("segments", [])
                        if float(s.get("no_speech_prob", 0)) < .6 and
                        math.isfinite(float(s["start"])) and math.isfinite(float(s["end"])) and
                        0 <= float(s["start"]) < float(s["end"])]
            return segments, ""
        except (RuntimeError, ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
            return [], "Local Whisper transcription failed; original voice ducking is unavailable."


def analyse(path: Path, directory: Path) -> dict[str, Any]:
    info = probe(path)
    cuts = scene_cuts(path, info["duration"])
    hits = motion_hits(path, float(Fraction(info["fps"])), cuts)
    segments, warning = transcript(path, directory, bool(info["audio_streams"]), info)
    return info | {"cuts": cuts, "motion_hits": hits, "transcript": segments,
                   "voice_regions": [[s["start"], min(s["end"], info["duration"])] for s in segments
                                     if s["start"] < info["duration"]],
                   "warnings": [warning] if warning else []}


def merge_regions(regions: list[list[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for start, end in sorted(regions):
        if end <= start:
            continue
        if result and start <= result[-1][1] + RAMP * 2:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def audio_info(path: Path) -> dict[str, Any]:
    data = json.loads(command("ffprobe", ["-v", "error", "-select_streams", "a:0", "-show_streams", "-show_format", "-of", "json", str(path)]).stdout)
    if not data.get("streams"):
        raise ValueError("Take has no audio stream.")
    stream = data["streams"][0]
    duration = float(stream.get("duration") or data["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Take has no usable audio duration.")
    return {"duration": duration, "channels": stream["channels"], "rate": stream["sample_rate"]}


def duck_gain(at: float, regions: list[list[float]]) -> float:
    for start, end in regions:
        if at < start - RAMP:
            break
        if start <= at <= end:
            return DUCK
        if start - RAMP <= at < start:
            return 1 - (1 - DUCK) * (at - start + RAMP) / RAMP
        if end < at < end + RAMP:
            return DUCK + (1 - DUCK) * (at - end) / RAMP
    return 1.0


def _samples(data: bytes, kind: str) -> array.array:
    values = array.array(kind)
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def mix_stem(cues: list[dict[str, Any]], audio: dict[str, Path], duration: float,
             voice_regions: list[list[float]], destination: Path, directory: Path) -> dict[str, float]:
    """Two bounded-memory passes, with a common gain that never boosts quiet material."""
    frames = round(duration * RATE)
    voices = merge_regions(voice_regions + [[c["start"], c["end"]] for c in cues if c["event_type"] == "voice-line"])
    peak = 0.0
    with tempfile.TemporaryDirectory(prefix=".mix-", dir=directory) as temp:
        decoded: dict[str, Path] = {}
        for ident, path in audio.items():
            target = Path(temp) / f"{len(decoded)}.wav"
            command("ffmpeg", ["-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(RATE),
                                "-c:a", "pcm_s16le", str(target)])
            decoded[ident] = target
        readers: OrderedDict[str, wave.Wave_read] = OrderedDict()

        def reader_for(ident: str) -> wave.Wave_read:
            if ident in readers:
                readers.move_to_end(ident)
            else:
                if len(readers) == 32:
                    readers.popitem(last=False)[1].close()
                readers[ident] = wave.open(str(decoded[ident]), "rb")
            return readers[ident]

        raw = Path(temp) / "mix.f32"
        try:
            with raw.open("wb") as stream:
                for offset in range(0, frames, 4096):
                    size = min(4096, frames - offset)
                    sfx, speech = [0.0] * (size * 2), [0.0] * (size * 2)
                    for cue in cues:
                        first, last = round(cue["start"] * RATE), round(cue["end"] * RATE)
                        left, right = max(offset, first), min(offset + size, last)
                        if left >= right:
                            continue
                        reader = reader_for(cue["take_id"])
                        source_at = left - first
                        if source_at >= reader.getnframes():
                            if not cue.get("loop") or not reader.getnframes():
                                continue
                            source_at %= reader.getnframes()
                        reader.setpos(source_at)
                        count = right - left
                        samples = _samples(reader.readframes(count), "h")
                        while len(samples) < count * 2 and cue.get("loop") and reader.getnframes():
                            reader.rewind()
                            samples.extend(_samples(reader.readframes(count - len(samples) // 2), "h"))
                        target = speech if cue["event_type"] == "voice-line" else sfx
                        begin = (left - offset) * 2
                        for i, value in enumerate(samples):
                            target[begin + i] += value / 32768
                    block = array.array("f")
                    for i in range(size):
                        gain = duck_gain((offset + i) / RATE, voices)
                        block.extend((sfx[i * 2] * gain + speech[i * 2], sfx[i * 2 + 1] * gain + speech[i * 2 + 1]))
                    peak = max(peak, max(map(abs, block), default=0))
                    if sys.byteorder != "little":
                        block.byteswap()
                    stream.write(block.tobytes())
        finally:
            for reader in readers.values():
                reader.close()
        gain = min(1.0, PEAK / peak) if peak else 1.0
        with raw.open("rb") as stream, wave.open(str(destination), "wb") as output:
            output.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
            while data := stream.read(4096 * 8):
                samples = _samples(data, "f")
                pcm = array.array("h", (int(value * gain * 32768) for value in samples))
                if sys.byteorder != "little":
                    pcm.byteswap()
                output.writeframesraw(pcm.tobytes())
    return {"peak_dbfs": 20 * math.log10(peak * gain) if peak else -120.0, "gain": gain}


def render_video(source: Path, stem: Path, info: dict[str, Any], destination: Path) -> None:
    filters, labels = original_audio_filters(info)
    labels.append("[1:a:0]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=longest,"
                   "alimiter=limit=0.95:level=false:latency=true[mix]")
    command("ffmpeg", ["-nostdin", "-v", "error", "-i", str(source), "-i", str(stem), "-filter_complex", ";".join(filters),
                       "-map", f"0:{info['video_stream']}", "-map", "[mix]", "-vf", "setpts=PTS-STARTPTS,pad=ceil(iw/2)*2:ceil(ih/2)*2",
                       "-r", info["fps"], "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "18",
                       "-c:a", "aac", "-b:a", "192k", "-t", str(info["duration"]), "-movflags", "+faststart", str(destination)])
