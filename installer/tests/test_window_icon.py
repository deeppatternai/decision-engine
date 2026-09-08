"""The shipped window icon: is the committed .ico actually loadable by the thing that loads it, and
does the wiring stay out of the way when it cannot be?

The parts a Windows box would tell us — that the title bar really shows this — cannot be checked
from a POSIX CI host, so these tests check the two things that CAN be decided here: the byte format
Tk's Windows ICO reader requires, and that every failure path in ``apply_window_icon`` is silent.
"""

from __future__ import annotations

import contextlib
import importlib
import struct
import subprocess
import sys
import textwrap
import unittest
import unittest.mock
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from client import tk_icon  # noqa: E402

ICONS = REPO / "desktop" / "icons"
ICO = ICONS / "logo.ico"
DECLARED_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
BRACE_CUTOFF = 32   # 32px is SM_CXICON at 100% DPI — the taskbar button must carry the full mark
BRAND = (11, 133, 101)

# The macOS 11+ app-icon grid: a 1024x1024 canvas whose rounded-rect body is 824x824, i.e. 100px of
# transparent margin per side. The Dock scales the whole canvas into its tile slot, so a body drawn
# edge-to-edge fills the slot a conformant icon fills to 80.47% — it renders 1024/824 = 1.24x
# oversized next to every other app. That is the defect these assertions exist to catch.
TILE_BODY_RATIO = 824 / 1024
# Slack on the measured body, as a fraction of the canvas, plus a flat allowance for the antialiased
# edge. Both are needed and they cover different things:
#
#   * the proportional part absorbs the rounding of a fractional margin — a 16px entry cannot place
#     Apple's 1.5625px margin on a pixel boundary;
#   * the flat 2px is the squircle's antialiased edge, one partial pixel per side. `ink_bbox` counts
#     any pixel above alpha 16, so at 16px the edge pixel (alpha 112) is inside the measurement and
#     the body reads 14px against an ideal 12.875. At 256px and up the margin lands on whole pixels
#     and the measurement is exact — 206 of 256, 824 of 1024.
#
# Neither admits the defect this pins against: at 1024 the pair allows 814..834 against a full-canvas
# tile of 1024, and at 16px 10.9..14.9 against 16.
TILE_RATIO_SLACK = 8 / 1024
TILE_ANTIALIAS_SLACK_PX = 2


def ico_entries(path: Path):
    """(width, height, bpp, image_blob) per ICONDIRENTRY, with the directory checked as we go."""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and kind == 1, "not an ICO directory"
    out = []
    for index in range(count):
        head = 6 + 16 * index
        w, h, _colors, _r, _planes, bpp, nbytes, offset = struct.unpack(
            "<BBBBHHII", data[head:head + 16])
        assert offset + nbytes <= len(data), "entry %d runs past EOF" % index
        out.append((w or 256, h or 256, bpp, data[offset:offset + nbytes]))
    return out


def dib_pixels(blob: bytes, size: int):
    """Decode a 32bpp BI_RGB icon image to rows of (r, g, b, a). DIB rows are stored bottom-up."""
    stride = size * 4
    rows = []
    for r in range(size):
        base = 40 + (size - 1 - r) * stride
        row = []
        for x in range(size):
            b, g, red, a = blob[base + x * 4:base + x * 4 + 4]
            row.append((red, g, b, a))
        rows.append(row)
    return rows


def ink_bbox(rows):
    xs, ys = [], []
    for y, row in enumerate(rows):
        for x, (_r, _g, _b, a) in enumerate(row):
            if a > 16:
                xs.append(x)
                ys.append(y)
    assert xs, "image is entirely transparent"
    return min(xs), min(ys), max(xs), max(ys)


# ── icns readers ─────────────────────────────────────────────────────────────────────────────────
# Deliberately NOT the generator's own code. These are written from the format spec so a bug in the
# writer cannot be cancelled out by the same bug in the checker, and they were validated against the
# container iconutil itself produced before the tile geometry was fixed. Hand-decoding an asset is
# the existing habit in this file (`ico_entries` / `dib_pixels` above), not a new one.

def icns_entries(path: Path):
    """``{OSType: payload}`` for every chunk, with the container's own length field checked."""
    data = path.read_bytes()
    magic, declared = struct.unpack(">4sI", data[:8])
    assert magic == b"icns", "not an icns container"
    assert declared == len(data), "the icns header length disagrees with the file"
    out, offset = {}, 8
    while offset < declared:
        kind, length = struct.unpack(">4sI", data[offset:offset + 8])
        assert 8 <= length <= declared - offset, "chunk %r has an impossible length" % kind
        out[kind.decode("ascii")] = data[offset + 8:offset + length]
        offset += length
    return out


def _unfilter_png(raw: bytes, width: int, height: int, channels: int) -> bytes:
    """Reverse PNG's per-row filters. Only what a colour-type-6 8-bit image can carry (0-4)."""
    stride = width * channels
    out, prev, pos = bytearray(), bytearray(stride), 0
    for _y in range(height):
        kind = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = prev[i]
            upleft = prev[i - channels] if i >= channels else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                guess = left + up - upleft
                da, db, dc = abs(guess - left), abs(guess - up), abs(guess - upleft)
                near = left if (da <= db and da <= dc) else (up if db <= dc else upleft)
                line[i] = (line[i] + near) & 0xFF
            elif kind:
                raise AssertionError("unknown PNG filter %d" % kind)
        out.extend(line)
        prev = line
    return bytes(out)


def png_rows(blob: bytes):
    """Rows of (r, g, b, a) from an 8-bit RGBA PNG — the form every large icns entry takes."""
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    offset, idat, header = 8, bytearray(), None
    while offset < len(blob):
        length, kind = struct.unpack(">I4s", blob[offset:offset + 8])
        payload = blob[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBB", payload[:10])
        elif kind == b"IDAT":
            idat.extend(payload)
        offset += 12 + length
    width, height, depth, colour = header
    assert (depth, colour) == (8, 6), "expected 8-bit RGBA, got depth=%d colour=%d" % (depth, colour)
    flat = _unfilter_png(zlib.decompress(bytes(idat)), width, height, 4)
    return [[tuple(flat[(y * width + x) * 4:(y * width + x) * 4 + 4])
             for x in range(width)] for y in range(height)]


def rle_decode(data: bytes, expect: int):
    """Apple's PackBits variant, as used by the ARGB/is32 chunks: a control byte below 0x80 is a
    literal run of ``control + 1`` bytes, 0x80 and up a repeat of ``control - 0x7D`` (so 3..130).
    Returns the bytes and how many were consumed — the four channels are concatenated, so the
    caller needs the offset to find the next one."""
    out, pos = bytearray(), 0
    while pos < len(data) and len(out) < expect:
        control = data[pos]
        pos += 1
        if control & 0x80:
            out.extend(bytes([data[pos]]) * (control - 0x7D))
            pos += 1
        else:
            run = control + 1
            out.extend(data[pos:pos + run])
            pos += run
    return bytes(out), pos


def argb_rows(blob: bytes, size: int):
    """Rows of (r, g, b, a) from an ARGB chunk — the form the 16px and 32px icns entries take."""
    assert blob[:4] == b"ARGB", "not an ARGB chunk"
    pixels, body, offset, planes = size * size, blob[4:], 0, []
    for name in "ARGB":
        plane, used = rle_decode(body[offset:], pixels)
        assert len(plane) == pixels, "ARGB channel %s decoded %d of %d" % (name, len(plane), pixels)
        planes.append(plane)
        offset += used
    assert offset == len(body), "ARGB chunk has %d trailing bytes" % (len(body) - offset)
    alpha, red, green, blue = planes
    return [[(red[y * size + x], green[y * size + x], blue[y * size + x], alpha[y * size + x])
             for x in range(size)] for y in range(size)]


class IcoFormat(unittest.TestCase):
    def test_ships_every_declared_size(self):
        sizes = sorted(w for w, _h, _bpp, _blob in ico_entries(ICO))
        self.assertEqual(sizes, sorted(DECLARED_SIZES))

    def test_entries_are_dib_not_png(self):
        """A PNG-compressed entry is legal ICO since Vista and unreadable by Tk's own ICO parser —
        which would leave Windows, the only platform this file exists for, with no icon at all."""
        for w, _h, _bpp, blob in ico_entries(ICO):
            self.assertNotEqual(blob[:8], b"\x89PNG\r\n\x1a\n", "%dpx entry is a PNG" % w)
            header_size, width, height, planes, bpp, compression = struct.unpack(
                "<Iii HHI", blob[:20])
            self.assertEqual(header_size, 40, "%dpx: not a BITMAPINFOHEADER" % w)
            self.assertEqual(width, w)
            self.assertEqual(height, 2 * w, "%dpx: height must cover XOR bitmap + AND mask" % w)
            self.assertEqual((planes, bpp, compression), (1, 32, 0))

    def test_entry_payload_length_matches_its_geometry(self):
        for w, _h, _bpp, blob in ico_entries(ICO):
            mask = ((w + 31) // 32) * 4 * w
            self.assertEqual(len(blob), 40 + w * w * 4 + mask, "%dpx entry is truncated" % w)

    def test_bisizeimage_covers_the_xor_bitmap_only(self):
        """The AND mask follows the colour bitmap in the payload but is not part of it. A reader
        that uses this field to bound the XOR bitmap would run past its end."""
        for w, _h, _bpp, blob in ico_entries(ICO):
            bi_size_image = struct.unpack("<I", blob[20:24])[0]
            self.assertEqual(bi_size_image, w * w * 4,
                             "%dpx: biSizeImage must not include the AND mask" % w)


class HybridAssembly(unittest.TestCase):
    """Below the cutoff an entry carries its own near-square artwork; 32px and up carry the framed
    mark. Aspect ratio tells the two apart: the stand-in is 1.0:1, the full mark about 1.5:1.

    The thresholds sit well inside that gap rather than hugging it. They were 1.35/1.4 against a
    1.71:1 mark; the mark is now 1.5:1 and rounding at small pixel counts put the 48px entry at
    1.44 — four hundredths from failing a test it was never meant to be near."""

    SQUARISH, FRAMED = 1.2, 1.3

    def _aspect(self, size):
        blob = next(b for w, _h, _bpp, b in ico_entries(ICO) if w == size)
        x0, y0, x1, y1 = ink_bbox(dib_pixels(blob, size))
        return (x1 - x0 + 1) / (y1 - y0 + 1)

    def test_the_taskbar_size_carries_the_braces(self):
        """32px hard-coded, deliberately NOT derived from BRACE_CUTOFF. The cutoff shipped at 48
        once, which is above SM_CXICON's 100%-DPI value — so the taskbar button, the one icon most
        users ever see, lost the framing while every size-derived check stayed green. A test that
        mirrors the constant cannot catch the constant being wrong."""
        self.assertGreater(self._aspect(32), self.FRAMED,
                           "32px is the taskbar button at 100% DPI and it lost the braces")

    def test_small_sizes_are_near_square(self):
        for size in (s for s in DECLARED_SIZES if s < BRACE_CUTOFF):
            self.assertLess(self._aspect(size), self.SQUARISH,
                            "%dpx still carries the framing — it wastes the box" % size)

    def test_large_sizes_carry_the_full_mark(self):
        for size in (s for s in DECLARED_SIZES if s >= BRACE_CUTOFF):
            self.assertGreater(self._aspect(size), self.FRAMED,
                               "%dpx lost the braces — the full mark is about 1.5:1" % size)

    def test_the_mark_is_opaque_in_its_brand_colour(self):
        """Fully opaque brand-colour pixels must survive to the smallest size. Anti-aliasing alone
        would leave only partial alpha — that is what a mark rendered too thin to hold its own
        stroke looks like, and at 16px this one is line art with counters to keep open."""
        for size in (16, 64):
            blob = next(b for w, _h, _bpp, b in ico_entries(ICO) if w == size)
            solid = [p for row in dib_pixels(blob, size) for p in row
                     if p[:3] == BRAND and p[3] == 255]
            self.assertTrue(solid, "%dpx has no fully opaque brand-colour pixel" % size)


class ApplyTaskbarIcon(unittest.TestCase):
    """WM_SETICON against a raw handle — the only route into the frameless pywebview popup, whose
    Windows backend takes no icon argument."""

    @staticmethod
    def fake_user32(icon=0x1234):
        user32 = unittest.mock.Mock()
        user32.GetSystemMetrics.side_effect = lambda metric: {11: 32, 49: 16}[metric]
        user32.LoadImageW.return_value = icon
        return user32

    @contextlib.contextmanager
    def on_windows(self, user32):
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Windows"), \
                unittest.mock.patch("ctypes.windll", unittest.mock.Mock(user32=user32),
                                    create=True):
            yield

    def test_both_icon_sizes_reach_the_window(self):
        """The taskbar button reads ICON_BIG and the Alt-Tab card ICON_SMALL; a window with only
        one set falls back to the executable's icon — Python's — for the other."""
        user32 = self.fake_user32()
        with self.on_windows(user32):
            self.assertTrue(tk_icon.apply_taskbar_icon(0xABCD))

        self.assertEqual([call.args[3] for call in user32.LoadImageW.call_args_list], [32, 16])
        self.assertEqual({call.args[1] for call in user32.LoadImageW.call_args_list},
                         {str(tk_icon.ICO_PATH)})
        sent = user32.SendMessageW.call_args_list
        self.assertEqual([call.args[0].value for call in sent], [0xABCD, 0xABCD])
        self.assertEqual([call.args[1] for call in sent], [0x0080, 0x0080])   # WM_SETICON
        self.assertEqual([call.args[2] for call in sent], [1, 0])             # BIG, then SMALL
        self.assertEqual({call.args[3] for call in sent}, {0x1234})

    def test_an_unloadable_icon_sends_nothing(self):
        """LoadImageW answers NULL on failure. Passing that on sets the window's icon to nothing at
        all, which is worse than leaving the inherited one alone."""
        user32 = self.fake_user32(icon=0)
        with self.on_windows(user32):
            self.assertFalse(tk_icon.apply_taskbar_icon(0xABCD))
        user32.SendMessageW.assert_not_called()

    def test_no_handle_is_not_an_error(self):
        """pywebview exposes no handle on some backends; the popup opens anyway, icon-less."""
        user32 = self.fake_user32()
        with self.on_windows(user32):
            self.assertFalse(tk_icon.apply_taskbar_icon(0))
        user32.LoadImageW.assert_not_called()

    def test_off_windows_is_a_no_op(self):
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Darwin"):
            self.assertFalse(tk_icon.apply_taskbar_icon(0xABCD))

    def test_a_missing_user32_never_escapes(self):
        """Same contract as the rest of this module: the popup is the product, the icon is not."""
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Windows"), \
                unittest.mock.patch("ctypes.windll", object(), create=True):
            self.assertFalse(tk_icon.apply_taskbar_icon(0xABCD))


class _NoApplicationAppKit:
    """An AppKit whose ``NSApp()`` is nil — i.e. nothing has created the application yet."""

    @staticmethod
    def NSApp():
        return None

    class NSImage:            # never reached: the nil check comes first, and that is the assertion
        pass


class _FakeProcessInfo:
    def __init__(self):
        self.names = []

    def setProcessName_(self, name):
        self.names.append(name)


class _FakeFoundation:
    def __init__(self):
        self.process_info = _FakeProcessInfo()

        class NSProcessInfo:
            @staticmethod
            def processInfo():
                return self.process_info

        self.NSProcessInfo = NSProcessInfo


class ApplyDockIcon(unittest.TestCase):
    """The macOS half of the same defect the AppUserModelID fixed on Windows: a process that shows
    a Dock tile and never sets one is drawn as the interpreter that started it."""

    def test_off_macos_is_a_no_op(self):
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Windows"):
            self.assertFalse(tk_icon.apply_dock_icon())

    def test_a_missing_icns_is_silent(self):
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Darwin"), \
                unittest.mock.patch.object(tk_icon, "ICNS_PATH", ICONS / "does-not-exist.icns"):
            self.assertFalse(tk_icon.apply_dock_icon())

    def test_absent_pyobjc_never_escapes(self):
        """Windows and Linux have no AppKit at all, and a bare Tk caller on macOS may not either.
        This runs immediately before the process's only window is built."""
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Darwin"), \
                unittest.mock.patch.dict(sys.modules, {"AppKit": None}):
            self.assertFalse(tk_icon.apply_dock_icon())

    def test_dock_app_name_is_set_to_the_localized_window_title(self):
        """macOS Dock hover text reads the process/app name, not the frameless window title."""
        fake_foundation = _FakeFoundation()
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Darwin"), \
                unittest.mock.patch.dict(sys.modules, {"Foundation": fake_foundation}):
            self.assertTrue(tk_icon.apply_dock_app_name("Decision Engine - 图解"))
        self.assertEqual(fake_foundation.process_info.names, ["Decision Engine - 图解"])

    def test_blank_dock_app_name_is_not_applied(self):
        fake_foundation = _FakeFoundation()
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Darwin"), \
                unittest.mock.patch.dict(sys.modules, {"Foundation": fake_foundation}):
            self.assertFalse(tk_icon.apply_dock_app_name("   "))
        self.assertEqual(fake_foundation.process_info.names, [])

    def test_it_refuses_to_create_the_application_itself(self):
        """The whole shape of the fix. Reading NSApp() instead of sharedApplication() is what keeps
        this call from being the one that instantiates a plain NSApplication — which is fatal, not
        untidy: Tk installs its own subclass and Tk_Init sends it -_setup:, so a plain one already
        in place aborts the process. False here is the correct answer, not a failure."""
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Darwin"), \
                unittest.mock.patch.dict(sys.modules, {"AppKit": _NoApplicationAppKit()}):
            self.assertFalse(tk_icon.apply_dock_icon())

    @unittest.skipUnless(sys.platform == "darwin", "needs a real AppKit")
    def test_the_dock_tile_really_changes_on_macos(self):
        """The one icon claim in this repo that can be checked rather than reasoned about — the
        Windows ones cannot be, from here. Sets the tile and reads it straight back off the live
        NSApplication.

        In a subprocess because proving this needs an NSApplication to exist, and creating a plain
        one is exactly what must never leak into a process that later builds a Tk window — which
        this suite does. Isolation here is the property under test, not tidiness."""
        probe = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            from AppKit import NSApplication
            from client import tk_icon
            app = NSApplication.sharedApplication()
            # Read the size out to an int NOW: applicationIconImage() returns a live reference and
            # setApplicationIconImage_ mutates that same object, so a retained `before` reports the
            # new icon and comparing the two would compare an object with itself and always pass.
            before = app.applicationIconImage()
            was = max((r.pixelsWide() for r in before.representations()), default=0) if before else 0
            assert tk_icon.apply_dock_icon(), "apply_dock_icon returned False"
            after = app.applicationIconImage()
            assert after is not None, "no Dock tile at all after setting one"
            now = max(r.pixelsWide() for r in after.representations())
            print("%%d %%d" %% (was, now))
        """) % str(REPO)
        done = subprocess.run([sys.executable, "-c", probe], cwd=REPO,
                              capture_output=True, text=True, timeout=60)
        if done.returncode and "No module named" in done.stderr:
            self.skipTest("pyobjc is not installed for %s" % sys.executable)
        self.assertEqual(done.returncode, 0, done.stderr)
        was, now = (int(v) for v in done.stdout.split())
        self.assertGreaterEqual(now, 512, "the Dock tile has no high-resolution representation")
        self.assertNotEqual(now, was, "the tile did not change — the default may still be showing")

    @unittest.skipUnless(sys.platform == "darwin", "needs a real AppKit and a real Tk")
    def test_it_does_not_abort_a_tk_that_starts_afterwards(self):
        """The regression. The first version of this called NSApplication.sharedApplication(), and
        panel.py called it one line BEFORE tk.Tk() — so every macOS launch of the stop panel died
        on '-[NSApplication _setup:]: unrecognized selector' before a window ever appeared. The
        suite only surfaced it as an abort partway through an otherwise green run.

        Subprocess because the failure mode is SIGABRT: nothing catchable is raised, so an
        in-process version would take the whole run down with it instead of failing one test."""
        probe = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            from client import tk_icon
            tk_icon.apply_dock_icon()      # must not create an NSApplication
            import tkinter
            root = tkinter.Tk()            # aborts here if it did
            tk_icon.apply_dock_icon()      # the supported ordering; must work
            root.destroy()
            print("ok")
        """) % str(REPO)
        done = subprocess.run([sys.executable, "-c", probe], cwd=REPO,
                              capture_output=True, text=True, timeout=60)
        if done.returncode and "No module named" in done.stderr:
            self.skipTest("pyobjc or tkinter is not installed for %s" % sys.executable)
        self.assertNotEqual(done.returncode, -6, "Tk aborted — a plain NSApplication got there first")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.strip(), "ok")


class ApplyWindowIcon(unittest.TestCase):
    class FakeRoot:
        def __init__(self):
            self.calls = []

        def iconbitmap(self, **kwargs):
            self.calls.append(("iconbitmap", kwargs))

        def iconphoto(self, *args):
            self.calls.append(("iconphoto", args))

    def test_windows_sets_the_app_wide_default(self):
        """`default=` is what makes simpledialog/messagebox children inherit the icon."""
        root = self.FakeRoot()
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Windows"):
            self.assertTrue(tk_icon.apply_window_icon(root))
        self.assertEqual(root.calls[0][0], "iconbitmap")
        self.assertEqual(root.calls[0][1]["default"], str(tk_icon.ICO_PATH))

    def test_missing_asset_is_silent(self):
        root = self.FakeRoot()
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Windows"), \
                unittest.mock.patch.object(tk_icon, "ICO_PATH", ICONS / "does-not-exist.ico"):
            self.assertFalse(tk_icon.apply_window_icon(root))
        self.assertEqual(root.calls, [])

    def test_a_raising_asset_probe_never_escapes(self):
        """icon_paths() touches the filesystem. Path.exists() swallows OSError today, but the
        contract this function advertises must not depend on that staying true."""
        root = self.FakeRoot()
        with unittest.mock.patch.object(tk_icon, "icon_paths", side_effect=OSError("I/O error")):
            self.assertFalse(tk_icon.apply_window_icon(root))
        self.assertEqual(root.calls, [])

    def test_a_raising_tk_never_escapes(self):
        """A window that opens without an icon beats a window that does not open."""
        class Exploding(self.FakeRoot):
            def iconbitmap(self, **kwargs):
                raise RuntimeError("TclError: bitmap not defined")

        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Windows"):
            self.assertFalse(tk_icon.apply_window_icon(Exploding()))

    def test_non_windows_takes_the_png_path(self):
        """macOS ignores it (no title-bar icon exists there) and X11 uses it; neither may raise."""
        root = self.FakeRoot()
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Linux"):
            tk_icon.apply_window_icon(root)   # PhotoImage needs a real Tk; the guard must absorb it
        self.assertTrue(all(call[0] != "iconbitmap" for call in root.calls))

    def test_app_identity_is_claimed_only_on_windows(self):
        """The taskbar takes its button icon from the process AppUserModelID, not from the window —
        a bare pythonw.exe inherits Python's, which is why the taskbar kept showing Python's logo
        while the title bar was already correct. Everywhere else this is a no-op."""
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Darwin"):
            self.assertFalse(tk_icon.claim_app_identity())

    def test_app_identity_never_raises_without_shell32(self):
        """ctypes.windll does not exist off Windows; a broken lookup must not reach the caller,
        which calls this immediately before constructing its only window."""
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Windows"), \
                unittest.mock.patch("ctypes.windll", object(), create=True):
            self.assertFalse(tk_icon.claim_app_identity())

    def test_app_identity_hands_shell32_our_id(self):
        """The success path — the one the whole AUMID change exists for. Without this, deleting the
        SetCurrentProcessExplicitAppUserModelID call or passing the wrong id leaves the suite green
        while the taskbar goes back to showing Python's logo."""
        shell32 = unittest.mock.Mock()
        with unittest.mock.patch.object(tk_icon.platform, "system", return_value="Windows"), \
                unittest.mock.patch("ctypes.windll", unittest.mock.Mock(shell32=shell32),
                                    create=True):
            self.assertTrue(tk_icon.claim_app_identity())
        shell32.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with(
            "DeepPattern.DecisionEngine")

    def test_assets_are_committed(self):
        ico, png = tk_icon.icon_paths()
        self.assertIsNotNone(ico, "desktop/icons/logo.ico is missing from the body")
        self.assertIsNotNone(png, "desktop/icons/logo-256.png is missing from the body")

    def test_the_icns_is_committed_and_is_an_icns(self):
        """It is a runtime asset now, not just packaging input — the Dock reads it on macOS. And
        nothing regenerates it on a user's machine, so a missing or malformed file means a Dock tile
        that never appears. Checked on every platform for that reason; only the reader is
        macOS-only. (The generator no longer needs macOS either — see ``write_icns``.)"""
        self.assertTrue(tk_icon.ICNS_PATH.exists(),
                        "desktop/icons/AppIcon.icns is missing from the body")
        with tk_icon.ICNS_PATH.open("rb") as handle:
            magic, size = struct.unpack(">4sI", handle.read(8))
        self.assertEqual(magic, b"icns", "AppIcon.icns is not an icns container")
        self.assertEqual(size, tk_icon.ICNS_PATH.stat().st_size,
                         "the icns header length disagrees with the file — it is truncated")


class IcnsGeometry(unittest.TestCase):
    """The reported macOS defect, pinned as geometry.

    A user's Dock showed the audit / graphic-explanation / comic-explanation / whiteboard tiles
    standing visibly taller than their neighbours. The cause was not the artwork but the canvas:
    every representation drew its rounded tile edge-to-edge, so the Dock scaled a 1024px body into
    the slot that a conformant icon fills with an 824px one.

    Measured off the committed bytes rather than off the generator, because the committed bytes are
    what ships — the client body arrives by ``git pull`` and nothing regenerates anything on a
    user's machine."""

    # (OSType, pixels). The ARGB pair and a spread of the PNG entries; every size is the same
    # compositor, so decoding all ten would only cost seconds to re-prove one property.
    MEASURED = (("ic04", 16), ("ic05", 32), ("ic08", 256), ("ic14", 512), ("ic10", 1024))

    def rows_for(self, kind, payload, size):
        return argb_rows(payload, size) if kind in ("ic04", "ic05") else png_rows(payload)

    def test_every_dock_representation_is_present(self):
        """The Dock picks a representation by the slot it is drawing; a missing one is scaled up
        from a smaller neighbour and looks soft rather than absent, which is easy to miss by eye."""
        present = set(icns_entries(tk_icon.ICNS_PATH))
        for kind, _size in self.MEASURED:
            self.assertIn(kind, present, "%s is missing from AppIcon.icns" % kind)

    def test_the_tile_leaves_apples_margin(self):
        """The regression. Was: fill ratio 1.0000 at every size — no margin at all."""
        entries = icns_entries(tk_icon.ICNS_PATH)
        for kind, size in self.MEASURED:
            rows = self.rows_for(kind, entries[kind], size)
            self.assertEqual(len(rows), size, "%s is not %dpx tall" % (kind, size))
            x0, y0, x1, y1 = ink_bbox(rows)
            width, height = x1 - x0 + 1, y1 - y0 + 1
            want = TILE_BODY_RATIO * size
            slack = TILE_RATIO_SLACK * size + TILE_ANTIALIAS_SLACK_PX
            for label, extent in (("width", width), ("height", height)):
                self.assertLessEqual(
                    extent, want + slack,
                    "%s (%dpx) tile %s is %dpx of a %dpx canvas (fill ratio %.4f) — Apple's grid "
                    "wants %.0fpx, so the Dock draws this %.2fx oversized. Re-run "
                    "desktop/icons/make_icons.py and commit the result."
                    % (kind, size, label, extent, size, extent / size, want, extent / want))
                self.assertGreaterEqual(
                    extent, want - slack,
                    "%s (%dpx) tile %s is only %dpx of a %dpx canvas — undersized against "
                    "Apple's %.0fpx grid" % (kind, size, label, extent, size, want))

    def test_the_tile_is_centred_in_its_canvas(self):
        """Apple's margin is symmetric. An off-centre body reads as a misaligned icon in the Dock
        row even when its size is right, and it is the failure a one-sided inset would produce."""
        entries = icns_entries(tk_icon.ICNS_PATH)
        for kind, size in self.MEASURED:
            x0, y0, x1, y1 = ink_bbox(self.rows_for(kind, entries[kind], size))
            for label, near, far in (("horizontally", x0, size - 1 - x1),
                                     ("vertically", y0, size - 1 - y1)):
                self.assertLessEqual(abs(near - far), 1,
                                     "%s (%dpx) is off-centre %s: %dpx one side, %dpx the other"
                                     % (kind, size, label, near, far))

    def test_the_corners_are_transparent_and_the_body_is_not(self):
        """What separates a tile with a margin from a smaller mark on bare transparency: the corner
        pixel must be clear (it is outside the squircle) while the centre must be opaque."""
        entries = icns_entries(tk_icon.ICNS_PATH)
        for kind, size in self.MEASURED:
            rows = self.rows_for(kind, entries[kind], size)
            self.assertEqual(rows[0][0][3], 0, "%s: the top-left corner is not transparent" % kind)
            self.assertEqual(rows[size // 2][size // 2][3], 255,
                             "%s: the centre of the tile is not opaque" % kind)

    def test_the_small_representations_keep_a_legible_mark(self):
        """Shrinking the tile shrinks the mark with it. At 16px the mark is only ~9px across, so
        this guards the floor: brand-coloured ink must still be there in quantity. A mark that
        silted up to nothing would otherwise satisfy every geometry assertion above."""
        entries = icns_entries(tk_icon.ICNS_PATH)
        for kind, size in (("ic04", 16), ("ic05", 32)):
            rows = argb_rows(entries[kind], size)
            ink = sum(1 for row in rows for r, g, b, a in row
                      if a > 128 and max(r, g, b) < 200 and g > r and g > b)
            self.assertGreaterEqual(ink, max(4, size * size // 40),
                                    "%s (%dpx) has almost no mark left: %d ink pixels"
                                    % (kind, size, ink))


class Generator(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ICONS))
        self.make = importlib.import_module("make_icons")

    def test_master_paths_are_addressable_by_id(self):
        """The generator selects paths by id; document order is not a contract.

        Asserts the contract the generator actually depends on — every framing id resolves, and
        something is left once they are dropped — rather than an exact path list. The mark's own
        paths may be split or renamed by a re-export (this one arrived as `brain` plus a separate
        `brain-detail`), and that is the case the id-based selection exists to survive."""
        _vw, _vh, shapes = self.make.load_master()
        for framing in self.make.FRAMING_IDS:
            self.assertIn(framing, shapes, "the master has no %r to drop at small sizes" % framing)
        self.assertTrue([i for i in shapes if i not in self.make.FRAMING_IDS],
                        "the master is all framing and no mark")

    def test_the_small_variant_paints_the_stand_in_alone(self):
        """Below the cutoff the master's stand-in replaces the mark outright — it is not the mark
        with pieces removed. 16x16 is 256 pixels, too coarse for the drawn mark's interior."""
        _vw, _vh, shapes = self.make.load_master()
        small = [s.id for s, _c in self.make.layers(shapes, full_mark=False)]
        large = [s.id for s, _c in self.make.layers(shapes, full_mark=True)]
        self.assertEqual(small, list(self.make.SMALL_ONLY_IDS))
        for framing in self.make.FRAMING_IDS:
            self.assertIn(framing, large, "the full mark lost its framing")
        self.assertFalse(set(small) & set(large), "a path is painted in both assemblies")

    def test_a_master_without_a_stand_in_still_gets_a_small_icon(self):
        """The stand-in is optional. Drop it and the small variant must fall back to the mark minus
        its framing — the behaviour every master before this one relied on."""
        _vw, _vh, shapes = self.make.load_master()
        without = {k: v for k, v in shapes.items() if k not in self.make.SMALL_ONLY_IDS}
        small = [s.id for s, _c in self.make.layers(without, full_mark=False)]
        self.assertTrue(small, "the small variant paints nothing")
        self.assertFalse(set(small) & set(self.make.FRAMING_IDS), "the framing survived")

    def test_an_all_framing_master_is_rejected(self):
        """Painting nothing would write a blank entry, which is worse than failing the build."""
        _vw, _vh, shapes = self.make.load_master()
        only_framing = {k: v for k, v in shapes.items() if k in self.make.FRAMING_IDS}
        with self.assertRaises(ValueError):
            self.make.layers(only_framing, full_mark=False)

    def test_a_cropping_clippath_is_rejected(self):
        """Figma wraps exports in a full-canvas clipPath that clips nothing. This rasteriser cannot
        clip at all, so a clip that really crops must fail the build rather than be ignored."""
        full = '<svg viewBox="0 0 60 35"><clipPath><rect width="60" height="35"/></clipPath></svg>'
        cropping = '<svg viewBox="0 0 60 35"><clipPath><rect width="30" height="35"/></clipPath></svg>'
        self.make._check_clip(full, 60, 35)          # the no-op form is accepted
        with self.assertRaises(ValueError):
            self.make._check_clip(cropping, 60, 35)

    def test_relative_path_commands_are_rejected_loudly(self):
        """A re-export with relative coordinates must fail the build, not render a mangled icon."""
        with self.assertRaises(ValueError):
            self.make.parse_path_data("M10 10 c 5 5 10 10 15 15")

    def test_a_number_after_Z_raises_instead_of_hanging(self):
        """Z takes no arguments, so nothing can implicitly repeat it. Before this was handled, the
        Z branch consumed no token and re-ran on the same index forever — a hang, which is strictly
        worse than a crash in a hand-run build script."""
        probe = (
            "from desktop.icons.make_icons import parse_path_data; "
            "parse_path_data('M0 0 L10 0 L10 10 Z 5')"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("parse_path_data did not terminate within 5s")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("ValueError", completed.stderr)

    def test_an_open_subpath_fills_like_a_closed_one(self):
        """SVG fills an open subpath as if it were closed, but this rasteriser builds edges from
        consecutive points only — so without an explicit closing edge the scanline crossings stop
        balancing and the wrong region fills."""
        self.assertEqual(self.make.parse_path_data("M0 0 L10 0 L10 10"),
                         self.make.parse_path_data("M0 0 L10 0 L10 10 Z"))

    def test_committed_ico_still_regenerates_from_the_master(self):
        """Every other test here would pass against a stale .ico built from an older logo.svg, or
        against a hand-edited one. This is the only assertion that ties the committed bytes to the
        master they claim to come from. Small sizes only — the 256px entry is ~4s of pure Python."""
        sizes = [16, 20, 24, 32]
        _vw, _vh, shapes = self.make.load_master()
        fresh = {size: self.make.variant(shapes, size, size >= BRACE_CUTOFF, supersample=4)
                 for size in sizes}
        committed = {w: blob for w, _h, _bpp, blob in ico_entries(ICO) if w in fresh}
        for size in sizes:
            self.assertEqual(self.make.ico_image(fresh[size], size), committed[size],
                             "%dpx entry no longer matches logo.svg — re-run "
                             "desktop/icons/make_icons.py and commit the result" % size)

    def test_a_non_opaque_fill_is_baked_against_white(self):
        """The current master is flat, but a re-export carrying fill-opacity must not reach the
        rasteriser half-transparent — on a dark title bar that paint goes muddy and merges with
        whatever it overlaps."""
        _vw, _vh, shapes = self.make.load_master()
        half = next(iter(shapes.values()))
        half.opacity = 0.5
        color = dict((s.id, c) for s, c in self.make.layers(shapes, full_mark=True))[half.id]
        self.assertEqual(color, tuple(round(half.fill[i] * 0.5 + 255 * 0.5) for i in range(3)))

    def test_the_tile_geometry_matches_apples_grid(self):
        """The constants, checked against Apple's published numbers rather than against themselves:
        a 1024 canvas, an 824 body, a 185.4px radius. Getting these wrong is not a crash, it is a
        Dock tile that renders the wrong size — which is what shipped."""
        self.assertEqual(self.make.APPLE_CANVAS_PX, 1024)
        self.assertAlmostEqual(self.make.TILE_BODY_RATIO * 1024, 824, places=6)
        self.assertAlmostEqual(self.make.TILE_MARGIN_RATIO * 1024, 100, places=6)
        self.assertAlmostEqual(self.make.TILE_RADIUS_RATIO * 824, 185.4, places=6)

    def test_the_tile_body_is_inset_and_centred(self):
        """``rounded_tile`` against an empty buffer, so what is measured is the tile alone. Guards
        the two-variable split inside it: the body's extent and the canvas centre are different
        numbers now, and collapsing them back into one restores the edge-to-edge tile."""
        size = 64
        tile = self.make.rounded_tile(bytearray(size * size * 4), size, self.make.WHITE)
        rows = [[tuple(tile[(y * size + x) * 4:(y * size + x) * 4 + 4]) for x in range(size)]
                for y in range(size)]
        x0, y0, x1, y1 = ink_bbox(rows)
        want = self.make.TILE_BODY_RATIO * size
        self.assertLessEqual(abs((x1 - x0 + 1) - want), 1, "tile body is not %.1fpx wide" % want)
        self.assertLessEqual(abs((y1 - y0 + 1) - want), 1, "tile body is not %.1fpx tall" % want)
        self.assertEqual((x0, y0), (size - 1 - x1, size - 1 - y1), "the margin is not symmetric")
        self.assertEqual(rows[0][0][3], 0, "the corner is inside the tile")

    def test_the_mark_is_centred_at_every_size(self):
        """``_inset`` truncates its offset, so an odd ``size - inner`` puts the mark a pixel left of
        and above centre inside a tile that is itself symmetric. Worst at 16px, where one pixel is
        ~11% of a 9px mark — and 16/32/64 are exactly the sizes that rebasing on the tile body moved
        into that case.

        Measured by feeding in a fully opaque square and locating where the ink actually lands, so
        this pins the observable placement rather than re-deriving the function's own arithmetic."""
        for _ostype, size, _points in self.make.ICNS_SLOTS:
            opaque = bytes([0, 0, 0, 255]) * (size * size)
            placed = self.make._inset(opaque, size)
            self.assertEqual(len(placed), size * size * 4, "%dpx: buffer changed size" % size)
            rows = [[tuple(placed[(y * size + x) * 4:(y * size + x) * 4 + 4]) for x in range(size)]
                    for y in range(size)]
            x0, y0, x1, y1 = ink_bbox(rows)
            self.assertEqual((x0, y0), (size - 1 - x1, size - 1 - y1),
                             "%dpx: mark sits at L%d/R%d T%d/B%d — not centred"
                             % (size, x0, size - 1 - x1, y0, size - 1 - y1))

    def test_the_argb_codec_round_trips(self):
        """``argb_chunk`` writes the two smallest representations, and its PackBits repeat bias is 3
        rather than stock PackBits' 2 — the kind of off-by-one that yields a smeared icon rather than
        an error. Includes the runs that sit on the encoding's boundaries (130 is the longest repeat
        expressible, 128 the longest literal)."""
        for label, data in (
                ("flat", b"\x00" * 1000),
                ("no repeats", bytes(range(256))),
                ("longest repeat", b"\xff" * 130),
                ("one past it", b"\xff" * 131),
                ("alternating", b"ab" * 300),
                ("mixed", b"\x00" * 3 + b"\x01" + b"\x02" * 2 + b"\x03" * 129)):
            encoded = self.make._packbits(data)
            decoded, used = rle_decode(encoded, len(data))
            self.assertEqual(decoded, data, "%s: did not survive the round trip" % label)
            self.assertEqual(used, len(encoded), "%s: %d of %d bytes consumed"
                             % (label, used, len(encoded)))

    def test_the_icns_writer_refuses_an_incomplete_set(self):
        """Fail-closed. A missing representation is not a missing icon — the Dock scales a neighbour
        up, which looks soft rather than absent, so a silently skipped slot could ship unnoticed."""
        size_of = {ostype: px for ostype, px, _pts in self.make.ICNS_SLOTS}
        full = {ostype: bytes(px * px * 4) for ostype, px in size_of.items()}
        with self.assertRaises(ValueError):
            self.make.write_icns(ICONS / "unwritten.icns", {k: v for k, v in full.items()
                                                            if k != "ic10"})
        wrong = dict(full, ic10=bytes(16 * 16 * 4))
        with self.assertRaises(ValueError):
            self.make.write_icns(ICONS / "unwritten.icns", wrong)
        self.assertFalse((ICONS / "unwritten.icns").exists(), "it wrote a file before validating")

    def test_slots_that_share_a_size_do_not_share_artwork(self):
        """ic11 and ic05 are both 32px, but ic11 is a 16-POINT tile and ic05 a 32-point one, so one
        drops the framing braces and the other keeps them. A writer keyed on pixel size instead of
        slot would put one in the other's place — invisible at a glance, wrong on a Retina Dock."""
        by_size = {}
        for ostype, pixels, points in self.make.ICNS_SLOTS:
            by_size.setdefault(pixels, []).append((ostype, points))
        shared = {px: slots for px, slots in by_size.items() if len(slots) > 1}
        self.assertTrue(shared, "no size is shared — this test no longer guards anything")
        cutoff = self.make.BRACE_CUTOFF
        self.assertTrue(
            any(len({pts >= cutoff for _os, pts in slots}) > 1 for slots in shared.values()),
            "no shared size straddles BRACE_CUTOFF, so nothing distinguishes the slots")

    def test_committed_icns_still_regenerates_from_the_master(self):
        """The icns counterpart of the .ico test above, and the assertion whose absence let an
        edge-to-edge tile ship: every other icns check would pass against a stale or hand-built
        file. Small slots only — the 512px and 1024px renders are minutes of pure Python."""
        entries = icns_entries(tk_icon.ICNS_PATH)
        _vw, _vh, shapes = self.make.load_master()
        for ostype, size, points in self.make.ICNS_SLOTS:
            if size > 64:
                continue
            mark = self.make.variant(shapes, size, points >= BRACE_CUTOFF, supersample=4)
            tile = bytes(self.make.rounded_tile(
                bytearray(self.make._inset(mark, size)), size, self.make.WHITE))
            fresh = (self.make.argb_chunk(tile, size) if ostype in self.make.ICNS_ARGB
                     else self.make.png_bytes(tile, size))
            self.assertEqual(fresh, entries[ostype],
                             "%s (%dpx) no longer matches logo.svg — re-run "
                             "desktop/icons/make_icons.py and commit the result" % (ostype, size))


if __name__ == "__main__":
    unittest.main()
