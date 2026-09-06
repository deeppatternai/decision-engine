# Icons

One vector master, one generator, every raster the client ships.

```bash
python3 desktop/icons/make_icons.py        # add --no-icns to skip the macOS bundle icon
```

| file | generated? | who reads it |
|---|---|---|
| `logo.svg` | no — **the master** | the generator, and nothing else |
| `logo.ico` | yes | `client/tk_icon.py` → the Windows stop panel and the setup dialogs |
| `logo-64.png`, `logo-256.png` | yes | `client/tk_icon.py` → Tk's `iconphoto` on non-Windows |
| `AppIcon.icns` | yes | `client/tk_icon.py` → the macOS Dock tile, at runtime; and the `Decision Engine Stopper.app` bundle, built out of band |
| `../macos/bin/stopper_icon.png`, `@2x` | yes | the Swift panel's menu-bar item, and `client/popup/native_shell.py` |

The outputs are **committed**. The client body is delivered by `git pull` and nothing runs a build
step on a user's machine, so a generated-but-uncommitted asset is an asset that does not exist.
Re-run the generator and commit its output whenever `logo.svg` changes.

## Two decisions live in the generator, not the master

The master stays byte-faithful to what design authored. Everything icon-specific is applied at
generation time, where it is one place and it is explained:

**Sizes below 32px get their own artwork.** An ICO is a container of independent images, not one
bitmap Windows resizes, so every entry may be drawn for the size it is actually seen at. Two things
follow from that:

*The framing braces are dropped.* The full mark is 60×40 (1.5:1); squaring it for an icon spends a
third of the box on empty margin, and by 16px the braces are two smudges charged against the pixels
the brain needs. Without them the brain is very nearly square and fills the box. From 32px up the
braces hold their stroke and the full mark ships.

*The mark itself is swapped for a stand-in.* 16×16 is 256 pixels. The drawn mark's interior — a
diagonal sweep and fine folds — lands between the grid lines there and silts up into a blob, while
an upright symmetrical figure stays crisp. So the master carries `brain-small`, the earlier plainer
brain, and `SMALL_ONLY_IDS` routes it to those entries alone. This is optional: a master without a
stand-in still gets the mark-minus-framing, which is what every earlier master relied on.

The cutoff is 32 and not 48, which is where it first sat. `SM_CXICON` is 32 at 100% DPI, so 32px
*is* the taskbar button — the one place most users ever see this icon. Cutting above it shipped the
braces only to high-DPI desktops and the shell's large-icon views, and from an ordinary desktop
that is indistinguishable from having dropped the framing entirely.

Which paths get dropped is defined by `FRAMING_IDS` — the paths that *frame* the mark — rather than
by listing the mark's own paths. A re-export that renames or splits the mark still produces a
correct small icon; only a change to the framing itself needs that constant touched.

**Any non-opaque fill is baked against white.** A `fill-opacity` in the master is authored against
the white artboard it was drawn on; rasterised onto transparency and dropped on a dark title bar the
same paint turns muddy and merges with whatever it overlaps. The generator substitutes the
composite-over-white colour, so white backgrounds are unchanged and dark ones keep their layering.
The current master is a single flat `#0B8565`, so nothing triggers this — it stays because the next
re-export is where it would silently matter.

## The macOS tile has 100px of margin, and it is not spare space

Apple's macOS 11+ app-icon grid is a 1024×1024 canvas whose rounded-rect body is **824×824**, with a
185.4px corner radius — 100px of transparent margin per side. macOS does not mask an app icon the way
iOS does (the squircle is yours to draw), and the Dock scales the *whole canvas* into its tile slot.

So a body drawn edge-to-edge lands in the slot every conformant icon fills to 80.47%, and renders
**1.24x larger than its neighbours**. That is not theory — it shipped, and the report was Dock tiles
for the audit / graphic-explanation / comic-explanation / whiteboard windows standing visibly taller
than the apps beside them. `TILE_BODY_RATIO` and its neighbours in the generator carry the numbers;
`IcnsGeometry` in `installer/tests/test_window_icon.py` measures them back off the committed bytes.

Only `AppIcon.icns` is inset. Windows scales a window icon to fit its own box, so the same margin in
`logo.ico` would just make the icon smaller for nothing.

## Why the .icns is not built by `iconutil`

It used to be, which meant the file could only be regenerated on macOS — and, more to the point, only
be *measured* there. The asset is committed and read at runtime, nothing builds on a user's machine,
and every check this repo had on it amounted to "does it start with `icns`". An edge-to-edge tile
satisfied that for as long as nobody looked at a Dock.

`write_icns` writes the container directly: PNG payloads for the eight larger representations, and
Apple's PackBits-compressed ARGB for the 16px and 32px pair, which is what `iconutil` emits at those
sizes. Stdlib only, like the rest of this directory. It drops one thing `iconutil` writes — the
`info` chunk, an NSKeyedArchiver blob naming the asset-catalog entry — which no image reader consults.

Slots are keyed by OSType, not by pixel size, because two of them share a size without sharing
artwork: `ic11` and `ic05` are both 32px, but `ic11` is a 16-**point** tile with the braces dropped
and `ic05` a 32-point one that keeps them.

The trade is deliberate: one writer, one output, on every platform. Keeping `iconutil` as a second
path would mean the same master producing different bytes depending on who regenerated it.

## What the generator refuses

It rasterises `<path>` fills and nothing else, so anything it cannot honour must fail the build
rather than be silently dropped:

- **relative path commands** — a re-export with `c`/`l`/`m` raises instead of rendering mangled art;
- **a `<clipPath>` that actually crops** — Figma wraps exports in a full-canvas clip that clips
  nothing, which is accepted; a real crop raises, because this rasteriser has no clipping;
- **a `<path>` with no `id`** — paths are selected by id, so document order is never a contract.

## Why the generator is stdlib-only

No Pillow, no cairosvg, no `rsvg-convert`. The client declares exactly one runtime dependency
(`certifi`), and a dev tool that needs an image stack installed is a tool nobody can run. So this
directory carries its own SVG rasteriser, covering the subset the master uses (absolute
`M`/`L`/`H`/`V`/`C`/`Z`).

If the master is re-exported, add an `id` to each `<path>` — Figma writes none, and the generator
refuses to build without them rather than guess by document order. The mark's own paths may be
named anything and may be split across several (the current export splits the brain into `brain`
plus a 586-subpath `brain-detail`); only `FRAMING_IDS` (`brace-left`, `brace-right`) and
`SMALL_ONLY_IDS` (`brain-small`) carry meaning. Re-exporting will not include `brain-small` — it is
the previous mark, kept deliberately, so preserve that path when replacing the file.

## The taskbar icon is not the window icon

On Windows the taskbar button takes its icon from the process's **AppUserModelID**, not from the
window. A bare `pythonw.exe` inherits Python's AUMID, so the taskbar keeps showing the Python logo
even once the title bar is correct — which is exactly what the first Windows check found. Every
entry point therefore calls `client.tk_icon.claim_app_identity()` **before** creating its first
window; Windows reads the AUMID when the window registers with the shell, so claiming it afterwards
leaves the already-registered button alone.

The id (`DeepPattern.DecisionEngine`) is arbitrary but must stay stable: changing it re-groups and
un-pins existing taskbar buttons. It is product-wide rather than per-surface on purpose — the stop
panel and the popup shell are separate processes, and sharing one id is what makes their taskbar
buttons group as one application instead of two unrelated ones.

The popup shell needs a second step the Tk surfaces do not. It is pywebview, whose Windows backend
is a WinForms host with no `icon` argument — that parameter exists only on the GTK and Qt backends —
so `client.tk_icon.apply_taskbar_icon()` stamps the icon straight onto the window handle with
`WM_SETICON`, in both sizes (the taskbar reads the big one, Alt-Tab the small one). That window is
frameless, so those two are the only places its icon is ever drawn.

### macOS has the same defect and a different lever

A process that shows a Dock tile and never sets one is drawn as the binary that started it — for us
a bare `python3`. The popup is a Dock app whether we like it or not (pywebview forces
`NSApplicationActivationPolicyRegular`), so `client.tk_icon.apply_dock_icon()` sets the tile from
`AppIcon.icns`.

**It runs AFTER the toolkit has made its application, which is the opposite of the AUMID's rule.**
Tk installs its own `NSApplication` subclass and `Tk_Init` sends it `-_setup:`; a plain
`NSApplication` created first does not implement that selector, and the process aborts before any
window opens. `apply_dock_icon` therefore reads `NSApp()` and returns `False` when it is nil rather
than creating one, and the callers sit after `Tk()` / on the popup's `loaded` event. The Dock reads
the tile whenever it is set, so nothing is lost by waiting.

### The menu-bar icon has to sit beside the binary

The Swift panel loads it with `Bundle.main.url(forResource: "stopper_icon")`, and the panel ships as
a **bare Mach-O executable** rather than an `.app`. For those, `Bundle.main` is simply the directory
holding the executable and is not searched upwards — so `desktop/macos/stopper_icon.png` was
invisible to the binary in `desktop/macos/bin/`, one level down. `statusIconImage()` fell through to
`makeStatusImage`, which draws a coloured badge with the letter `A`, and that is what the menu bar
showed. The file now lives in `bin/` beside the binary, one copy shared with `native_shell.py`, and
a test in `test_stopper_fallback.py` pins the relationship — move either alone and it re-breaks.

That is also what stops `AppIcon.icns` from being a write-only file. It used to be generated on
every run and consumed by nothing in the repo — the app bundle that wanted it is assembled out of
band — so nothing here could tell whether it was correct, or even valid. It now has a runtime
reader and a test that sets the tile and reads it back off the live `NSApplication`.

## The one thing the ICO format gets to decide

Entries are written as classic 32bpp DIBs, never PNG-compressed. Embedded PNG has been legal in ICO
since Vista and every modern viewer reads it — but Tk is not a modern viewer. `wm iconbitmap` on
Windows parses the icon directory itself and builds icons from DIB data, so a PNG entry is where
this silently degrades to *no icon at all* on the one platform the file exists for. Uncompressed
256px costs ~270KB. That is the price of it working.

`installer/tests/test_window_icon.py` pins all of the above. What it cannot check from a POSIX host
is whether a real Windows title bar renders it; that needs a Windows box.
