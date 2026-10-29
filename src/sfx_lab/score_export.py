"""FCPXML 1.8 interchange and CMX3600 events with Resolve marker comments."""

import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any

from .lab import Lab, atomic_write


def frames(seconds: float, fps: str) -> int:
    return round(Fraction(str(seconds)) * Fraction(fps))


def xml_time(seconds: float, fps: str) -> str:
    value = Fraction(frames(seconds, fps), 1) / Fraction(fps)
    return f"{value.numerator}/{value.denominator}s"


def timecode(frame: int, fps: str) -> str:
    nominal = round(float(Fraction(fps)))
    seconds, f = divmod(frame, nominal)
    minutes, s = divmod(seconds, 60)
    h, m = divmod(minutes, 60)
    if h > 23:
        raise ValueError("CMX3600 cannot represent timelines longer than 24 hours.")
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def parse_timecode(value: str, fps: str) -> float:
    if re.fullmatch(r"\d+:\d{2}:\d{2}:\d{2}", value):
        h, m, s, f = map(int, value.split(":"))
        nominal = round(float(Fraction(fps)))
        if m >= 60 or s >= 60 or f >= nominal:
            raise ValueError("Timecode must be HH:MM:SS:FF, non-drop-frame, at this edit's frame rate.")
        return float(Fraction(((h * 60 + m) * 60 + s) * nominal + f, 1) / Fraction(fps))
    try:
        seconds = Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError("Use seconds or HH:MM:SS:FF (non-drop-frame).") from error
    if seconds < 0:
        raise ValueError("Time must be nonnegative.")
    return float(seconds)


def fcpxml(lab: Lab, score: dict[str, Any], destination: Path) -> None:
    info, cues = score["analysis"], score["cues"]
    fps = info["fps"]
    duration = xml_time(info["duration"], fps)
    frame = xml_time(float(1 / Fraction(fps)), fps)
    root = ET.Element("fcpxml", version="1.8")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format", id="r1", frameDuration=frame,
                  width=str(info["width"]), height=str(info["height"]), colorSpace="1-1-1 (Rec. 709)")

    def asset(ident: str, path: str, video: bool, length: str, channels: int = 2, rate: str = "48000") -> None:
        attrs = {"id": ident, "name": Path(path).name, "src": lab.safe_path(path).as_uri(), "start": "0s", "duration": length,
                 "hasVideo": "1" if video else "0", "hasAudio": "1", "audioSources": "1", "audioChannels": str(channels), "audioRate": rate}
        if video:
            attrs.update(format="r1", hasAudio="1" if info["audio_streams"] else "0",
                         audioSources=str(len(info["audio_streams"])), audioChannels=str(info["audio_channels"]), audioRate=info["audio_rate"])
            if not info["audio_streams"]:
                for key in ("audioSources", "audioChannels", "audioRate"):
                    attrs.pop(key)
        ET.SubElement(resources, "asset", attrs)

    asset("r2", score["source"], True, duration)
    asset("r3", score["outputs"]["stem"], False, duration)
    event = ET.SubElement(root, "event", name="SFX Lab")
    project = ET.SubElement(event, "project", name=score["name"] + " scored")
    sequence = ET.SubElement(project, "sequence", format="r1", duration=duration, tcStart="0s", tcFormat="NDF",
                             audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(sequence, "spine")
    picture = ET.SubElement(spine, "asset-clip", ref="r2", name=score["name"], offset="0s", start="0s", duration=duration)
    ET.SubElement(picture, "asset-clip", ref="r3", name="SFX stem", lane="-1", offset="0s", start="0s", duration=duration,
                  audioRole="effects")
    # Disabled reference takes supply cue-named clips without doubling the audible stem.
    for n, cue in enumerate(cues, 4):
        sound = next(s for s in score["export_takes"] if s["id"] == cue["take_id"])
        asset(f"r{n}", sound["audio"], False, xml_time(sound["duration"], fps), sound["channels"], sound["rate"])
        ET.SubElement(picture, "asset-clip", ref=f"r{n}", name=cue["id"], lane=str(-n), enabled="0",
                      offset=xml_time(cue["start"], fps), start="0s", duration=xml_time(min(cue["end"] - cue["start"], sound["duration"]), fps),
                      audioRole="dialogue" if cue["event_type"] == "voice-line" else "effects")
    for cue in cues:
        ET.SubElement(picture, "marker", start=xml_time(cue["start"], fps), duration=frame,
                      value=cue["id"], note=f"{cue['event_type']}: {cue['prompt']}")
    ET.indent(root, space="  ")
    atomic_write(destination, '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(root, encoding="unicode") + "\n")


def edl(score: dict[str, Any], destination: Path) -> None:
    def plain(text: str) -> str:
        return " ".join(text.replace("|", "/").encode("ascii", "replace").decode().split())

    fps = score["analysis"]["fps"]
    lines = [f"TITLE: {plain(score['name'])} SFX markers", "FCM: NON-DROP FRAME", f"* FRAME RATE: {fps}", ""]
    for n, cue in enumerate(score["cues"], 1):
        at = frames(cue["start"], fps)
        start, end = timecode(at, fps), timecode(at + 1, fps)
        length = max(1, frames(cue["end"] - cue["start"], fps))
        lines += [f"{n:03d}  AX       V     C        {start} {end} {start} {end}",
                  f"* FROM CLIP NAME: {plain(cue['id'])}",
                  f"* COMMENT: {plain(cue['event_type'] + ': ' + cue['prompt'])}",
                  f" |C:ResolveColorBlue |M:{plain(cue['id'])} |D:{length}", ""]
    atomic_write(destination, "\n".join(lines) + "\n")
