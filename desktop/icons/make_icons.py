#!/usr/bin/env python3
"""Generate every raster icon the client ships, from the one vector master (``logo.svg``).

Run it after touching ``logo.svg``; the outputs are committed, because the client body is delivered
by ``git pull`` and nothing runs a build step on the user's machine.

    python3 desktop/icons/make_icons.py

Stdlib only — no Pillow, no cairosvg, no ``rsvg-convert``. The client declares exactly one runtime
dependency (``certifi``) and a dev tool that needs an image stack installed would be a tool nobody
can run. So this file carries its own SVG rasteriser: it handles the subset the master actually uses
(absolute ``M/L/H/V/C/Z``), which is checked on load — an unsupported command raises rather than
silently dropping a path.

Two icon-specific transforms live HERE and deliberately not in the master, which stays byte-faithful
to what design authored:

1. **Sizes below 32px get their own artwork.** A multi-size ICO is a container of independent
   images, not one bitmap Windows resizes, so each entry may be drawn for the size it will be seen
   at. Two things follow from that licence:

   *The framing braces are dropped.* With them the mark is 60×40 (1.5:1), so squaring it for an
   icon spends a third of the box on empty margin, and by 16px the braces are two smudges charged
   against the pixels the brain needs. Without them the brain is very nearly square and fills the
   box. From 32px up the braces hold their stroke and the full mark ships.

   *The mark itself is swapped for a stand-in* (``SMALL_ONLY_IDS``) where the master supplies one.
   16×16 is 256 pixels: a diagonal or an interior curve lands between the grid lines and silts up,
   while an upright, symmetrical figure stays crisp. The current master pairs its drawn mark with
   an earlier, plainer brain for exactly this. Optional — a master with no stand-in still gets the
   mark-minus-framing, which is what every earlier master relied on.

   The cutoff is 32 and not 48, where it first sat: 32px is what ``SM_CXICON`` asks for at 100%
   DPI, so it is the taskbar button — the one place most users ever see this icon. Cutting above it
   shipped the braces only to high-DPI desktops and the shell's large-icon views, which reads from
   an ordinary desktop as the framing having been dropped altogether.

2. **Any non-opaque fill is baked opaque.** A ``fill-opacity`` in the master is authored against the
   white artboard it was drawn on. Rasterised onto transparency and dropped on a dark title bar the
   same paint turns muddy and merges with whatever it overlaps. Baking the composite-over-white
   colour leaves white backgrounds identical and keeps the layering readable on dark ones. The
   current master is a single flat colour, so nothing triggers this — it stays because the next
   re-export is where it would silently matter.

3. **The macOS tile is inset to Apple's grid.** The Dock scales a whole 1024px canvas into its tile
   slot, and macOS does not mask an app icon the way iOS does — the rounded rect is ours to draw. So
   the body has to be the 824 of 1024 Apple's grid specifies, or the icon renders 1.24x larger than
   every neighbour in the Dock. It did: see ``TILE_BODY_RATIO``. Only the ``.icns`` is affected —
   Windows scales a window icon to fit its own box, so the same margin there would just shrink it.

The ``.icns`` is written by this file rather than by Apple's ``iconutil`` so that it can be rebuilt
and MEASURED anywhere. It is a committed runtime asset — the Dock reads it and nothing builds on a
user's machine — and an Apple-only writer is how a full-canvas tile shipped without being noticed.
"""

from __future__ import annotations

import math
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
MASTER = HERE / "logo.svg"
REPO = HERE.parent.parent

WHITE = (255, 255, 255)

# Windows window-icon sizes. 16 = title bar, 20/24 = title bar at 125%/150% DPI, 32 = taskbar and
# Alt-Tab, 48/64 = Alt-Tab and taskbar at high DPI, 128/256 = Task View and the properties sheet.
ICO_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
# Below this, the framing costs more than it carries. See the module docstring. Set to 32, not
# higher: 32 is what SM_CXICON asks for at 100% DPI, i.e. the taskbar button — the single place
# most users ever see this icon. A cutoff of 48 renders the braces only for high-DPI and the
# shell's large-icon views, which reads as "the braces were dropped" from every normal desktop.
BRACE_CUTOFF = 32
# Path ids that FRAME the mark rather than being it. Named here — not by listing the mark's own
# paths — so a re-export that renames or splits the mark still produces a correct small icon; only
# a change to the framing itself needs this touched.
FRAMING_IDS = ("brace-left", "brace-right")
# Paths drawn ONLY for the sizes below BRACE_CUTOFF, replacing the mark's own. An ICO entry is an
# independent image, not the big one resized, so a size too coarse for the real artwork may carry
# artwork drawn for it instead — the same licence the framing drop already takes, one step further.
# Optional: a master without these still gets a small icon, just the mark minus its framing.
SMALL_ONLY_IDS = ("brain-small",)
# ── Apple's macOS 11+ app-icon grid ──────────────────────────────────────────────────────────────
# The numbers, verbatim: a 1024x1024 document whose rounded-rect body is 824x824 with a 185.4px
# corner radius, leaving 100px of transparent margin per side.
#
# That margin is NOT slack to be reclaimed. macOS does not mask an app icon the way iOS does — the
# squircle is drawn by us — and the Dock scales the whole canvas into its tile slot. So a body drawn
# edge-to-edge lands in the slot that every conformant icon fills to 824/1024, and renders
# 1024/824 = 1.24x larger than its neighbours. That is exactly the defect this ratio fixes: the
# Dock tile for the audit / graphic-explanation / comic-explanation / whiteboard windows stood
# visibly taller than the apps beside it.
#
# Expressed as ratios rather than pixels because the same geometry has to hold for the 16px entry.
APPLE_CANVAS_PX = 1024
TILE_BODY_RATIO = 824 / APPLE_CANVAS_PX          # 0.8047 — body width as a fraction of the canvas
TILE_MARGIN_RATIO = (1 - TILE_BODY_RATIO) / 2    # 0.0977 — per side
# Radius as a fraction of the BODY, not of the canvas: 185.4/824. Apple's real curve is a
# superellipse; a plain rounded rect at this radius is close enough at icon sizes, which is the
# approximation this generator has always made. (It was 0.2237 of the canvas before — the same
# number in spirit, but measured against the wrong extent once the body stopped being the canvas.)
TILE_RADIUS_RATIO = 185.4 / 824
# How much of the tile BODY the mark occupies, per side. Body-relative on purpose: it is the mark's
# proportion inside the tile that the eye reads, and keeping it there means fixing the canvas
# geometry above does not also redraw the interior.
MARK_INSET_RATIO = 0.12
# Width/height the macOS menu-bar image is generated at, before trimming back to the ink. Only has
# to be at least the mark's own ratio; anything wider is trimmed away again, anything narrower would
# letterbox left/right instead, which trimming rows cannot fix.
MENU_BAR_ASPECT = 1.6
# Pixel height of that image. Drawn at ~18pt, so this is about 2x — Retina-crisp without a second
# @2x file, which neither reader would look for anyway.
MENU_BAR_HEIGHT_PX = 44

Color = Tuple[int, int, int]
Point = Tuple[float, float]


# ── SVG: the subset the master uses ──────────────────────────────────────────────────────────────
_TOKEN = re.compile(r"[A-Za-z]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
_PATH = re.compile(r"<path\b([^>]*)/>")
_ATTR = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_VIEWBOX = re.compile(r'viewBox\s*=\s*"([^"]*)"')


class Shape:
    """One ``<path>``: its id, its flat fill, and its outline as user-space subpaths."""

    def __init__(self, ident: str, fill: Color, opacity: float, subpaths: List[List[Point]]):
        self.id = ident
        self.fill = fill
        self.opacity = opacity
        self.subpaths = subpaths
        self.z = 0          # document order; the glyph must paint over the slash


def _hex_rgb(value: str) -> Color:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        raise ValueError("unsupported fill %r — the generator only reads #rgb / #rrggbb" % value)
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _flatten_cubic(p0: Point, p1: Point, p2: Point, p3: Point, out: List[Point]) -> None:
    """Append a polyline approximation of one cubic. Segment count comes from the control-polygon
    length, so a long sweeping curve gets more segments than a short one. The master is rasterised
    at >=1024px and then box-downsampled, so ~0.3 user-unit chords are far below one output pixel."""
    length = (math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3))
    steps = max(2, min(240, int(length / 0.9) + 2))
    for i in range(1, steps + 1):
        t = i / steps
        u = 1.0 - t
        out.append((
            u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        ))


def parse_path_data(d: str) -> List[List[Point]]:
    """Absolute ``M/L/H/V/C/Z`` only, including SVG's implicit command repetition."""
    tokens = _TOKEN.findall(d)
    subpaths: List[List[Point]] = []
    cur: List[Point] = []
    x = y = 0.0
    start: Point = (0.0, 0.0)
    i = 0
    cmd = ""
    while i < len(tokens):
        token = tokens[i]
        if token.isalpha():
            cmd = token
            i += 1
        elif not cmd:
            # Also the landing spot for a number after Z (which clears cmd): Z takes no arguments,
            # so it has no implicit repetition to continue into. Without this the Z branch below
            # would re-run on the same index forever — a hang, not an error, which is the one
            # failure mode a hand-run build script must never have.
            raise ValueError("expected a path command before %r, got a number: %r" % (token, d[:60]))
        elif cmd == "M":
            cmd = "L"   # per spec, numbers following an M continue as an implicit lineto
        if cmd in "mlhvcz":
            raise ValueError("relative command %r is not supported — re-export with absolute "
                             "coordinates" % cmd)
        if cmd == "M":
            x, y = float(tokens[i]), float(tokens[i + 1]); i += 2
            if len(cur) > 1:
                subpaths.append(_closed(cur))
            cur = [(x, y)]
            start = (x, y)
        elif cmd == "L":
            x, y = float(tokens[i]), float(tokens[i + 1]); i += 2
            cur.append((x, y))
        elif cmd == "H":
            x = float(tokens[i]); i += 1
            cur.append((x, y))
        elif cmd == "V":
            y = float(tokens[i]); i += 1
            cur.append((x, y))
        elif cmd == "C":
            c1 = (float(tokens[i]), float(tokens[i + 1]))
            c2 = (float(tokens[i + 2]), float(tokens[i + 3]))
            end = (float(tokens[i + 4]), float(tokens[i + 5]))
            i += 6
            _flatten_cubic((x, y), c1, c2, end, cur)
            x, y = end
        elif cmd == "Z":
            if len(cur) > 1:
                subpaths.append(_closed(cur))
            cur = [start]
            x, y = start
            cmd = ""    # Z has no arguments, so nothing may implicitly repeat it — see above
        else:
            raise ValueError("unsupported path command %r" % cmd)
    if len(cur) > 1:
        subpaths.append(_closed(cur))
    return subpaths


def _closed(sub: List[Point]) -> List[Point]:
    """A subpath with its closing edge present.

    SVG fills an open subpath as if it were closed, but this rasteriser builds edges from
    consecutive points only — so an unclosed subpath is missing the last→first edge and its
    scanline crossings no longer balance, which fills the wrong region rather than erroring. Every
    path in the current master ends with Z, so this is latent; it is exactly the kind of latency
    that surfaces as a corrupt icon on the next re-export."""
    return sub if sub[0] == sub[-1] else sub + [sub[0]]


def load_master(path: Path = MASTER) -> Tuple[float, float, Dict[str, Shape]]:
    text = path.read_text(encoding="utf-8")
    vb = _VIEWBOX.search(text)
    if not vb:
        raise ValueError("%s has no viewBox" % path)
    _, _, vw, vh = (float(v) for v in vb.group(1).split())
    _check_clip(text, vw, vh)
    shapes: Dict[str, Shape] = {}
    order: List[str] = []
    for body in _PATH.findall(text):
        attrs = dict(_ATTR.findall(body))
        ident = attrs.get("id")
        if not ident:
            raise ValueError("every <path> in the master needs a stable id — the generator selects "
                             "by id, not by document order")
        shapes[ident] = Shape(ident, _hex_rgb(attrs["fill"]),
                              float(attrs.get("fill-opacity", 1.0)),
                              parse_path_data(attrs["d"]))
        order.append(ident)
    if not shapes:
        raise ValueError("%s contains no <path>" % path)
    # Painter order matters (the glyph sits over the slash); preserve document order.
    for index, ident in enumerate(order):
        shapes[ident].z = index
    return vw, vh, shapes


_CLIP_RECT = re.compile(r"<clipPath\b[^>]*>\s*<rect\b([^>]*?)/>\s*</clipPath>", re.S)


def _check_clip(text: str, vw: float, vh: float) -> None:
    """This rasteriser has no clipping. Figma wraps its exports in a full-canvas ``<clipPath>``,
    which clips nothing and is safe to ignore — but a clip that actually crops the art would be
    silently dropped, producing an icon that does not match the master. So: accept the no-op form,
    reject anything else rather than guess."""
    if "<clipPath" not in text:
        return
    for body in _CLIP_RECT.findall(text):
        attrs = dict(_ATTR.findall(body))
        if (float(attrs.get("width", 0)) >= vw and float(attrs.get("height", 0)) >= vh
                and float(attrs.get("x", 0)) <= 0 and float(attrs.get("y", 0)) <= 0):
            continue
        raise ValueError("the master has a clipPath that crops the art (%r); this generator does "
                         "not clip — flatten it in the export" % body.strip())
    if not _CLIP_RECT.search(text):
        raise ValueError("the master has a clipPath this generator cannot verify as a no-op; "
                         "flatten it in the export")


# ── rasteriser ───────────────────────────────────────────────────────────────────────────────────
def coverage(subpaths: Sequence[Sequence[Point]], width: int, height: int,
             sx: float, sy: float, ox: float, oy: float, samples: int = 4) -> List[float]:
    """Per-pixel coverage 0..1 of one filled shape, nonzero winding.

    Scanline, not point-sampling: for each of ``samples`` sub-rows per pixel row we intersect the
    edge list once, sort the crossings, and add exact fractional spans. That is O(rows × edges)
    instead of O(pixels × samples² × edges) — the difference between a second and several minutes
    for the 1024px pass, in a script with no C extension under it."""
    edges = []
    for sub in subpaths:
        for (x0, y0), (x1, y1) in zip(sub, sub[1:]):
            px0, py0 = x0 * sx + ox, y0 * sy + oy
            px1, py1 = x1 * sx + ox, y1 * sy + oy
            if py0 == py1:
                continue            # horizontal edges contribute no crossings
            edges.append((py0, py1, px0, px1))
    acc = [0.0] * (width * height)
    if not edges:
        return acc
    step = 1.0 / samples
    weight = 1.0 / samples
    for row in range(height):
        base = row * width
        for s in range(samples):
            yy = row + (s + 0.5) * step
            crossings = []
            for py0, py1, px0, px1 in edges:
                if (py0 <= yy < py1) or (py1 <= yy < py0):
                    t = (yy - py0) / (py1 - py0)
                    crossings.append((px0 + t * (px1 - px0), 1 if py1 > py0 else -1))
            if not crossings:
                continue
            crossings.sort()
            winding = 0
            span_start = 0.0
            for xpos, direction in crossings:
                if winding == 0:
                    span_start = xpos
                winding += direction
                if winding == 0 and xpos > span_start:
                    _add_span(acc, base, width, span_start, xpos, weight)
    return acc


def _add_span(acc: List[float], base: int, width: int, x0: float, x1: float, weight: float) -> None:
    """Add a horizontal span [x0, x1) with fractional coverage at both ends."""
    x0 = max(0.0, x0)
    x1 = min(float(width), x1)
    if x1 <= x0:
        return
    first, last = int(x0), int(math.ceil(x1)) - 1
    if first == last:
        acc[base + first] += (x1 - x0) * weight
        return
    acc[base + first] += (first + 1 - x0) * weight
    for col in range(first + 1, last):
        acc[base + col] += weight
    acc[base + last] += (x1 - last) * weight


def render(shapes: Sequence[Tuple[Shape, Color]], size: int, box: Tuple[float, float, float, float],
           background: Optional[Color] = None, samples: int = 4) -> bytearray:
    """Composite the given (shape, resolved colour) layers into ``size``×``size`` straight-alpha RGBA.

    ``box`` is the user-space rect (x0, y0, x1, y1) mapped into the square, letterboxed to preserve
    aspect. Layers composite in the order given (painter's algorithm)."""
    x0, y0, x1, y1 = box
    scale = min(size / (x1 - x0), size / (y1 - y0))
    ox = (size - (x1 - x0) * scale) / 2 - x0 * scale
    oy = (size - (y1 - y0) * scale) / 2 - y0 * scale

    if background is None:
        dst_r = [0.0] * (size * size)
        dst_g = [0.0] * (size * size)
        dst_b = [0.0] * (size * size)
        dst_a = [0.0] * (size * size)
    else:
        dst_r = [float(background[0])] * (size * size)
        dst_g = [float(background[1])] * (size * size)
        dst_b = [float(background[2])] * (size * size)
        dst_a = [1.0] * (size * size)

    for shape, color in shapes:
        cov = coverage(shape.subpaths, size, size, scale, scale, ox, oy, samples)
        cr, cg, cb = color
        for i, a in enumerate(cov):
            if a <= 0.0:
                continue
            a = 1.0 if a > 1.0 else a
            inv = 1.0 - a
            out_a = a + dst_a[i] * inv
            if out_a <= 0.0:
                continue
            # straight-alpha "over": premultiply, blend, un-premultiply
            dst_r[i] = (cr * a + dst_r[i] * dst_a[i] * inv) / out_a
            dst_g[i] = (cg * a + dst_g[i] * dst_a[i] * inv) / out_a
            dst_b[i] = (cb * a + dst_b[i] * dst_a[i] * inv) / out_a
            dst_a[i] = out_a

    out = bytearray(size * size * 4)
    for i in range(size * size):
        out[i * 4 + 0] = int(dst_r[i] + 0.5)
        out[i * 4 + 1] = int(dst_g[i] + 0.5)
        out[i * 4 + 2] = int(dst_b[i] + 0.5)
        out[i * 4 + 3] = int(dst_a[i] * 255 + 0.5)
    return out


def rounded_tile(rgba: bytearray, size: int, fill: Color,
                 radius_ratio: float = TILE_RADIUS_RATIO,
                 margin_ratio: float = TILE_MARGIN_RATIO) -> bytearray:
    """Composite an RGBA buffer onto a rounded-rect tile (the macOS app-icon convention; the icon
    this replaces was a tile too, so the Dock keeps the shape it had).

    ``margin_ratio`` is the transparent border Apple's grid requires — see TILE_MARGIN_RATIO. The
    tile BODY is what the radius is measured against, so the corner curve stays proportional to the
    shape it rounds rather than to the canvas that shape floats in."""
    body = size * (1 - 2 * margin_ratio)
    radius = body * radius_ratio
    # Two distinct numbers now that the body is smaller than the canvas: distance is measured from
    # the CANVAS centre (the body stays centred, so the margin is symmetric), while the extent it is
    # measured against is the BODY's half-width. Folding them back into one variable is what would
    # silently restore the edge-to-edge tile.
    centre = size / 2.0
    inner = body / 2.0 - radius
    out = bytearray(size * size * 4)
    for y in range(size):
        qy = abs(y + 0.5 - centre) - inner
        for x in range(size):
            qx = abs(x + 0.5 - centre) - inner
            # signed distance to the rounded rect; ±0.5px of it is the antialiased edge
            dist = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius
            tile_a = max(0.0, min(1.0, 0.5 - dist))
            i = (y * size + x) * 4
            fa = rgba[i + 3] / 255.0
            out_a = fa + tile_a * (1 - fa)
            if out_a <= 0:
                continue
            for c in range(3):
                out[i + c] = int((rgba[i + c] * fa + fill[c] * tile_a * (1 - fa)) / out_a + 0.5)
            out[i + 3] = int(out_a * 255 + 0.5)
    return out


def downsample(rgba: bytearray, src: int, dst: int) -> bytearray:
    """Box-filter ``src``→``dst``. Averaging is done on PREMULTIPLIED colour, or transparent pixels
    (whose RGB is meaningless) drag their neighbours toward black and every edge picks up a dark
    fringe."""
    if src == dst:
        return rgba
    factor = src / dst
    out = bytearray(dst * dst * 4)
    for y in range(dst):
        y0, y1 = int(y * factor), max(int(y * factor) + 1, int((y + 1) * factor))
        for x in range(dst):
            x0, x1 = int(x * factor), max(int(x * factor) + 1, int((x + 1) * factor))
            r = g = b = a = 0.0
            n = 0
            for sy in range(y0, y1):
                row = sy * src
                for sx in range(x0, x1):
                    i = (row + sx) * 4
                    alpha = rgba[i + 3] / 255.0
                    r += rgba[i] * alpha
                    g += rgba[i + 1] * alpha
                    b += rgba[i + 2] * alpha
                    a += alpha
                    n += 1
            i = (y * dst + x) * 4
            if a <= 0 or n == 0:
                continue
            out[i + 0] = min(255, int(r / a + 0.5))
            out[i + 1] = min(255, int(g / a + 0.5))
            out[i + 2] = min(255, int(b / a + 0.5))
            out[i + 3] = min(255, int(a / n * 255 + 0.5))
    return out


# ── writers ──────────────────────────────────────────────────────────────────────────────────────
def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def png_bytes(rgba: bytes, size: int, height: Optional[int] = None) -> bytes:
    """``height`` defaults to ``size`` — every icon here is square except the menu-bar image, which
    is height-constrained and free in width because that is the shape of a macOS menu bar."""
    tall = size if height is None else height
    raw = bytearray()
    stride = size * 4
    for y in range(tall):
        raw.append(0)                       # filter type 0 (None)
        raw.extend(rgba[y * stride:(y + 1) * stride])
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, tall, 8, 6, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _png_chunk(b"IEND", b"")
    return png


def write_png(path: Path, rgba: bytes, size: int, height: Optional[int] = None) -> None:
    path.write_bytes(png_bytes(rgba, size, height))


def trim_transparent_rows(rgba: bytes, size: int) -> Tuple[bytes, int]:
    """Drop the fully transparent rows a squared box leaves above and below a wide mark.

    ``bounds`` squares its box so the mark stays centred whichever paths are included, which is
    right for an icon that lands in a square slot. A macOS menu bar is not a square slot: it fixes
    HEIGHT and lets width run, so those empty rows would be charged against the only dimension that
    is scarce, shrinking the mark by a third for nothing.

    Rows are kept whenever any pixel has non-zero alpha — not a threshold — so antialiased edges
    survive intact. Returns the cropped buffer and its new height."""
    stride = size * 4
    rows = [rgba[y * stride:(y + 1) * stride] for y in range(size)]
    keep = [row for row in rows if any(row[3::4])]
    if not keep:
        raise ValueError("nothing to trim — the render is entirely transparent")
    return b"".join(bytes(row) for row in keep), len(keep)


def ico_image(rgba: bytes, size: int) -> bytes:
    """One ICO entry as a classic 32bpp DIB — NOT a PNG-compressed entry.

    ICO has allowed embedded PNG since Vista, and every modern viewer reads it. Tk is not a modern
    viewer: ``wm iconbitmap`` on Windows parses the ICONDIR itself and builds icons from DIB data,
    so a PNG entry is where this silently degrades to "no icon at all" on exactly the platform the
    icon exists for. Uncompressed 256px costs ~270KB; that is the price of it actually working."""
    header = struct.pack("<IiiHHIIiiII",
                         40,            # header size
                         size, size * 2,  # height is doubled: XOR bitmap + AND mask
                         1, 32,         # planes, bits per pixel
                         0,             # BI_RGB, no compression
                         # biSizeImage describes the XOR bitmap only. The AND mask follows it in the
                         # payload but is not part of it, and a reader that uses this field to bound
                         # the colour bitmap would read past its end. (Checked against Pillow, which
                         # writes width*height*4 here.)
                         size * size * 4,
                         0, 0, 0, 0)
    xor = bytearray()
    for y in range(size - 1, -1, -1):        # DIB rows run bottom-up
        row = y * size * 4
        for x in range(size):
            i = row + x * 4
            xor += bytes((rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]))   # BGRA
    # AND mask: all zero = "take the colour from the XOR bitmap", which defers to the alpha channel.
    return header + bytes(xor) + b"\x00" * _mask_bytes(size)


def _mask_bytes(size: int) -> int:
    return ((size + 31) // 32) * 4 * size    # 1bpp, rows padded to 4 bytes


def write_ico(path: Path, images: Sequence[Tuple[int, bytes]]) -> None:
    """``images`` is [(size, rgba)] — one entry per size, largest last is conventional but not required."""
    blobs = [(size, ico_image(rgba, size)) for size, rgba in images]
    offset = 6 + 16 * len(blobs)
    out = bytearray(struct.pack("<HHH", 0, 1, len(blobs)))
    for size, blob in blobs:
        out += struct.pack("<BBBBHHII",
                           size if size < 256 else 0,   # 256 is encoded as 0
                           size if size < 256 else 0,
                           0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for _, blob in blobs:
        out += blob
    path.write_bytes(bytes(out))


# ── icns ─────────────────────────────────────────────────────────────────────────────────────────
# Written here rather than shelled out to ``iconutil``, which is macOS-only. AppIcon.icns is a
# COMMITTED runtime asset — the Dock reads it and nothing builds on a user's machine — so an
# Apple-only writer meant the file could not be regenerated, or measured, from the machine most of
# this repo is edited on. It shipped a full-canvas tile for exactly that long. ~60 lines of stdlib
# buys every platform the ability to rebuild and verify it.
#
# Each OSType is a slot the Dock and Finder ask for by name. The pair below the modern PNG types are
# ARGB chunks, which is what iconutil emits at those sizes; the payloads are otherwise plain PNG.
# Chunk ORDER is not a contract — every reader looks up by type — but ascending size keeps a hexdump
# legible. (``info``, the NSKeyedArchiver blob iconutil adds, is asset-catalog metadata no image
# reader consults, and is not written.)
#
# (ostype, pixels, points) — points is what BRACE_CUTOFF is read against; see main().
ICNS_SLOTS = (("ic04", 16, 16), ("ic11", 32, 16), ("ic05", 32, 32), ("ic12", 64, 32),
              ("ic07", 128, 128), ("ic13", 256, 128), ("ic08", 256, 256), ("ic14", 512, 256),
              ("ic09", 512, 512), ("ic10", 1024, 512))
# The two that must be ARGB rather than PNG, matching iconutil.
ICNS_ARGB = ("ic04", "ic05")


def _packbits(data: bytes) -> bytes:
    """Apple's PackBits variant, as the ARGB and is32 chunks use it: a control byte below 0x80 means
    a literal run of ``control + 1`` bytes, 0x80 and up a repeat of ``control - 0x7D`` (3..130).

    Note the repeat encoding is NOT stock PackBits — the bias is 3, not 2 — which is the detail a
    reader silently mis-decodes into a smeared icon. Verified in both directions: the matching
    decoder in installer/tests/test_window_icon.py consumes iconutil's own output byte-exactly, and
    round-trips this encoder's."""
    out, i, n = bytearray(), 0, len(data)
    while i < n:
        run = 1
        while run < 130 and i + run < n and data[i + run] == data[i]:
            run += 1
        if run >= 3:
            out.append(0x7D + run)
            out.append(data[i])
            i += run
            continue
        # No worthwhile repeat here: emit literals up to the next run of 3, capped at 128.
        start = i
        i += 1
        while i < n and i - start < 128:
            if i + 2 < n and data[i] == data[i + 1] == data[i + 2]:
                break
            i += 1
        out.append(i - start - 1)
        out.extend(data[start:i])
    return bytes(out)


def argb_chunk(rgba: bytes, size: int) -> bytes:
    """An ARGB chunk: the magic, then the four channels de-interleaved and each PackBits-compressed.
    Straight alpha, exactly as the buffer holds it — matching what iconutil wrote at these sizes."""
    planes = [bytes(rgba[offset::4]) for offset in (3, 0, 1, 2)]     # A, R, G, B
    for name, plane in zip("ARGB", planes):
        if len(plane) != size * size:
            raise ValueError("channel %s has %d of %d pixels" % (name, len(plane), size * size))
    return b"ARGB" + b"".join(_packbits(plane) for plane in planes)


def write_icns(path: Path, images: Dict[str, bytes]) -> None:
    """``images`` is {ostype: rgba} and must cover every slot in ICNS_SLOTS.

    Keyed by SLOT and not by pixel size, because two slots can share a size and not the artwork:
    ic11 and ic05 are both 32px, but ic11 is a 16-POINT tile (braces dropped) and ic05 a 32-point one
    (braces kept). Keying by size would put one of them in the other's slot.

    Fail-closed on a missing or mis-sized buffer rather than skipping the slot: a Dock that cannot
    find the representation for the tile it is drawing scales a neighbour up instead, which looks
    soft rather than absent and is easy to miss by eye."""
    chunks = bytearray()
    for ostype, pixels, _points in ICNS_SLOTS:
        rgba = images.get(ostype)
        if rgba is None:
            raise ValueError("no image for icns slot %s (%dpx)" % (ostype, pixels))
        if len(rgba) != pixels * pixels * 4:
            raise ValueError("icns slot %s wants %dpx RGBA (%d bytes), got %d"
                             % (ostype, pixels, pixels * pixels * 4, len(rgba)))
        payload = (argb_chunk(rgba, pixels) if ostype in ICNS_ARGB
                   else png_bytes(rgba, pixels))
        chunks += struct.pack(">4sI", ostype.encode("ascii"), len(payload) + 8) + payload
    # The container's own length counts its 8-byte header too, and a reader that trusts it will run
    # off the end of a file that disagrees.
    path.write_bytes(struct.pack(">4sI", b"icns", len(chunks) + 8) + bytes(chunks))


# ── assembly ─────────────────────────────────────────────────────────────────────────────────────
def layers(shapes: Dict[str, Shape], full_mark: bool) -> List[Tuple[Shape, Color]]:
    """The paint list in document order, with any non-opaque fill baked. See the module docstring.

    ``full_mark`` picks between the two assemblies an ICO is allowed to carry, since its entries are
    independent images rather than one bitmap resized:

    * True  — the mark as designed: everything except the small-size stand-ins.
    * False — the small variant: the stand-ins alone when the master supplies them, otherwise the
      mark minus its framing.

    The fallback is what makes the stand-in optional. A master that never draws one still gets a
    correct small icon, which is the behaviour every earlier master relied on."""
    stand_ins = [i for i in SMALL_ONLY_IDS if i in shapes]
    if full_mark:
        keep = lambda s: s.id not in SMALL_ONLY_IDS            # noqa: E731
    elif stand_ins:
        keep = lambda s: s.id in SMALL_ONLY_IDS                # noqa: E731
    else:
        keep = lambda s: s.id not in FRAMING_IDS               # noqa: E731
    picked = sorted((s for s in shapes.values() if keep(s)), key=lambda s: s.z)
    if not picked:
        raise ValueError("nothing left to paint — every path in the master is named in "
                         "FRAMING_IDS/SMALL_ONLY_IDS")
    out: List[Tuple[Shape, Color]] = []
    for shape in picked:
        if shape.opacity >= 1.0:
            out.append((shape, shape.fill))
        else:
            blend = tuple(int(shape.fill[c] * shape.opacity + 255 * (1 - shape.opacity) + 0.5)
                          for c in range(3))
            out.append((shape, blend))      # type: ignore[arg-type]
    return out


def bounds(paint: Sequence[Tuple[Shape, Color]], margin: float = 0.03) -> Tuple[float, float, float, float]:
    """Tight user-space box around the paint list, squared with a small even margin. Squaring here
    (rather than letterboxing at render time) keeps the mark centred whichever paths are included."""
    xs = [p[0] for shape, _ in paint for sub in shape.subpaths for p in sub]
    ys = [p[1] for shape, _ in paint for sub in shape.subpaths for p in sub]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    side = max(x1 - x0, y1 - y0) * (1 + 2 * margin)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return (cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)


def variant(shapes: Dict[str, Shape], size: int, full_mark: bool, *,
            supersample: int = 4, background: Optional[Color] = None) -> bytes:
    """Render one square icon. Small sizes are rendered large and box-downsampled: the scanline
    filler antialiases edges, but only supersampling recovers detail thinner than a pixel."""
    paint = layers(shapes, full_mark)
    box = bounds(paint)
    hi = size * supersample
    # Sub-row sampling on top of supersampling has sharply diminishing returns, and this is pure
    # Python — at 512px and up the two together cost minutes for a difference nobody can see.
    rgba = render(paint, hi, box, background=background, samples=4 if hi <= 256 else 2)
    return bytes(downsample(rgba, hi, size))


def main(argv: List[str]) -> int:
    vw, vh, shapes = load_master()
    print("master %s (%g×%g, %d paths)" % (MASTER.name, vw, vh, len(shapes)))

    # 1. Windows window icon — the hybrid assembly.
    entries = []
    for size in ICO_SIZES:
        full_mark = size >= BRACE_CUTOFF
        # Big sizes are already detailed; supersampling them 4× is minutes of pure-Python work for
        # no visible gain, so taper it.
        ss = 4 if size <= 64 else 2
        entries.append((size, variant(shapes, size, full_mark, supersample=ss)))
        print("  ico %3dpx  %s" % (size, "full mark" if full_mark else "small variant"))
    write_ico(HERE / "logo.ico", entries)
    print("wrote %s (%d bytes)" % (HERE / "logo.ico", (HERE / "logo.ico").stat().st_size))

    # 1b. PNG fallback for Tk's `iconphoto`, which is what non-Windows platforms take. (Aqua ignores
    #     it entirely — an app's icon there comes from the bundle — but X11 uses it, and asking for
    #     it costs nothing.)
    by_size = dict(entries)
    for size in (64, 256):
        write_png(HERE / ("logo-%d.png" % size), by_size[size], size)
        print("wrote %s" % (HERE / ("logo-%d.png" % size)))

    # 2. macOS menu-bar icon. 22px — the braces are long gone at that size.
    #    mkdir first: this runs after logo.ico is already written, so a missing directory would
    #    abort half-way and leave a partially regenerated asset set — which `git status` cannot
    #    distinguish from a complete one.
    # BESIDE THE STOPPER BINARY, not in desktop/macos/. The Swift panel asks for it through
    # `Bundle.main.url(forResource:)`, and for a bare Mach-O executable — which is what ships —
    # Bundle.main resolves to the directory holding the executable, with no parent search. One
    # directory out is as good as absent: it silently falls back to drawing a lettered badge.
    macos = REPO / "desktop" / "macos" / "bin"
    macos.mkdir(parents=True, exist_ok=True)
    # The FULL mark here, braces included, and wider than it is tall. A menu bar fixes height and
    # lets width run, so the framing costs nothing in the dimension that is scarce — the opposite of
    # the square icon slots above, where squaring a 1.5:1 mark is what made the braces too expensive
    # to keep at small sizes. Rendered square, then trimmed back to the ink.
    # ONE file, at roughly twice the ~18pt it is drawn at, because both readers load it with an
    # explicit path (NSImage(contentsOf:) / initWithContentsOfFile_) and neither consults the @2x
    # naming convention — so a second file would be bytes nobody reads, and the callers size it in
    # POINTS from its own aspect, which keeps it crisp on Retina from this one bitmap.
    square = round(MENU_BAR_HEIGHT_PX * MENU_BAR_ASPECT)
    rgba, height = trim_transparent_rows(variant(shapes, square, True), square)
    write_png(macos / "stopper_icon.png", rgba, square, height)
    print("wrote %s (%dx%d)" % (macos / "stopper_icon.png", square, height))

    # 3. macOS app icon. A white rounded tile, not bare transparency: the Dock expects a tile (the
    #    icon this replaces was one), and white is the ground the mark was drawn for. The tile is
    #    inset to Apple's 824/1024 grid — drawn edge-to-edge it renders 1.24x oversized in the Dock,
    #    which is the defect TILE_BODY_RATIO documents.
    #    Written by write_icns rather than iconutil so this runs on every platform; the 1024px render
    #    is the slow part, hence --no-icns for a quick pass over the Windows assets alone.
    if "--no-icns" in argv:
        return 0
    # BRACE_CUTOFF is a legibility threshold, so on macOS it has to be read against POINTS, not
    # pixels: ic11 is 32 pixels drawn in a 16-point slot, and at 16 points the braces are the same
    # two smudges they are on a non-Retina screen — just sharper ones. Keying it on pixels would put
    # braces on Retina and none on everything else, at the same apparent size. Windows has no
    # equivalent: there a 32px entry is 32px of screen.
    #
    # Cached on (pixels, full_mark) rather than per slot: ic09/ic14 are both 512px full-mark tiles
    # and ic08/ic13 both 256px, so this halves the number of renders — and the 512px one is seconds.
    cache: Dict[Tuple[int, bool], bytes] = {}
    tiles: Dict[str, bytes] = {}
    for ostype, size, points in ICNS_SLOTS:
        key = (size, points >= BRACE_CUTOFF)
        if key not in cache:
            ss = 4 if size <= 64 else (2 if size <= 256 else 1)
            mark = variant(shapes, size, key[1], supersample=ss)
            cache[key] = bytes(rounded_tile(bytearray(_inset(mark, size)), size, WHITE))
            print("  icns %4dpx  %s" % (size, "full mark" if key[1] else "braces dropped"))
        tiles[ostype] = cache[key]
    write_icns(HERE / "AppIcon.icns", tiles)
    print("wrote %s (%d bytes)" % (HERE / "AppIcon.icns",
                                   (HERE / "AppIcon.icns").stat().st_size))
    return 0


def _inset(rgba: bytes, size: int, ratio: float = MARK_INSET_RATIO,
           span_ratio: float = TILE_BODY_RATIO) -> bytearray:
    """Shrink the mark, centred, so the tile it lands on has breathing room around it.

    ``ratio`` is measured against ``span_ratio`` of the canvas — i.e. against the tile BODY, not the
    canvas — so the mark keeps its proportion inside the tile. Rebasing this is what stops Apple's
    100px margin from being taken out of the tile's own padding and leaving the mark oversized
    inside a correctly-sized tile."""
    inner = max(1, int(size * span_ratio * (1 - 2 * ratio)))
    # Match the canvas's parity, so (size - inner) is even and the offset below splits it exactly.
    # Otherwise the truncating // leaves the mark a pixel left of and above centre inside a tile that
    # is itself symmetric — worst at 16px, where one pixel is ~11% of a 9px mark. Costs at most 1px
    # of mark, and only where it would otherwise be off-centre.
    if inner != size and (size - inner) % 2:
        inner -= 1
    small = downsample(bytearray(rgba), size, inner) if inner != size else bytearray(rgba)
    out = bytearray(size * size * 4)
    off = (size - inner) // 2
    for y in range(inner):
        src = y * inner * 4
        dst = ((y + off) * size + off) * 4
        out[dst:dst + inner * 4] = small[src:src + inner * 4]
    return out


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
