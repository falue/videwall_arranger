# Video wall

Previz, renderer and cabling plan for the monitor wall. A video wall is one
video source (at NINE a Raspberry Pi) feeding a 2x2 video wall adapter (wall 1
= A B C D, wall 2 = E F G H). Every source plays one file, the adapter splits
it into four monitors, splitters duplicate signals onto more monitors.

```
python3 wall.py        # opens http://localhost:8770
```

Needs `ffmpeg`, `flask`, `pillow`.

## Files
path | what
--- | ---
`_content/` | media; drop files on the page to add them. Images are in git (stand-ins for videos to come), videos and `.proxy/` (small previz copies) are not
`wall.json` | the arrangement (recipe), saved automatically on every change
`_render/videowall<N>.mp4` | the rendered files, one per video source. `.png` if all content of that wall is still images
`_render/wall_preview.mp4` | the video wall files placed on the whole wall, made with every render (`.png` if all of them are)
`burnin.py` | separate CLI: clock / text burn-in and noise, see below

## Stand-ins
Every output of a video wall, and every direct source, shows a generated stand-in until something is
dropped there, in the previz and in the render: its own bright colour with a thick frame in the inverted
colour, its own letter (A-D wall 1, E-H wall 2, ...), wall and output, source and monitors. They follow every change of walls, sources and monitors.
`Pattern` (or `P`) lays them at 50 % over the dropped content of the walls too, in the previz. For the
render: "render the stand-in patterns too" under `Render videos…`.
The old `_content/videowall-4x4-*.png` files are not used any more and are not listed as media.

## Use
- Drag media onto a screen. Drag a screen onto another one to swap content. `×` on a media item takes it off the list (the file stays, dropping it again brings it back).
- Click a screen: in / out (drag the bar), loop mode, fit.
- `Space` play / pause, `⌫` clears a screen, `R` shows the rendered files on the wall.
- The strip under the wall shows the stitched frame of each video wall: live, or the rendered file with `R`.
  `+ video wall` adds one, `×` removes one. Click a wall: its source, video wall adapter and max video length.
- `Available hardware…`: the video sources (identical outputs, port type), video wall adapters (inputs, type per output)
  and splitters (outputs, in / out type) there are, each with `count` = how many of them you own. Each wall
  uses one of its source and adapter, a source feeding monitors directly one more; too few shows in red.
- `Render videos…`: the render settings in the right column, `Render` or `Enter` starts. "same format for
  all outputs" (default) or FPS / resolution / quality CRF per output file; a direct source's file is copied
  ("copy file", default) or re-encoded.
- Nothing selected, `Materials used` or `Esc`: "Materials used" on the right, grouped with piece counts: video sources, video wall
  adapters, splitters, monitors (identical ones grouped), cables by connector, cable adapters. Every line
  has a stock field (`-1` = unlimited), what is missing shows in colour.
- Monitors have input types (inspector chips, HDMI by default). Ports: HDMI, Micro HDMI, DisplayPort, VGA,
  DVI-I, DVI-D, Scart, RCA, TV antenna.
- `Screen arrangement…` (`L`): drag screens to move, the corner to resize, `⌫` deletes,
  `+ screen` adds. Source and aspect are set in the inspector.

## Sources
source | meaning
--- | ---
`wall 1 · out 1` ... `wall 2 · out 4` | an output of a video wall's adapter: 1 top left, 2 top right, 3 bottom left, 4 bottom right
`wall 1 · full` | the whole output (a monitor behind a video splitter). A clip here fills every quadrant of that wall without its own clip, e.g. a ready made 2x2 file
`direct · <source>` | hangs directly on a video source from the hardware list that plays its own content (e.g. the Raspberry Pi of 85_gegensprechanlage). Media dropped on it is copied unchanged to `_render/<source-name>.*` (or re-encoded, see `Render videos…`), without media its stand-in goes there as `.png`; own page in the cabling pdf
`external / off` | not connected (broken monitor)

The adapter sends each 960x540 quadrant as 16:9, most 4:3 monitors squeeze
it to their shape. `⇔ stretch` (button on every non-16:9 video screen, or in
the inspector) renders the content cropped / padded to the monitor's aspect
(`fit`) and pre-stretched, so the monitor's squeeze undoes it. Off: the
picture goes out as is and looks squeezed there. The previz shows both.

## Cabling plan
`Cabling` shows the plan in place of the wall (drag / scroll pans, pinch or ⌘ / Ctrl + wheel
zooms, double-click fits), `Export PDF…` on the right prints it. One page per video wall:
flowchart video source → splitter → adapter → splitter → monitors and a
table of every cable. The video source feeds the adapter and the monitors on
`wall N · full` from its own outputs; when they run out, its last output
feeds a splitter. Several monitors on one quadrant hang on a splitter after
the adapter. Splitters are taken from the hardware list (the smallest free
one with enough outputs, wall 1 first). If none is left, the plan assumes one
more of the smallest listed type with enough outputs (or a plain HDMI splitter
1×n). Where output and input types differ, the plan names the
adapter, `(active converter)` when a plain plug adapter can't do it,
and lists all adapters needed per wall. Each cable shows its output port and
adapter right at the port, the cable in the middle and the input at its end; the adapter
sits on the output, so the cable has the input's connector. Red is only what is short in stock
(the stock fields of "Materials used"), listed on every page.

## Looping
Each video wall has its own file length; clips always play at their own
speed. Set per wall: `max video length`. 0 (default): the file is as long as
its longest clip. Otherwise: the least common multiple of its clip lengths, so
every clip loops seamlessly, if that is not longer than the max; else as long
as its longest clip, at most the max. Clips that don't fit a whole number of
times are cut off at the end (the header names them, the inspector shows the
loops). Clip lengths like 4 / 6 / 8 s keep the common multiple short.

mode | loop seam
--- | ---
`loop` | hard cut from out back to in
`pingpong` | forward, then backward
`xfade` | the end fades into the start, the clip gets `crossfade` seconds shorter (default 3 s)

## Burn-in (`burnin.py`)
Separate command line tool, independent of the wall. Burns the clock and text
of 85_gegensprechanlage into a video or image: same font
(copied to `data/`), black border, every second row
see-through, size 3 at 60 / 40 on a 1080 high picture, clock from
`1999-09-11 21:47:00`. Optional noise, grey, scanlines, vignette (all off by
default, as in 85). Image in, image out. Needs `ffmpeg`, `numpy`, `pillow`.

```
python3 burnin.py clip.mp4                                   # clock + "Floor 2"
python3 burnin.py clip.mp4 --text "Laundry room 1" --noise 20 --gray
python3 burnin.py clip.mp4 --text "- Connection lost -" --start 12 --end 20
python3 burnin.py still.png --text "- Live -" --text "Cam 2" --no-clock
python3 burnin.py -h                                          # all options
```

Writes `<name>_burnin.mp4` / `.png` next to the input, or `-o <file>`.
