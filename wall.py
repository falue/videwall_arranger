#!/usr/bin/env python3
"""
Video wall previz and renderer.

    python3 wall.py        opens http://localhost:8770

Media lives in _content/, the arrangement in wall.json (autosaved), renders go
to _render/videowall<N>.mp4: one file per Pi, split 2x2 by its video wall adapter.
"""

import argparse
import colorsys
import io
import json
import math
import re
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
QUADS = ("tl", "tr", "bl", "br")
PATTERN_PREFIX = "videowall-4x4-"   # old stand-in pictures in _content, no media (stand-ins are generated now)
PATTERN_OPACITY = 0.5   # stand-ins over dropped content when the pattern setting is on

# Set from the command line in main().
CONTENT = HERE / "_content"
RENDER = HERE / "_render"
PROJECT_FILE = HERE / "wall.json"

DEFAULT_PROJECT = {
    "settings": {
        "fps": 25,
        "width": 1920,
        "height": 1080,
        "crf": 18,
    },
    # Position and width in free units (~cm), aspect of the visible picture.
    "screens": [
        {"id": "s1", "name": "Philips", "x": 22, "y": 4, "w": 45, "aspect": "16:9", "src": "none"},
        {"id": "s2", "name": "terra", "x": 71, "y": 5, "w": 41, "aspect": "16:9", "src": "none"},
        {"id": "s3", "name": "C", "x": 116, "y": 1, "w": 36, "aspect": "16:9", "src": "1.bl"},
        {"id": "s4", "name": "D", "x": 157, "y": 0, "w": 36, "aspect": "16:9", "src": "1.br"},
        {"id": "s5", "name": "E", "x": 22, "y": 44, "w": 53, "aspect": "16:9", "src": "2.tl"},
        {"id": "s6", "name": "F", "x": 81, "y": 43, "w": 53, "aspect": "16:9", "src": "2.tr"},
        {"id": "s7", "name": "Panasonic", "x": 143, "y": 43, "w": 35, "aspect": "16:9", "src": "none"},
        {"id": "s8", "name": "G", "x": 22, "y": 87, "w": 50, "aspect": "16:9", "src": "2.bl"},
        {"id": "s9", "name": "H", "x": 81, "y": 86, "w": 53, "aspect": "16:9", "src": "2.br"},
        {"id": "s10", "name": "Compaq", "x": 140, "y": 90, "w": 50, "aspect": "16:9", "src": "1.full"},
        {"id": "s11", "name": "A", "x": 22, "y": 135, "w": 45, "aspect": "16:9", "src": "1.tl"},
        {"id": "s12", "name": "CCTV", "x": 72, "y": 136, "w": 58, "aspect": "16:9", "src": "none"},
        {"id": "s13", "name": "B", "x": 139, "y": 134, "w": 60, "aspect": "16:9", "src": "1.tr"},
    ],
    # Video walls: "source" and "adapter" index into the hardware list, "length" max seconds (0 = longest clip).
    "walls": [{}, {}],
    # Content per adapter output, key "<video wall>.<quadrant>".
    "clips": {},
}

app = Flask(__name__)


# =============================================================================
# MEDIA
# =============================================================================

_probe_cache: dict[tuple[str, float], dict] = {}
_proxy_jobs: set[str] = set()
_proxy_lock = threading.Lock()


def media_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    return "video" if ext in VIDEO_EXT else "image" if ext in IMAGE_EXT else None


def proxy_path(name: str) -> Path:
    return CONTENT / ".proxy" / (name + ".mp4")


def probe(path: Path) -> dict:
    key = (path.name, path.stat().st_mtime)
    if key in _probe_cache:
        return _probe_cache[key]

    if media_kind(path) == "image":
        with Image.open(path) as img:
            info = {"kind": "image", "width": img.width, "height": img.height}
    else:
        data = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout)
        stream = data["streams"][0]
        num, den = stream["r_frame_rate"].split("/")
        info = {
            "kind": "video",
            "width": stream["width"],
            "height": stream["height"],
            "fps": int(num) / int(den),
            "duration": float(data["format"]["duration"]),
        }

    _probe_cache[key] = info
    return info


def make_proxy(name: str) -> None:
    """Small h264 copy with many keyframes, so the browser plays and seeks it fast."""
    target = proxy_path(name)
    target.parent.mkdir(exist_ok=True)
    temp = target.with_suffix(".tmp.mp4")
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(CONTENT / name),
         "-vf", "scale='min(640,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "26", "-g", "6", "-pix_fmt", "yuv420p", "-an", str(temp)],
    )
    if result.returncode == 0:
        temp.replace(target)
    with _proxy_lock:
        _proxy_jobs.discard(name)


def proxy_ok(proxy: Path, source: Path, duration: float) -> bool:
    """The proxy exists, is not older than its source and as long (a copied or cut off one is made again)."""
    try:
        return proxy.stat().st_mtime >= source.stat().st_mtime and abs(probe(proxy)["duration"] - duration) < 1
    except (OSError, subprocess.CalledProcessError, KeyError, IndexError):
        return False


def list_media() -> dict:
    media = {}
    for path in sorted(CONTENT.iterdir(), key=lambda p: p.name.lower()):
        kind = media_kind(path)
        if path.name.startswith((".", PATTERN_PREFIX)) or not kind:
            continue
        try:
            info = dict(probe(path))
        except (subprocess.CalledProcessError, KeyError, IndexError, OSError):
            continue

        info["url"] = f"/media/{path.name}"
        if kind == "video":
            proxy = proxy_path(path.name)
            if proxy_ok(proxy, path, info["duration"]):
                info["url"] = f"/proxy/{path.name}"
            else:
                info["proxyPending"] = True
                with _proxy_lock:
                    if path.name not in _proxy_jobs:
                        _proxy_jobs.add(path.name)
                        threading.Thread(target=make_proxy, args=(path.name,), daemon=True).start()
        media[path.name] = info
    return media


def list_renders() -> list[dict]:
    return [
        {"name": p.name, "mtime": p.stat().st_mtime}
        for p in sorted(RENDER.iterdir()) if p.suffix in (".mp4", ".png")
    ]


# =============================================================================
# PROJECT AND LOOP PLAN
# =============================================================================

def load_project() -> dict:
    project = json.loads(PROJECT_FILE.read_text() if PROJECT_FILE.exists() else json.dumps(DEFAULT_PROJECT))
    if "walls" not in project:   # older files only had a number of walls
        project["walls"] = [{} for _ in range(project["settings"].pop("devices", 2))]
    if "hardware" not in project:   # older files kept the ports per wall
        first = project["walls"][0] if project["walls"] else {}
        project["hardware"] = {
            "sources": [{"name": "Raspberry Pi", "outs": first.get("sourceOuts", 1),
                         "type": first.get("sourceType", "Micro HDMI")}],
            "adapters": [{"name": "2x2 video wall adapter", "inputs": first.get("inputs", ["HDMI"]),
                          "out": {q: first.get("out", {}).get(q, "HDMI") for q in QUADS}}],
            "splitters": [{"name": "HDMI splitter 1x2", "count": 2, "outs": 2, "input": "HDMI", "output": "HDMI"}],
        }
        for wall in project["walls"]:
            for key in ("sourceOuts", "sourceType", "inputs", "out"):
                wall.pop(key, None)
    for item in project["hardware"]["sources"] + project["hardware"]["adapters"]:
        item.setdefault("count", 1)   # how many of them there are, older files had none
    # Older files could put a clip on a wall's whole frame ("1.full"); a full monitor only mirrors the wall now.
    project["clips"] = {key: clip for key, clip in project["clips"].items() if not key.endswith(".full")}
    cap = project["settings"].pop("maxLength", 0)   # older files: one max length for all walls
    for wall in project["walls"]:
        if not wall.get("length"):
            wall["length"] = cap
    return project


def ratio(text) -> float:
    a, _, b = str(text).partition(":")
    return float(a) / float(b) if b else float(a)


def quad_aspect(project: dict, key: str) -> float:
    """
    Aspect the content is made for: that of the first monitor showing this
    output if it is pre-stretched for it (the monitor squeezes it back), else
    the output's own.
    """
    s = project["settings"]
    for screen in project["screens"]:
        if screen["src"] == key and screen.get("stretch"):
            return ratio(screen["aspect"])
    return s["width"] / s["height"]


def output_format(project: dict, owner: dict) -> dict:
    """fps, size and crf of a wall's file or a direct source's (owner): the common one, or its own."""
    s = project["settings"]
    own = {} if s.get("sameFormat", True) else owner.get("format", {})
    return {key: own.get(key, s[key]) for key in ("fps", "width", "height", "crf")}


def clip_frames(clip: dict, info: dict, fps: int) -> tuple[int, int, int]:
    """Source frames, frames of one loop unit and crossfade frames of a video clip."""
    end = min(clip.get("out") or info["duration"], info["duration"])
    source = max(2, round((end - clip.get("in", 0)) * fps))
    mode = clip.get("mode", "loop")
    xfade = min(source // 2, max(1, round(clip.get("xfade", 3) * fps))) if mode == "xfade" else 0
    unit = 2 * source - 2 if mode == "pingpong" else source - xfade
    return source, unit, xfade


def make_plan(project: dict, media: dict) -> dict:
    """
    Per video wall one length for its file. Clips always play at their own
    speed: the least common multiple of its clips, so all loop seamlessly, if
    it is not longer than the wall's max length, else as long as its longest
    clip (at most the max length) and the others are cut off at the end.
    Max length 0: always as long as the longest clip.
    """
    walls, clips = {}, {}

    for wall, wall_settings in enumerate(project["walls"], 1):
        fps = output_format(project, wall_settings)["fps"]
        videos, images = {}, []
        for key, clip in project["clips"].items():
            info = media.get(clip.get("file"))
            if key.split(".")[0] != str(wall) or not info:
                continue
            if info["kind"] == "image":
                images.append(key)
            else:
                videos[key] = clip_frames(clip, info, fps)

        units = [unit for _, unit, _ in videos.values()]
        cap = round(wall_settings.get("length", 0) * fps)
        if not units:
            total = 10 * fps   # still images only
        elif cap and (common := math.lcm(*units)) <= cap:
            total = common
        else:
            total = max(1, min(max(units), cap)) if cap else max(units)

        for key, (source, unit, xfade) in videos.items():
            clips[key] = {"source": source, "unit": unit, "xfade": xfade, "loops": total / unit}
        for key in images:
            clips[key] = {"image": True}
        cut = [key for key, (_, unit, _) in videos.items() if total % unit]
        walls[wall] = {"frames": total, "seconds": total / fps, "cut": cut}

    return {"walls": walls, "clips": clips}


# =============================================================================
# RENDER
# =============================================================================

JOB = {"state": "idle", "progress": 0.0, "message": ""}
_job_lock = threading.Lock()


def run_ffmpeg(args: list, frames: int, step: int, steps: int) -> None:
    process = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    errors = []
    for line in process.stdout:
        if line.startswith("frame="):
            JOB["progress"] = (step + min(1, int(line[6:]) / frames)) / steps
        elif not re.match(r"^\w+=", line):
            errors.append(line.strip())
    if process.wait():
        raise RuntimeError("\n".join(errors[-8:]) or "ffmpeg failed")


def fit_filter(fit: str, width: int, height: int, aspect: float) -> str:
    """Crop or pad to the monitor's aspect, then squash into the output size."""
    if fit == "contain":
        geometry = f"pad=w='max(iw,ih*{aspect})':h='max(ih,iw/{aspect})':x=(ow-iw)/2:y=(oh-ih)/2,"
    elif fit == "stretch":
        geometry = ""
    else:
        geometry = f"crop=w='min(iw,ih*{aspect})':h='min(ih,iw/{aspect})',"
    return f"format=yuv444p,{geometry}scale={width}:{height},setsar=1,format=yuv420p"


def unit_args(clip: dict, info: dict, plan_clip: dict, width: int, height: int,
              aspect: float, fps: int, total: int, target: Path) -> list:
    """ffmpeg arguments for one loop of a clip (an image: the whole wall), already in its final size."""
    source = CONTENT / clip["file"]
    fit = fit_filter(clip.get("fit", "cover"), width, height, aspect)
    frames = total if info["kind"] == "image" else plan_clip["unit"]
    encode = ["-map", "[v]", "-frames:v", str(frames), "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", "-an", str(target)]

    if info["kind"] == "image":
        return ["-loop", "1", "-framerate", str(fps), "-i", str(source),
                "-filter_complex", f"[0:v]{fit},format=yuv420p[v]", *encode]

    n, x = plan_clip["source"], plan_clip["xfade"]
    graph = f"[0:v]fps={fps},tpad=stop_mode=clone:stop=2,trim=end_frame={n},setpts=PTS-STARTPTS,{fit}"
    mode = clip.get("mode", "loop")
    if mode == "pingpong":
        graph += (f",split[a][b];[b]reverse,trim=start_frame=1:end_frame={n - 1},"
                  f"setpts=PTS-STARTPTS[r];[a][r]concat=n=2:v=1:a=0")
    elif mode == "xfade":
        graph += (f",split[a][b];[a]trim=end_frame={x},setpts=PTS-STARTPTS[h];"
                  f"[b]trim=start_frame={x},setpts=PTS-STARTPTS[m];"
                  f"[m][h]xfade=transition=fade:duration={x / fps}:offset={(n - 2 * x) / fps}")
    graph += f",tpad=stop_mode=clone:stop=4,trim=end_frame={frames},format=yuv420p[v]"

    start = clip.get("in", 0)
    return ["-ss", str(start), "-t", str(n / fps + 1), "-i", str(source),
            "-filter_complex", graph, *encode]


def direct_name(source: dict, index: int) -> str:
    """File name for what a direct source plays, from its name: "Raspberry Pi X" -> raspberry-pi-x."""
    return re.sub(r"[^a-z0-9]+", "-", source["name"].lower()).strip("-") or f"source-{index + 1}"


def pattern_image(project: dict, key: str, size: tuple = (960, 540)) -> Image.Image:
    """
    Stand-in of a wall output ("1.tl") or a direct source ("src.0"): shown where
    nothing is dropped and overlaid with the pattern setting. Every one gets its
    own bright colour and letter, says where it comes from and which monitors
    show it.
    """
    wall, part = key.split(".")
    if wall == "src":
        index = len(project["walls"]) * 4 + int(part)
        top, source = "direct source", project["hardware"]["sources"][int(part)]["name"]
    else:
        index = (int(wall) - 1) * 4 + QUADS.index(part)
        sources = project["hardware"]["sources"]
        source = sources[min(project["walls"][int(wall) - 1].get("source", 0), len(sources) - 1)]["name"]
        top = f"video wall {wall} · out {QUADS.index(part) + 1}"
    monitors = ", ".join(s["name"] for s in project["screens"] if s["src"] == key) or "no monitor"

    letter, n = "", index
    while n >= 0:
        letter, n = chr(65 + n % 26) + letter, n // 26 - 1
    red, green, blue = colorsys.hsv_to_rgb(index * 0.618034 % 1, 1, 1)

    # Drawn at the aspect its content is made for, squashed to size at the end (pre-stretch).
    height = size[1]
    width = round(height * quad_aspect(project, key))
    k = height / 540   # sizes below are for 960 x 540
    colour = (round(red * 255), round(green * 255), round(blue * 255))
    image = Image.new("RGB", (width, height), colour)
    draw = ImageDraw.Draw(image)
    draw.line([(0, 0), (width, height)], fill="black", width=2)
    draw.line([(width, 0), (0, height)], fill="black", width=2)
    # Thick frame in the inverted colour, so every edge of the picture is plain to see.
    draw.rectangle([0, 0, width - 1, height - 1], outline=tuple(255 - c for c in colour), width=round(16 * k))
    # Square and circle around the letter: on the monitor they are only square and round if the aspect is right.
    half = 135 * k
    box = [width / 2 - half, height / 2 - half, width / 2 + half, height / 2 + half]
    draw.rectangle(box, outline="black", width=round(6 * k))
    draw.ellipse(box, outline="black", width=round(6 * k))

    def text(line: str, y: float, size: int) -> None:
        font = ImageFont.load_default(size=size)
        while draw.textlength(line, font=font) > width - 60 and size > 12:
            size -= 2
            font = ImageFont.load_default(size=size)
        draw.text((width / 2, y), line, font=font, anchor="mm", fill="white",
                  stroke_width=max(2, size // 9), stroke_fill="black")

    text(top, 70 * k, round(46 * k))
    text(letter, height / 2, round(230 * k))
    text(source, height - 110 * k, round(34 * k))
    text(monitors, height - 58 * k, round(30 * k))
    return image.resize(size, Image.LANCZOS)


def render_wall(project: dict) -> None:
    media = list_media()
    plan = make_plan(project, media)
    s = project["settings"]
    sources = project["hardware"]["sources"]
    formats = {d: output_format(project, wall) for d, wall in enumerate(project["walls"], 1)}

    missing = [c["file"] for c in project["clips"].values() if c.get("file") not in media]
    if missing:
        raise RuntimeError("missing in _content: " + ", ".join(missing))

    work = RENDER / ".work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    devices = range(1, len(project["walls"]) + 1)
    keys = [f"{d}.{q}" for d in devices for q in QUADS if f"{d}.{q}" in project["clips"]]
    direct_used = [i for i in range(len(sources)) if any(screen["src"] == f"src.{i}" for screen in project["screens"])]
    with_preview = s.get("renderPreview", True)
    steps, step = len(keys) + len(devices) + len(direct_used) + with_preview, 0

    units = {}
    for key in keys:
        clip, device = project["clips"][key], int(key.split(".")[0])
        f = formats[device]
        JOB["message"] = f"preparing {key} ({clip['file']})"
        units[key] = work / f"{key}.mp4"
        wall_frames = plan["walls"][device]["frames"]
        run_ffmpeg(   # one quadrant, even sizes
            unit_args(clip, media[clip["file"]], plan["clips"][key], f["width"] // 4 * 2, f["height"] // 4 * 2,
                      quad_aspect(project, key), f["fps"], wall_frames, units[key]),
            plan["clips"][key].get("unit", wall_frames), step, steps,
        )
        step += 1

    outputs = {}
    for device in devices:
        JOB["message"] = f"rendering video wall {device}"
        f = formats[device]
        fps, width, height = f["fps"], f["width"] // 4 * 2, f["height"] // 4 * 2
        inputs, graph = [], []

        for quad in QUADS:   # its clip, else its stand-in
            key = f"{device}.{quad}"
            pattern = work / f"pattern_{key}.png"
            pattern_image(project, key, (width, height)).save(pattern)
            dropped = key in units
            if dropped:
                inputs += ["-stream_loop", "-1", "-i", str(units[key])]
            else:
                inputs += ["-loop", "1", "-i", str(pattern)]
            graph.append(f"[{inputs.count('-i') - 1}:v]scale={width}:{height},fps={fps},setsar=1[{quad}]")
            if dropped and s.get("renderPattern"):
                inputs += ["-loop", "1", "-i", str(pattern)]
                graph.append(f"[{inputs.count('-i') - 1}:v]scale={width}:{height},format=rgba,"
                             f"colorchannelmixer=aa={PATTERN_OPACITY}[p{quad}]")
                graph.append(f"[{quad}][p{quad}]overlay[o{quad}]")
                graph.append(f"[o{quad}]format=yuv420p[{quad}x]")
            else:
                graph.append(f"[{quad}]format=yuv420p[{quad}x]")
        graph.append("[tlx][trx][blx][brx]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[v]")

        # Only still images on this wall: the result is an image too.
        still = all(media[project["clips"][k]["file"]]["kind"] == "image" for k in keys if k.startswith(f"{device}."))
        outputs[device] = save_output(
            [*inputs, "-filter_complex", ";".join(graph), "-map", "[v]"],
            f"videowall{device}", still, plan["walls"][device]["frames"], fps, f["crf"], "medium", work, step, steps,
        )
        step += 1

    # Direct sources: their file copied unchanged or re-encoded, named after the source. Without one: its stand-in.
    direct, made = {}, []
    for index in direct_used:
        source, key = sources[index], f"src.{index}"
        stem, f = direct_name(source, index), output_format(project, source)
        clip = project["clips"].get(key)
        JOB["message"] = f"direct source {source['name']}"
        if not clip:
            target = RENDER / f"{stem}.png"
            pattern_image(project, key, (f["width"], f["height"])).save(target)
            made.append(f"{target.name} (stand-in)")
        elif source.get("copy", True):
            file = CONTENT / clip["file"]
            target = RENDER / (stem + file.suffix.lower())
            shutil.copyfile(file, target)
            made.append(f"{target.name} (copied)")
        else:
            info, (width, height) = media[clip["file"]], (f["width"] // 2 * 2, f["height"] // 2 * 2)
            still = info["kind"] == "image"
            fit = fit_filter(clip.get("fit", "cover"), width, height, width / height)
            graph = f"[0:v]{fit}[v]" if still else f"[0:v]fps={f['fps']},{fit}[v]"
            target = save_output(
                ["-i", str(CONTENT / clip["file"]), "-filter_complex", graph, "-map", "[v]"],
                stem, still, 1 if still else max(1, round(info["duration"] * f["fps"])),
                f["fps"], f["crf"], "medium", work, step, steps,
            )
            made.append(f"{target.name} (re-encoded)")
        for old in RENDER.glob(f"{stem}.*"):   # an earlier file of another type
            if old != target:
                old.unlink()
        direct[key] = target
        step += 1

    if with_preview:
        JOB["message"] = "rendering wall preview"
        feeds = {str(d): o for d, o in outputs.items()} | direct
        fps = s["fps"]
        made.append(save_output(
            preview_args(project, feeds, fps), "wall_preview", all(media_kind(f) == "image" for f in feeds.values()),
            round(max(w["seconds"] for w in plan["walls"].values()) * fps), fps, 23, "veryfast", work, step, steps,
        ).name)

    shutil.rmtree(work, ignore_errors=True)
    JOB["message"] = "done: " + ", ".join(
        [o.name + ("" if o.suffix == ".png" else f" ({plan['walls'][d]['seconds']:.2f} s)") for d, o in outputs.items()]
        + made
    )


def save_output(args: list, name: str, still: bool, frames: int, fps: int, crf: int,
                preset: str, work: Path, step: int, steps: int) -> Path:
    """Renders to _render/<name>.png or .mp4 and removes the other one, so no stale file stays."""
    target = RENDER / (name + (".png" if still else ".mp4"))
    temp = work / target.name
    if still:
        run_ffmpeg([*args, "-frames:v", "1", "-update", "1", str(temp)], 1, step, steps)
    else:
        run_ffmpeg(
            [*args, "-frames:v", str(frames), "-r", str(fps), "-c:v", "libx264", "-preset", preset,
             "-crf", str(crf), "-pix_fmt", "yuv420p", "-g", str(fps * 2), "-movflags", "+faststart",
             "-an", str(temp)],
            frames, step, steps,
        )
    temp.replace(target)
    target.with_suffix(".mp4" if still else ".png").unlink(missing_ok=True)
    return target


def preview_args(project: dict, feeds: dict[str, Path], fps: int,
                 width: int = 1920, bezel: float = 2.5) -> list:
    """
    Input and filter arguments: the rendered files on the whole wall. feeds maps a
    wall number ("1") or a direct source ("src.0") to its file. Each quadrant is
    stretched onto its monitor, a direct source's file fitted as its clip says.
    """
    screens = [(s, s["w"] / ratio(s["aspect"])) for s in project["screens"]]
    left = min(s["x"] for s, _ in screens) - bezel
    top = min(s["y"] for s, _ in screens) - bezel
    scale = width / (max(s["x"] + s["w"] for s, _ in screens) + bezel - left)
    height = round((max(s["y"] + h for s, h in screens) + bezel - top) * scale / 2) * 2

    def box(x, y, w, h):
        return round((x - left) * scale), round((y - top) * scale), round(w * scale), round(h * scale)

    # Bezels and dark pictures of the external monitors go into the background.
    background = f"color=c=0x121417:s={width}x{height}:r={fps}"
    for screen, h in screens:
        x, y, w, hh = box(screen["x"] - bezel, screen["y"] - bezel, screen["w"] + 2 * bezel, h + 2 * bezel)
        background += f",drawbox={x}:{y}:{w}:{hh}:0x050607:fill"
        x, y, w, hh = box(screen["x"], screen["y"], screen["w"], h)
        background += f",drawbox={x}:{y}:{w}:{hh}:0x0b0c0e:fill"

    inputs, graph, layer = [], [f"{background}[bg0]"], 0
    for feed, (key, file) in enumerate(feeds.items()):
        uses = [(s, h) for s, h in screens if s["src"] == key or s["src"].split(".")[0] == key]
        if not uses:
            continue
        still = media_kind(file) == "image"
        inputs += ["-loop" if still else "-stream_loop", "1" if still else "-1", "-i", str(file)]
        graph.append(f"[{inputs.count('-i') - 1}:v]split={len(uses)}" + "".join(f"[f{feed}_{i}]" for i in range(len(uses))))
        for index, (screen, h) in enumerate(uses):
            quad = screen["src"].split(".")[1]
            x, y, w, hh = box(screen["x"], screen["y"], screen["w"], h)
            w, hh = max(2, w // 2 * 2), max(2, hh // 2 * 2)
            clip = project["clips"].get(key) if key.startswith("src.") else None
            fit = clip.get("fit", "cover") if clip else "stretch"
            if quad in QUADS:
                geometry = f"crop=iw/2:ih/2:{QUADS.index(quad) % 2}*iw/2:{QUADS.index(quad) // 2}*ih/2,scale={w}:{hh}"
            elif fit == "cover":
                geometry = f"scale={w}:{hh}:force_original_aspect_ratio=increase,crop={w}:{hh}"
            elif fit == "contain":
                geometry = (f"scale={w}:{hh}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                            f"pad={w}:{hh}:(ow-iw)/2:(oh-ih)/2")
            else:
                geometry = f"scale={w}:{hh}"
            graph.append(f"[f{feed}_{index}]{geometry},setsar=1[s{layer}]")
            graph.append(f"[bg{layer}][s{layer}]overlay={x}:{y}[bg{layer + 1}]")
            layer += 1

    return [*inputs, "-filter_complex", ";".join(graph), "-map", f"[bg{layer}]"]


def start_job(task, *args):
    with _job_lock:
        if JOB["state"] == "running":
            return jsonify(error="a render is already running"), 409
        JOB.update(state="running", progress=0.0, message="starting")

    def run():
        try:
            task(*args)
            JOB.update(state="done", progress=1.0)
        except Exception as error:  # report any failure to the page
            JOB.update(state="error", message=str(error))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(JOB)


# =============================================================================
# ROUTES
# =============================================================================

@app.get("/")
def index():
    return send_file(HERE / "wall.html")


@app.get("/api/state")
def get_state():
    project = load_project()
    media = list_media()
    return jsonify(project=project, media=media, plan=make_plan(project, media), renders=list_renders())


@app.post("/api/state")
def save_state():
    project = request.get_json()
    temp = PROJECT_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(project, indent=1))
    temp.replace(PROJECT_FILE)
    return jsonify(plan=make_plan(project, list_media()))


@app.get("/api/media")
def get_media():
    return jsonify(media=list_media(), renders=list_renders())


@app.post("/api/upload")
def upload():
    names = []
    for file in request.files.getlist("files"):
        name = Path(file.filename).name
        if media_kind(Path(name)):
            file.save(CONTENT / name)
            names.append(name)
    return jsonify(names=names, media=list_media())


@app.post("/api/render")
def render():
    return start_job(render_wall, load_project())


@app.get("/api/job")
def job():
    return jsonify(**JOB, renders=list_renders())


@app.get("/pattern/<key>")
def pattern(key):
    buffer = io.BytesIO()
    pattern_image(load_project(), key).save(buffer, "PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", max_age=0)


@app.get("/media/<path:name>")
def media_file(name):
    return send_from_directory(CONTENT, name)


@app.get("/proxy/<path:name>")
def proxy_file(name):
    return send_from_directory(CONTENT / ".proxy", name + ".mp4")


def main():
    global CONTENT, RENDER, PROJECT_FILE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--content", type=Path, default=CONTENT)
    parser.add_argument("--render", type=Path, default=RENDER)
    parser.add_argument("--project", type=Path, default=PROJECT_FILE)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    CONTENT, RENDER, PROJECT_FILE = args.content.resolve(), args.render.resolve(), args.project.resolve()
    CONTENT.mkdir(parents=True, exist_ok=True)
    RENDER.mkdir(parents=True, exist_ok=True)

    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (f"http://localhost:{args.port}",)).start()
    app.run(port=args.port, threaded=True)


if __name__ == "__main__":
    main()
