#!/usr/bin/env python3
"""
Burn-in in the look of 85_gegensprechanlage: a running clock and text in its
pixel font (black border, every second row see-through), plus optional noise,
grey, scanlines and vignette.

    python3 burnin.py clip.mp4                          clock + "Floor 2", like 85
    python3 burnin.py clip.mp4 --text "Laundry room 1" --noise 20 --gray
    python3 burnin.py clip.mp4 --text "- Connection lost -" --start 12 --end 20
    python3 burnin.py still.png --text "- Live -"       image in, image out

Writes <name>_burnin.mp4 (or .png) next to the input unless -o is given.
"""

import argparse
import json
import math
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_FILE = Path(__file__).resolve().parent / "data" / "Flexi_IBM_VGA_True.ttf"   # font of 85_gegensprechanlage
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def text_block(lines: list[str], scale: int) -> Image.Image:
    """As Terminal.overlay() in 85: 13 px text, 1 px black border, every second row at 55 %, scaled up unsmoothed."""
    font = ImageFont.truetype(str(FONT_FILE), 13)
    edge, step = 1, 15
    width = math.ceil(max(font.getlength(line) for line in lines)) + edge * 2
    height = len(lines) * step + edge * 2

    mask = Image.new("L", (width, height))
    draw = ImageDraw.Draw(mask)
    for index, line in enumerate(lines):
        draw.text((edge, index * step + edge), line, fill=255, font=font)

    text = np.asarray(mask, dtype=np.float32) / 255
    padded = np.pad(text, edge)
    border = np.max([padded[dy:dy + height, dx:dx + width] for dy in range(3) for dx in range(3)], axis=0)
    alpha = text + border * (1 - text)
    alpha[::2] *= 0.55

    rgba = np.zeros((height, width, 4), np.uint8)
    rgba[..., :3] = (text * 255)[..., None].astype(np.uint8)
    rgba[..., 3] = (alpha * 255).astype(np.uint8)
    return Image.fromarray(rgba).resize((width * scale, height * scale), Image.NEAREST)


def fx_image(width: int, height: int, scale: int, scanlines: float, vignette: float) -> Image.Image:
    """Static black layer: every second pixel row darkened, dark corners."""
    y = np.arange(height)[:, None]
    x = np.arange(width)[None, :]
    rows = np.where((y // scale) % 2 == 0, scanlines / 100, 0.0)
    corners = np.clip((np.hypot(x - width / 2, y - height / 2) - 0.34 * height) / (0.46 * height), 0, 1)
    alpha = 1 - (1 - rows) * (1 - corners * vignette / 100)

    rgba = np.zeros((height, width, 4), np.uint8)
    rgba[..., 3] = (alpha * 255).astype(np.uint8)
    return Image.fromarray(rgba)


def probe(path: Path) -> tuple[int, int, float | None]:
    data = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout)
    stream = data["streams"][0]
    duration = data["format"].get("duration")
    return stream["width"], stream["height"], float(duration) if duration else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="default: <name>_burnin.mp4 / .png next to the input")
    parser.add_argument("--text", action="append", help='text line under the clock, repeat for more lines (default "Floor 2", "" for none)')
    parser.add_argument("--no-clock", action="store_true", help="no clock line")
    parser.add_argument("--clock-start", default="1999-09-11 21:47:00", help="first clock time (default as in 85)")
    parser.add_argument("--size", type=float, default=3, help="pixel size on a 1080 high picture (default 3)")
    parser.add_argument("--x", type=float, default=60, help="left on a 1080 high picture (default 60)")
    parser.add_argument("--y", type=float, default=40, help="top on a 1080 high picture (default 40)")
    parser.add_argument("--noise", type=int, default=0, help="0 - 100 (default 0)")
    parser.add_argument("--gray", action="store_true", help="black and white")
    parser.add_argument("--scanlines", type=float, default=0, help="0 - 100 %% darkening of every second pixel row")
    parser.add_argument("--vignette", type=float, default=0, help="0 - 100 %% dark corners")
    parser.add_argument("--start", type=float, default=0, help="seconds, video only")
    parser.add_argument("--end", type=float, help="seconds, video only (default: to the end)")
    parser.add_argument("--crf", type=int, default=18)
    args = parser.parse_args()

    still = args.input.suffix.lower() in IMAGE_EXT
    width, height, duration = probe(args.input)
    width, height = width // 2 * 2, height // 2 * 2
    output = args.output or args.input.with_name(args.input.stem + "_burnin" + (".png" if still else ".mp4"))

    lines = [line for line in (args.text if args.text is not None else ["Floor 2"]) if line]
    scale = max(1, int(args.size * height / 1080 + 0.5))
    position = f"{round(args.x * height / 1080)}:{round(args.y * height / 1080)}"
    seconds = 0 if still else (args.end or duration) - args.start

    inputs = [] if still else ["-ss", str(args.start), "-t", str(seconds)]
    inputs += ["-i", str(args.input)]
    chain = f"[0:v]scale={width}:{height},setsar=1"
    if args.gray:
        chain += ",hue=s=0"
    if args.noise:
        chain += f",noise=alls={args.noise}:allf=t+u"

    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)

        def overlay(source: list, at: str) -> str:
            inputs.extend(source)
            index = inputs.count("-i") - 1
            return f"[l{index}];[l{index}][{index}:v]overlay={at}"

        if args.scanlines or args.vignette:
            fx_image(width, height, scale, args.scanlines, args.vignette).save(work / "fx.png")
            chain += overlay(["-loop", "1", "-i", str(work / "fx.png")], "0:0")

        if not args.no_clock:
            start = datetime.strptime(args.clock_start, "%Y-%m-%d %H:%M:%S")
            for second in range(math.ceil(seconds) + 1):
                clock = (start + timedelta(seconds=second)).strftime("%d.%m.%Y  %H:%M:%S")
                text_block([clock, *lines], scale).save(work / f"clock_{second:05d}.png")
            chain += overlay(["-framerate", "1", "-i", str(work / "clock_%05d.png")], position)
        elif lines:
            text_block(lines, scale).save(work / "text.png")
            chain += overlay(["-loop", "1", "-i", str(work / "text.png")], position)

        if still:
            encode = ["-frames:v", "1", "-update", "1"]
        else:
            encode = ["-map", "0:a?", "-t", str(seconds), "-c:v", "libx264", "-crf", str(args.crf),
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats", *inputs,
             "-filter_complex", chain + "[v]", "-map", "[v]", *encode, str(output)],
            check=True,
        )
    print(output)


if __name__ == "__main__":
    main()
