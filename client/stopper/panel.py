"""tkinter audit Stop Panel — the client's stop panel on platforms without the native macOS
Swift stopper (Windows first; Linux gets it too).

Design parity with `desktop/macos/DecisionEngineStopper.swift`:
  * COLLAPSED render — ONE line per run: audit tier (Fast/Standard/Deep) + overall status +
    live elapsed. Per-voice model names / counts are deliberately NOT shown (privacy; matches
    the Swift panel and the voice-redaction model).
  * Data comes from `client.runner`: the active-runs registry on disk (seeded by the shim at
    submit) plus a 1 Hz poll of the hub `/v1/audits/<id>` for status / profile / started_at.
  * Stop button cancels queued audits via the hub (`POST /v1/audits/<id>/cancel`).

Parity is about what the panel SHOWS, not how it behaves as a window. Deliberate divergence: this
panel is NOT always-on-top, while the Swift one is `.floating` (Owner, 2026-07-17). A floating
utility window is the macOS convention for a menu-bar accessory; on Windows this is a taskbar app,
and pinning it over everything meant an audit sat over your work for its whole run. Do not "restore
parity" by re-adding -topmost.

BEFORE TOUCHING ANY TIMING CODE, read README.md next to this file: a finished run's duration comes
from the hub's timestamps, never from this machine's clock. That one rule has already been broken
once here, and it cost a day of chasing a server that was never slow.

The pure render helpers (depth_label / overall_status / elapsed_seconds / depth_line_text) are
module-level and tkinter-free so they can be unit-tested without a display. tkinter is imported
lazily inside `StopPanelApp` / `main` so importing this module (and the helpers) never requires
a GUI toolkit.

Runtime: `pythonw -m client.stopper.panel` (the launcher spawns it detached, console-less).
Honors the same env overrides as runner (DE_CONFIG_PATH / DE_ACTIVE_RUNS / DE_ACTIVE_RUN).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from client import i18n, runner, tk_icon

# ── constants (mirror the Swift panel) ───────────────────────────────────────────────────────
TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}
FINISHED_LINGER_S = 30.0     # keep a finished run visible this long before pruning
IDLE_EXIT_S = 2.0            # quit the app this long after the last run disappears
POLL_INTERVAL_MS = 1000
SCROLL_MAX_SCREEN_FRACTION = 0.618
# A running row goes "连接中断" after this long with no successful poll. Polls run at 1 Hz, so this
# tolerates a long burst of failures (transient drop / hub restart / laptop resume) before we admit
# we're blind — but stays far under the minutes a real outage lasts.
STALE_AFTER_S = 20.0
# How many CONSECUTIVE hub 404s (run_not_found) retire a run. A submit-seeded run whose hub state
# is gone (purged, or never registered) 404s every poll and can never reach a terminal status, so
# nothing would ever prune it — the row shows "排队中" forever and stacks on each later start. But
# right after submit the hub can briefly 404 before it registers the run, so we require a short
# streak (grace) and reset it on any successful poll — never reaping a legitimate just-submitted
# audit. At the 1 Hz poll cadence this is a few seconds' grace; a real orphan has been 404 for far
# longer. (Mirrors the "never abort on the first run_not_found" convention in skills/audit.)
NOT_FOUND_FORGET_THRESHOLD = 3
# HTTP codes that mean "this run is definitively gone" (not a transient blip): 404 is what the hub
# returns for a purged / never-registered run today; 410 Gone is the same "resource is gone" class
# and is treated identically so a future hub that switches to it can't resurrect the immortal-orphan
# bug this reap fixes. Every OTHER code (401/403/5xx/…) stays transient → left as-is and retried.
_GONE_STATUS_CODES = frozenset({404, 410})
_SYNCHRONOUS_WORKFLOW_ERROR_CODE = "synchronous_workflow_id"

# Dark palette (A-repo BG_MAIN #262624 etc.) — same values the Swift panel uses.
BG = "#262624"
FG_TEXT = "#F5F5F4"
FG_MUTED = "#8E8D86"
FG_RED = "#BC4936"      # A-repo BTN_RED / FG_RED_FAIL (warm Claude red) — matches the macOS panel
FG_GREEN = "#34C759"
FG_AMBER = "#C7A14F"    # A-repo FG_YELLOW_FB — partial + cancelled
BTN_FINISHED = "#3A3A37"
BTN_RED_ACTIVE = "#A33D2E"
# Hub run IDs are a short prefix plus ``secrets.token_urlsafe`` output (and workflow IDs use the same
# URL-safe alphabet with ``-`` separators). Keep a generous UI bound while rejecting controls/spoofing.
_MAX_AUDIT_ID_LENGTH = 128

# Native UI font per platform — an empty family ("") makes tkinter fall back to a serif (Times) on
# Windows, which looks broken; name the OS UI font explicitly so the panel matches the desktop.
if sys.platform == "darwin":
    _UI = "SF Pro Text"
    _MONO = "Menlo"
elif sys.platform.startswith("win"):
    _UI = "Segoe UI"
    _MONO = "Consolas"
else:
    _UI = "DejaVu Sans"
    _MONO = "DejaVu Sans Mono"


# ── pure render helpers (no tkinter) ─────────────────────────────────────────────────────────
def _num(value: Any) -> float:
    """Coerce a JSON number (int/float) to float; 0.0 for anything else (str/None/NSNull)."""
    if isinstance(value, bool):  # bool is an int subclass — never a timestamp
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def depth_label(run: Dict[str, Any]) -> str:
    """Fast / Standard / Deep from the run's tier (carried in `profile`); a generic label when
    absent. Matches Swift depthLabel — the tier travels in `profile`, never `mode`.

    A LOCAL advisory run (design §11/§17: an offline single-model sanity read that never closes an
    audit gate) is labelled loudly and distinctly so it can never be mistaken for a cross-vendor
    panel — the honesty requirement is carried in the label itself, not left to the reader."""
    strings = _panel(run)
    if run.get("local"):
        if run.get("local_surface") == "de_lite":
            return "DE Lite"
        return strings["local_fallback_label"]
    profile = str(run.get("profile") or "").lower()
    return strings["tier"].get(profile, strings["cross_vendor"])


def ui_locale(run: Dict[str, Any]) -> str:
    """Resolve a run language; an absent/unsupported tag uses the host locale, then English."""
    return "en" if runner.normalize_ui_locale(run.get("ui_locale")) == "en-US" else "zh"


def _panel(run: Dict[str, Any]) -> Dict[str, Any]:
    """Panel string table for this run's resolved language (the single decision point in i18n)."""
    return i18n.panel("en-US" if ui_locale(run) == "en" else "zh-CN")


def action_text(run: Dict[str, Any], status: str) -> str:
    return _panel(run)["action"].get(status, status)


def overall_status(run: Dict[str, Any]) -> str:
    """Overall state WITHOUT exposing individual voices: running if any voice is non-terminal;
    else completed / failed / partial. Empty auditors → the run's own status. Mirrors Swift."""
    # cancel is a run-level state the voices don't carry — surface it directly so the depth line and
    # the action pill agree (a cancelled run must not read as "completed" off its auditors).
    run_status = str(run.get("status") or "").lower()
    if run_status in ("cancelling", "cancelled"):
        return run_status
    auditors = run.get("auditors")
    if not isinstance(auditors, list) or not auditors:
        return str(run.get("status") or "queued")
    statuses = [str((a or {}).get("status") or "pending").lower() for a in auditors]
    if any(s not in ("completed", "failed") for s in statuses):
        return "running"
    any_fail = "failed" in statuses
    any_ok = "completed" in statuses
    if any_fail and any_ok:
        return "partial"
    return "failed" if any_fail else "completed"


def is_active(run: Dict[str, Any]) -> bool:
    run_id = str(run.get("run_id") or "")
    return bool(run_id) and str(run.get("status") or "") not in TERMINAL_STATUSES


def _entry_is_active(run_id: str, run: Dict[str, Any]) -> bool:
    """Apply the single active-run predicate with the registry key as authoritative identity."""
    return is_active({**run, "run_id": run_id})


def should_hub_poll(run: Dict[str, Any]) -> bool:
    """False for a LOCAL advisory run (design §11/§17): it has no hub run_id, so a hub poll would
    404 and the reaper would retire it (and its honest 🔶 row) mid-read. A local entry's lifecycle
    is its own `hidden_after` TTL (see runner.save_local_advisory_run), never a hub poll."""
    return not run.get("local")


def elapsed_seconds(run: Dict[str, Any], now: float, frozen: Dict[str, float]) -> float:
    """How long the run TOOK (terminal) or has been going (live).

    A finished run's duration is `completed_at - started_at` — BOTH hub clocks. It must never be
    derived from this machine's `now`, because `now - started_at` answers "how long ago did it
    start", not "how long did it take": it inflates by however long the panel took to observe the
    finish (a restart drops the freeze cache; a disk re-seed flips the row back to running and
    re-freezes it at the current time; the two effects compound every linger cycle), and it silently
    mixes this box's clock with the hub's, so any skew lands straight in the number. Prod ran 2-4min
    audits that this displayed as 12-61min. Only fall back to the local-clock freeze when the hub
    gave us no `completed_at` (a disk-seeded row we never polled).
    """
    started = _num(run.get("started_at"))
    completed = _num(run.get("completed_at"))
    run_id = str(run.get("run_id") or "")
    status = overall_status(run)
    if status not in ("running", "queued") and started > 0 and completed > 0:
        return max(0.0, completed - started)
    live = max(0.0, now - started) if started > 0 else 0.0
    if status in ("running", "queued"):
        frozen.pop(run_id, None)
        return live
    if run_id in frozen:
        return frozen[run_id]
    frozen[run_id] = live
    return live


def is_stale(run: Dict[str, Any], now: float) -> bool:
    """True when a still-running row's last successful poll is old enough that we no longer know
    what the hub is doing (network down / hub unreachable). A poll failure leaves the last view in
    place, so without this the row would keep ticking a confident timer while blind — and the run
    may well have finished. Terminal rows are never stale: their duration is already hub-derived
    and cannot change."""
    if overall_status(run) not in ("running", "queued"):
        return False
    last_ok = _num(run.get("last_ok_at"))
    if last_ok <= 0:
        return False                      # never polled yet (just seeded) — not a disconnect
    return (now - last_ok) > STALE_AFTER_S


def elapsed_string(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    if total < 60:
        return "%ds" % total
    return "%dm %ds" % (total // 60, total % 60)


def _local_advisory_line(run: Dict[str, Any], now: float, frozen: Dict[str, float]) -> str:
    """One-line render for a LOCAL advisory run (design §11/§17). Deliberately uses words a real
    cross-vendor panel never uses — 分饰中 / 完成（仅参考） — and NEVER the ✓ / 已完成 pass mark, so
    a single-model sanity read cannot read as an audit that passed. No hub poll backs a local run,
    so it is never `连接中断` (is_stale needs a hub `last_ok_at` it never has)."""
    elapsed = elapsed_string(elapsed_seconds(run, now, frozen))
    status = overall_status(run)
    strings = _panel(run)
    # Arity is keyed off an EXPLICIT set, not dict membership: these statuses carry an elapsed slot
    # (2 %s: lead, elapsed). queued and ANY unrecognized status (incl. a literal "unknown", which
    # overall_status can pass through) fall to the "unknown"/queued 1-%s template — matching the
    # pre-catalog fallthrough and never risking a 2-arg format on a 1-slot template.
    two_arg = {"running", "cancelling", "cancelled", "failed", "completed", "partial"}
    if run.get("local_surface") == "de_lite":
        reason = strings["reason"].get(run.get("degrade_reason"), "")
        suffix = (" · " + reason) if reason else ""
        templates = strings["de_lite"]
        if status == "queued":
            return templates["queued"] % suffix
        if status in two_arg:
            return templates[status] % (suffix, elapsed)
        return templates["unknown"] % suffix
    # 🔶 local fallback (non-de_lite): the honesty invariant lives in the wording — NEVER a ✓ /
    # 已完成 pass mark, and an unreadable status must not fall through to a completion phrase.
    head = strings["local_head"]
    templates = strings["local_line"]
    if status == "queued":
        return templates["queued"] % head
    if status in two_arg:
        return templates[status] % (head, elapsed)
    return templates["unknown"] % head


def depth_line_text(run: Dict[str, Any], now: float, frozen: Dict[str, float]) -> str:
    return _collapsed_depth_line_text(run, now, frozen)

def _collapsed_depth_line_text(run: Dict[str, Any], now: float, frozen: Dict[str, float]) -> str:
    if run.get("local"):
        return _local_advisory_line(run, now, frozen)
    depth = depth_label(run)
    elapsed = elapsed_string(elapsed_seconds(run, now, frozen))
    status = overall_status(run)
    strings = _panel(run)
    if is_stale(run, now):
        # Blind: say so and show the last time we actually heard from the hub, instead of a live
        # number that pretends we still know. The audit itself keeps running server-side; when the
        # network returns, a terminal row's duration comes back hub-derived and exact.
        return strings["hub_stale"] % (
            depth, elapsed_string(_num(run.get("last_ok_at")) - _num(run.get("started_at"))))
    if status == "queued":
        return strings["hub_queued"] % depth
    return strings["hub"].get(status, strings["hub"]["reviewing"]) % (depth, elapsed)


def _debug_auditor_lines(run: Dict[str, Any], now: float) -> List[str]:
    return [detail["text"] for detail in _debug_auditor_details(run, now)]


def _debug_auditor_details(run: Dict[str, Any], now: float) -> List[Dict[str, str]]:
    if run.get("debug_authorized") is not True:
        return []
    auditors = run.get("auditors")
    if not isinstance(auditors, list) or not auditors:
        return []
    if any(not isinstance(auditor, dict) for auditor in auditors):
        return []
    return [
        {
            "text": _auditor_display_text(run, auditor, now),
            "color": _auditor_status_color(str(auditor.get("status") or "pending").lower()),
        }
        for auditor in auditors
    ]


def _auditor_status_color(status: str) -> str:
    if status == "completed":
        return FG_GREEN
    if status == "failed":
        return FG_RED
    if status in ("partial", "cancelled"):
        return FG_AMBER
    return FG_MUTED

def depth_line_color(run: Dict[str, Any], now: Optional[float] = None) -> str:
    if run.get("local"):
        # A local advisory read is never a cross-vendor "pass": NEVER render it green (green reads
        # as passed). Active states are muted; a failure keeps its red affordance; everything else
        # finished is amber — attention/degraded, not approval. (audit ee5e025a f1/f2)
        local_status = overall_status(run)
        if local_status in ("running", "queued", "cancelling"):
            return FG_MUTED
        if local_status == "failed":
            return FG_RED
        return FG_AMBER
    if now is not None and is_stale(run, now):
        return FG_AMBER            # unknown ≠ healthy-running; don't render a blind row as normal
    status = overall_status(run)
    if status == "completed":
        return FG_GREEN
    if status in ("partial", "cancelled"):
        return FG_AMBER
    if status == "failed":
        return FG_RED
    return FG_MUTED


def run_title(run: Dict[str, Any]) -> str:
    # Fallback title follows THIS run's resolved language (per-run, like the action/depth lines),
    # not the host locale — a zh run with no title reads「审核」, an en run reads "Audit".
    title = str(run.get("title") or "").strip() or _panel(run)["default_title"]
    return title if len(title) <= 48 else title[:47] + "…"


def audit_id_text(run: Dict[str, Any]) -> str:
    """Return the user-facing audit ID, omitting one fixed ``aud_`` prefix.

    The registry key remains authoritative for polling/cancellation; this helper controls presentation
    only. Local advisory identifiers and malformed/unbounded server values are never rendered.
    """
    if run.get("local"):
        return ""
    raw_run_id = str(run.get("run_id") or "")
    if raw_run_id != raw_run_id.strip():
        return ""
    run_id = raw_run_id
    if (not run_id or len(run_id) > _MAX_AUDIT_ID_LENGTH or
            any(not (char.isascii() and (char.isalnum() or char in "_-")) for char in run_id)):
        return ""
    if run_id.startswith("aud_"):
        run_id = run_id[4:]
    if not run_id:
        return ""
    return run_id


def merge_poll_view(
    existing: Dict[str, Any],
    view: Any,
    run_id: str,
    now: float,
    *,
    preserve_cancelling: bool = False,
) -> Dict[str, Any]:
    """Overlay a hub /v1/audits view onto the run we already hold. Non-None view fields win, but:
      * `cancelling` is a client-only request-in-flight state. Preserve it only while this process
        still owns that request; after a restart or response, the server view is authoritative;
      * a terminal status gets a linger deadline so the finished run stays visible briefly.
    Returns the merged run. A non-dict view (bad payload) leaves `existing` untouched."""
    if not isinstance(view, dict):
        return dict(existing)
    if (preserve_cancelling and existing.get("status") == "cancelling"
            and view.get("status") not in TERMINAL_STATUSES):
        view = {**view, "status": "cancelling"}
    merged = dict(existing)
    merged.update({k: v for k, v in view.items() if v is not None})
    merged["run_id"] = run_id
    merged["last_ok_at"] = now      # local clock: when the hub last answered (drives is_stale)
    if str(merged.get("status") or "") in TERMINAL_STATUSES:
        merged.setdefault("hidden_after", now + FINISHED_LINGER_S)
    return merged


# ── single-instance lock (cross-platform, held for the process lifetime) ──────────────────────
_singleton_handles = []  # keep all compatibility lock fds alive for the whole process


def _release_singleton_handles(handles) -> None:
    for held in reversed(handles):
        try:
            if runner.fcntl is not None:
                runner.fcntl.flock(held.fileno(), runner.fcntl.LOCK_UN)
            elif runner.msvcrt is not None:
                held.seek(0)
                runner.msvcrt.locking(held.fileno(), runner.msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        held.close()


def acquire_single_instance() -> bool:
    """True if we got the lock (no other panel running). Uses fcntl on POSIX / msvcrt on Windows;
    if neither exists we optimistically allow the launch (best-effort, like the Swift lock)."""
    global _singleton_handles
    if _singleton_handles:
        return True
    acquired = []
    for lock_path in runner.stopper_panel_lock_paths():
        try:
            if runner._path_key(lock_path) == runner._path_key(runner.stopper_panel_lock_path()):
                runner._ensure_private_dir(lock_path.parent)
            else:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = runner._open_lock_file(lock_path)
        except (OSError, runner.AuditError) as exc:
            _release_singleton_handles(acquired)
            # Without every compatibility lock we cannot exclude both previous-
            # stable and current panels. Audits still run; only the panel is skipped.
            runner._note_stopper_miss(
                "panel singleton lock unavailable at %s: %r" % (lock_path, exc)
            )
            return False
        try:
            if runner.fcntl is not None:
                runner.fcntl.flock(handle.fileno(), runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB)
            elif runner.msvcrt is not None:
                handle.seek(0)
                runner.msvcrt.locking(handle.fileno(), runner.msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            _release_singleton_handles(acquired)
            return False  # another old or new instance holds one of the compatibility locks
        acquired.append(handle)
    _singleton_handles = acquired  # deliberately never closed → locks held until process exit
    return True


# ── cross-platform action button -------------------------------------------------------------
_ACTION_WIDTH = 86
_ACTION_HEIGHT = 34
# Keep the 8px smooth-corner convention documented by the native macOS parity implementation.
_ACTION_RADIUS = 8
_ACTION_ICON_SIZE = 8
_ACTION_HORIZONTAL_PADDING = 12
_ACTION_ICON_GAP = 7
_ACTION_STOP_TEXT = "STOP"
_ACTION_CANCELLING_TEXT = "Cancelling…"
_ACTION_CANCELLED_TEXT = "Cancelled"
_ACTION_RUNNING_TEXT = "Running"
_ACTION_CHECKING_TEXT = "Checking…"
_ACTION_FINISHED_TEXT = "Finished"
_ACTION_LABELS = (
    _ACTION_STOP_TEXT,
    _ACTION_CANCELLING_TEXT,
    _ACTION_CANCELLED_TEXT,
    _ACTION_RUNNING_TEXT,
    _ACTION_CHECKING_TEXT,
    _ACTION_FINISHED_TEXT,
)


def _display_scale(widget: Any, tk: Any) -> float:
    """Return Windows-style logical-pixel scaling from Tk's reported screen DPI."""
    try:
        dpi = float(widget.winfo_fpixels("1i"))
    except (AttributeError, TypeError, ValueError, tk.TclError):
        return 1.0
    return min(max(dpi / 96.0, 1.0), 4.0)


class _DarkScrollbar:
    """Canvas-backed vertical scrollbar so Windows themes cannot paint a white trough."""

    def __init__(self, tk: Any, parent: Any, command: Any, width: int = 12) -> None:
        self._tk = tk
        self._command = command
        self._width = width
        self._first = 0.0
        self._last = 1.0
        self._command_name: Optional[str] = None
        self._canvas = tk.Canvas(
            parent, width=width, bg=BG, bd=0, highlightthickness=0,
            highlightbackground=BG, highlightcolor=BG, takefocus=False,
        )
        self._track = self._canvas.create_rectangle(0, 0, width, 1, fill=BG, outline=BG)
        self._thumb = self._canvas.create_rectangle(3, 0, width - 3, 1, fill=FG_MUTED, outline=FG_MUTED)
        self._canvas.bind("<Configure>", self._redraw)
        self._canvas.bind("<Button-1>", self._move_to_event)
        self._canvas.bind("<B1-Motion>", self._move_to_event)

    def set(self, first: Any, last: Any) -> None:
        self._first = max(0.0, min(1.0, float(first)))
        self._last = max(self._first, min(1.0, float(last)))
        self._redraw()

    def _redraw(self, _event: Any = None) -> None:
        height = max(1, self._canvas.winfo_height())
        self._canvas.coords(self._track, 0, 0, self._width, height)
        if self._last >= 1.0 and self._first <= 0.0:
            self._canvas.itemconfigure(self._thumb, state="hidden")
            return
        thumb_top = int(height * self._first)
        thumb_bottom = max(thumb_top + 24, int(height * self._last))
        if thumb_bottom > height:
            thumb_top = max(0, height - (thumb_bottom - thumb_top))
            thumb_bottom = height
        self._canvas.coords(self._thumb, 3, thumb_top, self._width - 3, thumb_bottom)
        self._canvas.itemconfigure(self._thumb, state="normal")

    def _move_to_event(self, event: Any) -> str:
        height = max(1, self._canvas.winfo_height())
        span = max(0.01, self._last - self._first)
        fraction = max(0.0, min(1.0 - span, (event.y / height) - (span / 2.0)))
        self._command("moveto", fraction)
        return "break"

    def _command_proxy(self, *args: Any) -> Any:
        return self._command(*args)

    def cget(self, option: str) -> Any:
        if option == "command":
            if self._command_name is None:
                self._command_name = self._canvas.register(self._command_proxy)
            return self._command_name
        return self._canvas.cget(option)

    def itemcget(self, *args: Any) -> Any:
        return self._canvas.itemcget(*args)

    def grid(self, *args: Any, **kwargs: Any) -> Any:
        return self._canvas.grid(*args, **kwargs)

    def grid_remove(self) -> None:
        self._canvas.grid_remove()

    def winfo_ismapped(self) -> int:
        return self._canvas.winfo_ismapped()


class _RoundedActionButton:
    """Small Canvas-backed button with deterministic corners and a drawn stop square.

    Classic ``tk.Button`` has no portable corner-radius API, and U+23F9 font-linking renders as a
    nested square on Windows. A real Button remains under the Canvas for semantic name/state and
    native keyboard behavior; the Canvas owns only the deterministic visual treatment and pointer
    forwarding.
    """

    def __init__(self, tk: Any, parent: Any, command: Any) -> None:
        self._tk = tk
        self._command = command
        self._scale = 0.0
        self._width = 0
        self._height = 0
        self._inside = False
        self._mouse_down = False
        self._focused = False
        self._options: Dict[str, Any] = {
            "text": "", "bg": BTN_FINISHED, "fg": FG_MUTED,
            "disabledforeground": FG_MUTED, "activebackground": BTN_FINISHED,
            "activeforeground": FG_MUTED, "state": "disabled", "cursor": "",
            "showicon": False,
        }
        self.frame = tk.Frame(parent, bg=BG, bd=0, highlightthickness=0)
        self.frame.pack_propagate(False)
        self.button = tk.Button(
            self.frame, text="", bg=BG, fg=FG_MUTED, disabledforeground=FG_MUTED,
            activebackground=BG, activeforeground=FG_MUTED, relief="flat", bd=0,
            highlightthickness=0, takefocus=False, state="disabled", command=command,
        )
        self.canvas = tk.Canvas(
            self.frame, width=1, height=1, bg=BG,
            bd=0, highlightthickness=0, takefocus=False,
        )
        self._background = self.canvas.create_polygon(
            0, 0, 1, 0, 1, 1, 0, 1,
            smooth=True, splinesteps=12, fill=BTN_FINISHED, outline=BTN_FINISHED,
        )
        self._icon = self.canvas.create_rectangle(
            0, 0, 1, 1,
            fill=FG_MUTED, outline="", state="hidden",
        )
        self._label = self.canvas.create_text(
            0, 0, text="", fill=FG_MUTED,
            font=(_UI, 12, "bold"), anchor="center",
        )
        self.button.place(x=0, y=0, width=1, height=1)
        self.canvas.place(x=0, y=0, width=1, height=1)
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.button.bind("<FocusIn>", self._on_focus_in)
        self.button.bind("<FocusOut>", self._on_focus_out)
        self._sync_geometry()

    def pack(self, **options: Any) -> None:
        self.frame.pack(**options)

    def configure(self, **options: Any) -> None:
        self._options.update(options)
        self._sync_geometry()
        enabled = self._options["state"] != "disabled"
        if not enabled:
            self._mouse_down = False
            self._focused = False
        self.button.configure(
            text=self._options["text"], state=self._options["state"],
            fg=self._options["fg"], disabledforeground=self._options["disabledforeground"],
            activebackground=BG, activeforeground=self._options["activeforeground"],
            cursor=self._options["cursor"], takefocus=enabled,
        )
        self.canvas.configure(cursor=self._options["cursor"])
        self._layout_content()
        self._paint()

    def _sync_geometry(self) -> None:
        scale = _display_scale(self.frame, self._tk)
        if scale == self._scale:
            return
        self._scale = scale
        self._width = self._required_width(scale)
        self._height = round(_ACTION_HEIGHT * scale)
        radius = round(_ACTION_RADIUS * scale)
        self.frame.configure(width=self._width, height=self._height)
        self.button.place_configure(x=0, y=0, width=self._width, height=self._height)
        self.canvas.configure(width=self._width, height=self._height)
        self.canvas.place_configure(x=0, y=0, width=self._width, height=self._height)
        self.canvas.coords(
            self._background,
            radius, 0, self._width - radius, 0,
            self._width, 0, self._width, radius,
            self._width, self._height - radius, self._width, self._height,
            self._width - radius, self._height, radius, self._height,
            0, self._height, 0, self._height - radius,
            0, radius, 0, 0,
        )
        self._layout_content()

    def _required_width(self, scale: float) -> int:
        widths: Dict[str, int] = {}
        previous_text = self.canvas.itemcget(self._label, "text")
        previous_anchor = self.canvas.itemcget(self._label, "anchor")
        try:
            for text in _ACTION_LABELS:
                self.canvas.itemconfigure(self._label, text=text, anchor="center")
                try:
                    bounds = self.canvas.bbox(self._label)
                except (AttributeError, TypeError, self._tk.TclError):
                    bounds = None
                if bounds is not None:
                    widths[text] = max(0, int(bounds[2] - bounds[0]))
        finally:
            self.canvas.itemconfigure(
                self._label, text=previous_text, anchor=previous_anchor,
            )

        padding = round(_ACTION_HORIZONTAL_PADDING * scale)
        centered_labels = max(widths.values(), default=0) + 2 * padding
        icon_label = (
            round(_ACTION_ICON_SIZE * scale)
            + round(_ACTION_ICON_GAP * scale)
            + widths.get(_ACTION_STOP_TEXT, 0)
            + 2 * padding
        )
        return max(round(_ACTION_WIDTH * scale), centered_labels, icon_label)

    def _layout_content(self) -> None:
        icon_top = round((_ACTION_HEIGHT - _ACTION_ICON_SIZE) * self._scale / 2)
        icon_size = round(_ACTION_ICON_SIZE * self._scale)
        show_icon = bool(self._options["showicon"])
        self.canvas.itemconfigure(
            self._icon, state=("normal" if show_icon else "hidden"),
        )
        self.canvas.itemconfigure(self._label, text=self._options["text"])
        if show_icon:
            self.canvas.itemconfigure(self._label, anchor="w")
            self.canvas.coords(self._label, 0, self._height / 2)
            try:
                bounds = self.canvas.bbox(self._label)
            except (AttributeError, TypeError, self._tk.TclError):
                bounds = None
            gap = round(_ACTION_ICON_GAP * self._scale)
            if bounds is None:
                label_left = 0
                icon_left = round(_ACTION_HORIZONTAL_PADDING * self._scale)
            else:
                label_left = int(bounds[0])
                label_width = max(0, int(bounds[2] - bounds[0]))
                content_width = icon_size + gap + label_width
                icon_left = round((self._width - content_width) / 2)
            self.canvas.coords(
                self._icon, icon_left, icon_top, icon_left + icon_size, icon_top + icon_size,
            )
            self.canvas.coords(
                self._label,
                icon_left + icon_size + gap - label_left,
                self._height / 2,
            )
        else:
            self.canvas.coords(self._label, self._width / 2, self._height / 2)
            self.canvas.itemconfigure(self._label, anchor="center")

    config = configure

    def cget(self, name: str) -> Any:
        return self._options.get(name)

    def invoke(self) -> Any:
        return self.button.invoke()

    def _paint(self) -> None:
        disabled = self._options["state"] == "disabled"
        active = self._inside and not disabled
        fill = self._options["activebackground"] if active else self._options["bg"]
        if disabled:
            foreground = self._options["disabledforeground"]
        elif active:
            foreground = self._options["activeforeground"]
        else:
            foreground = self._options["fg"]
        outline = "#FFFFFF" if self._focused and not disabled else fill
        self.canvas.itemconfigure(self._background, fill=fill, outline=outline)
        self.canvas.itemconfigure(self._icon, fill=foreground)
        self.canvas.itemconfigure(self._label, fill=foreground)

    def _on_enter(self, _event: Any) -> None:
        self._inside = True
        self._paint()

    def _on_leave(self, _event: Any) -> None:
        self._inside = False
        self._paint()

    def _on_press(self, _event: Any) -> None:
        if self._options["state"] == "disabled":
            return
        self._inside = True
        self._mouse_down = True
        self.button.focus_set()
        self._paint()

    def _on_release(self, _event: Any) -> None:
        invoke = self._mouse_down and self._inside
        self._mouse_down = False
        self._paint()
        if invoke:
            self.invoke()

    def _on_focus_in(self, _event: Any) -> None:
        self._focused = True
        self._paint()

    def _on_focus_out(self, _event: Any) -> None:
        self._focused = False
        self._paint()


# ── the tkinter app ──────────────────────────────────────────────────────────────────────────
class StopPanelApp:
    def __init__(self) -> None:
        import tkinter as tk  # lazy: keeps module import GUI-free

        self._tk = tk
        self.runs: Dict[str, Dict[str, Any]] = {}
        self._frozen: Dict[str, float] = {}
        self._polling: set = set()
        self._cancel_inflight: set = set()
        # Disk state is only a discovery seed. A queued row is cancellable only after this process
        # has confirmed it with the hub, preventing stale registry data from re-enabling STOP.
        self._server_verified: set = set()
        # A cancel transition bumps this generation so older in-flight GETs cannot regress state.
        self._state_epochs: Dict[str, int] = {}
        self._not_found_polls: Dict[str, int] = {}   # run_id → consecutive hub 404s (reap streak)
        self._retired: set = set()   # run_ids shown to completion + pruned — never re-seed from disk
        self._lock = threading.Lock()
        self._no_runs_since: Optional[float] = None
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._row_order: List[str] = []
        self._empty_label = None

        # Before Tk(): Windows reads the AppUserModelID when the window registers with the shell,
        # and without our own the taskbar button shows Python's icon rather than this window's.
        tk_icon.claim_app_identity()
        self.root = tk.Tk()
        # Panel chrome → host locale (like the empty-state label at _create_row), not any single run.
        shell_titles = i18n.shell(i18n.resolve_locale(None)).get("surface_title", {})
        audit_title = shell_titles.get("audit", "Decision Engine") if isinstance(shell_titles, dict) else "Decision Engine"
        self.root.title(audit_title)  # native title bar -> taskbar button, minimise, close
        tk_icon.apply_window_icon(self.root)     # …and the title bar shows an icon; Tk's default is a feather
        # AFTER Tk(), unlike the AUMID above. macOS has the same fall-back-to-the-interpreter defect
        # but the opposite ordering rule: Tk installs its own NSApplication subclass, and anything
        # that instantiates a plain one first makes Tk_Init abort the process. See apply_dock_icon.
        tk_icon.apply_dock_app_name(audit_title)
        tk_icon.apply_dock_icon()
        self.root.configure(bg=BG)
        self.root.minsize(320, 96)
        # Deliberately NOT always-on-top (Owner, 2026-07-17). It used to set -topmost to float like
        # the macOS Swift panel; on Windows that made an audit panel sit over whatever you were
        # working in for the whole run, with no way to push it back. A new panel still comes up in
        # front — it just stops winning every raise after that, so the title bar's minimise means
        # what it says. The Swift panel keeps .floating: that is a menu-bar-anchored accessory on a
        # platform where a floating utility window is the convention, not a taskbar app.
        viewport = tk.Frame(self.root, bg=BG)
        viewport.pack(fill="both", expand=True, padx=12, pady=12)
        viewport.grid_rowconfigure(0, weight=1)
        viewport.grid_columnconfigure(0, weight=1)
        self.scroll_canvas = tk.Canvas(
            viewport, bg=BG, width=520, height=72, bd=0, highlightthickness=0,
            yscrollincrement=1, takefocus=True,
        )
        self.scroll_canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = _DarkScrollbar(tk, viewport, self.scroll_canvas.yview)
        self.scroll_canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = tk.Frame(self.scroll_canvas, bg=BG)
        self._body_window = self.scroll_canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", self._sync_scroll_region)
        self.scroll_canvas.bind("<Configure>", self._resize_viewport)
        self._wheel_remainder = 0.0
        # Toplevel bindings also receive events over descendant labels, entries and buttons.
        # Keep them local to this window; bind_all would capture unrelated Tk windows.
        self.root.bind("<MouseWheel>", self._scroll_wheel, add="+")
        if self.root.tk.call("tk", "windowingsystem") == "x11":
            self.root.bind("<Button-4>", self._scroll_wheel, add="+")
            self.root.bind("<Button-5>", self._scroll_wheel, add="+")
        self.root.bind("<FocusIn>", self._reveal_focused_row, add="+")
        for key, direction in (("<Prior>", -1), ("<Next>", 1)):
            self.scroll_canvas.bind(key, lambda _event, d=direction: self.scroll_canvas.yview_scroll(d, "pages"))

    def _resize_viewport(self, event: Any) -> None:
        self.scroll_canvas.itemconfigure(self._body_window, width=event.width)
        self._sync_scroll_region()

    def _sync_scroll_region(self, _event: Any = None) -> None:
        canvas = self.scroll_canvas
        height = self.body.winfo_reqheight()
        # Let Tk auto-fit a short list, leaving room for the taskbar/Dock and native chrome.
        max_height = max(72, int(self.root.winfo_screenheight() * SCROLL_MAX_SCREEN_FRACTION) - 24)
        canvas.configure(height=min(height, max_height))
        canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), max(height, canvas.winfo_height())))
        if height > canvas.winfo_height():
            self.scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            self.scrollbar.grid_remove()
            canvas.yview_moveto(0)

    def _scroll_wheel(self, event: Any) -> str:
        if self.body.winfo_reqheight() <= self.scroll_canvas.winfo_height():
            return "break"
        if getattr(event, "num", None) in (4, 5):
            pixels = -40 if event.num == 4 else 40
        elif self.root.tk.call("tk", "windowingsystem") == "aqua":
            pixels = -event.delta  # Tk's Mac trackpad deltas are already small; never divide by 120.
        else:
            pixels = -event.delta * 40 / 120
        self._wheel_remainder += pixels * _display_scale(self.root, self._tk)
        whole_pixels = int(self._wheel_remainder)
        self._wheel_remainder -= whole_pixels
        self.scroll_canvas.yview_scroll(whole_pixels, "units")
        return "break"

    def _reveal_focused_row(self, event: Any) -> None:
        widget = event.widget
        if not str(widget).startswith(str(self.body) + "."):
            return
        canvas = self.scroll_canvas
        top = widget.winfo_rooty() - canvas.winfo_rooty()
        bottom = top + widget.winfo_height()
        offset = min(top, 0) if top < 0 else max(0, bottom - canvas.winfo_height())
        canvas.yview_scroll(offset, "units")

    # -- data --------------------------------------------------------------------------------
    def _merge_disk(self) -> None:
        """Pull in runs the shim seeded on disk; in-memory (poll-updated) runs win over disk."""
        try:
            registry = runner.load_active_runs_registry()
        except Exception:
            return
        disk = registry.get("runs") if isinstance(registry, dict) else None
        if not isinstance(disk, dict):
            return
        with self._lock:
            for run_id, run in disk.items():
                if not isinstance(run, dict):
                    continue
                existing = self.runs.get(run_id)
                if existing is not None:
                    # DE Lite never polls the Hub. Its completion callback updates the same
                    # registry after the panel has already rendered the initial running row, so
                    # an existing local row must absorb the disk terminal transition. Hosted rows
                    # remain memory-owned after their server poll; merging their disk copy here
                    # could regress a newer in-memory state with stale persisted data.
                    if (
                        isinstance(existing, dict)
                        and existing.get("local") is True
                        and run.get("local") is True
                    ):
                        current_status = existing.get("status")
                        disk_status = run.get("status")
                        if current_status not in TERMINAL_STATUSES or disk_status in TERMINAL_STATUSES:
                            merged = dict(existing)
                            merged.update(run)
                            merged.setdefault("_sort_at", existing.get("_sort_at"))
                            self.runs[run_id] = merged
                    continue
                if run_id in self._retired:
                    # We already showed this run through to its finish and pruned it. The registry
                    # entry outlives us (it is only pruned when the shim next saves/clears a run),
                    # and it still carries the status seeded at SUBMIT — so re-adding it would
                    # resurrect a finished run as "running", restart its timer, and re-freeze it at
                    # the current time on the next poll. That loop repeats every linger cycle and is
                    # what grew the displayed elapsed without bound.
                    continue
                run = dict(run)
                # The registry key is the authoritative identity. A stale payload id must never
                # redirect a poll or a STOP click to another run.
                run["run_id"] = run_id
                # Snapshot order on first sight. Poll responses can update `updated_at` every second;
                # sorting on that live field makes otherwise-stable rows jump around.
                run["_sort_at"] = (_num(run.get("started_at")) or
                                   _num(run.get("created_at")) or
                                   _num(run.get("updated_at")))
                self.runs[run_id] = run

    def _poll(self, run_id: str, expected_epoch: Optional[int] = None) -> None:
        """Background: fetch the hub run view and overlay it.

        A definitive "gone" reply (`run_not_found` — the hub purged or never registered this run,
        HTTP 404, 410 Gone, or the exact 409 ``synchronous_workflow_id`` contract) is NOT a transient
        blip: a submit-seeded orphan whose hub state is gone can never reach a terminal poll, so
        nothing prunes it and it shows "排队中" forever.
        After NOT_FOUND_FORGET_THRESHOLD consecutive gone-replies (grace for the brief post-submit
        window) it is retired like a finished run. Any OTHER failure (network down / hub unreachable
        — a transport AuditError with no status_code, or a 401/5xx) leaves state as-is and is
        retried next tick."""
        if expected_epoch is None:
            with self._lock:
                expected_epoch = self._state_epochs.get(run_id, 0)
        try:
            view = runner.request_json("GET", "/v1/audits/%s" % run_id)
        except runner.AuditError as exc:
            status_code = getattr(exc, "status_code", None)
            is_synchronous_workflow = (
                status_code == 409
                # Reaping deletes local state, so unlike display-only status normalization this
                # deliberately requires the hub's exact machine-readable contract.
                and getattr(exc, "error_code", None) == _SYNCHRONOUS_WORKFLOW_ERROR_CODE
            )
            if status_code in _GONE_STATUS_CODES or is_synchronous_workflow:
                self._on_run_vanished(run_id)
            else:
                with self._lock:
                    self._polling.discard(run_id)
            return
        except Exception:
            with self._lock:
                self._polling.discard(run_id)
            return
        with self._lock:
            if run_id in self._retired or self._state_epochs.get(run_id, 0) != expected_epoch:
                self._polling.discard(run_id)
                return
            self._not_found_polls.pop(run_id, None)   # a live view resets the reap streak
            existing = self.runs.get(run_id, {})
            if isinstance(view, dict):
                self._server_verified.add(run_id)
                merged = merge_poll_view(
                    existing, view, run_id, time.time(),
                    preserve_cancelling=run_id in self._cancel_inflight,
                )
                self.runs[run_id] = merged
                if str(merged.get("status") or "") in TERMINAL_STATUSES:
                    self._cancel_inflight.discard(run_id)
                if merged.get("status") != existing.get("status"):
                    try:
                        runner.save_active_run(merged, existing_only=True)
                    except Exception:  # aqg: top-level boundary — registry persistence is best-effort UI state
                        pass
            self._polling.discard(run_id)

    def _on_run_vanished(self, run_id: str) -> None:
        """Count a hub 404 for run_id; once the streak reaches the grace threshold, retire the run
        exactly as `_prune` does a finished one — drop it from memory, mark it `_retired` so
        `_merge_disk` cannot re-seed it, and reap the on-disk registry entry off the Tk thread."""
        with self._lock:
            self._polling.discard(run_id)
            count = self._not_found_polls.get(run_id, 0) + 1
            self._not_found_polls[run_id] = count
            if count < NOT_FOUND_FORGET_THRESHOLD:
                return
            self._not_found_polls.pop(run_id, None)
            self.runs.pop(run_id, None)
            self._cancel_inflight.discard(run_id)
            self._server_verified.discard(run_id)
            self._state_epochs.pop(run_id, None)
            self._retired.add(run_id)
        threading.Thread(target=self._forget_on_disk, args=(run_id,), daemon=True).start()

    def _prune(self, now: float) -> None:
        retired = []
        with self._lock:
            for run_id in list(self.runs):
                hidden_after = self.runs[run_id].get("hidden_after")
                if isinstance(hidden_after, (int, float)) and hidden_after <= now:
                    self.runs.pop(run_id, None)
                    self._cancel_inflight.discard(run_id)
                    self._server_verified.discard(run_id)
                    self._state_epochs.pop(run_id, None)
                    self._retired.add(run_id)
                    retired.append(run_id)
        # Drop it from the on-disk registry too, or `_retired` only holds until this process exits:
        # the entry the shim seeded at submit stays "running" forever (nothing on the MCP path ever
        # rewrites it terminal), so the next panel start re-seeds it and every finished audit comes
        # back and stacks up. Off the Tk thread — the registry lock is blocking and held by the shim
        # across a submit, and a frozen panel would be a worse bug than a slow reap.
        for run_id in retired:
            threading.Thread(target=self._forget_on_disk, args=(run_id,), daemon=True).start()

    def _forget_on_disk(self, run_id: str) -> None:
        try:
            runner.forget_active_run(run_id)
        except Exception:
            # Best-effort, like every other disk touch here: the run is already gone from the UI,
            # and a registry we failed to reap costs one stale row on the next start — never worth
            # taking the panel down for. (This process is console-less; there is nowhere to log.)
            pass

    def _visible_runs(self) -> List[tuple]:
        with self._lock:
            runs = list(self.runs.items())
        # Active first, newest submission/start first, then registry id as a stable tie-breaker.
        return sorted(runs, key=lambda item: (
            0 if _entry_is_active(item[0], item[1]) else 1,
            -(_num(item[1].get("_sort_at")) or
              _num(item[1].get("started_at")) or
              _num(item[1].get("created_at")) or
              _num(item[1].get("updated_at"))),
            str(item[0])))

    # -- cancel ------------------------------------------------------------------------------
    def _stop(self, run_id: str) -> None:
        with self._lock:
            run = self.runs.get(run_id)
            if (not isinstance(run, dict) or str(run.get("status") or "") != "queued"
                    or run_id not in self._server_verified or run_id in self._cancel_inflight):
                return
            self.runs[run_id] = {**run, "status": "cancelling"}
            self._cancel_inflight.add(run_id)
            self._server_verified.discard(run_id)
            self._state_epochs[run_id] = self._state_epochs.get(run_id, 0) + 1
        try:
            threading.Thread(target=self._cancel_request, args=(run_id,), daemon=True).start()
        except Exception:  # aqg: top-level boundary — a failed optional UI worker must stay retryable
            with self._lock:
                self._cancel_inflight.discard(run_id)
                current = self.runs.get(run_id)
                if isinstance(current, dict) and current.get("status") == "cancelling":
                    self.runs[run_id] = {**current, "status": "queued"}
        self._render()

    def _cancel_request(self, run_id: str) -> None:
        try:
            result = runner.request_json("POST", "/v1/audits/%s/cancel" % run_id, body={})
        except Exception:
            with self._lock:
                self._cancel_inflight.discard(run_id)
                current = self.runs.get(run_id)
                if isinstance(current, dict) and current.get("status") == "cancelling":
                    updated = {**current, "status": "unknown"}
                    self.runs[run_id] = updated
                    self._server_verified.discard(run_id)
                    self._state_epochs[run_id] = self._state_epochs.get(run_id, 0) + 1
                    try:
                        runner.save_active_run(updated, existing_only=True)
                    except Exception:  # aqg: top-level boundary — best-effort UI persistence
                        pass
            return

        with self._lock:
            self._cancel_inflight.discard(run_id)
            current = self.runs.get(run_id)
            # A terminal poll may have won the race while the POST was in flight. Terminal server
            # observations are absorbing; a late cancel response must never move them backwards.
            if not isinstance(current, dict) or current.get("status") != "cancelling":
                return
            self._server_verified.discard(run_id)
            self._state_epochs[run_id] = self._state_epochs.get(run_id, 0) + 1
            if isinstance(result, dict) and result.get("stopped") is True:
                now = time.time()
                updated = {
                    **current,
                    "status": "cancelled",
                    "finished_at": current.get("finished_at") or now,
                    "hidden_after": now + FINISHED_LINGER_S,
                }
            elif isinstance(result, dict) and result.get("stopped") is False:
                reported = str(result.get("status") or "").lower()
                status = reported if reported in TERMINAL_STATUSES or reported in ("running", "cancelling") \
                    else "unknown"
                updated = {**current, "status": status}
                if status in TERMINAL_STATUSES:
                    now = time.time()
                    updated.setdefault("finished_at", now)
                    updated.setdefault("hidden_after", now + FINISHED_LINGER_S)
            else:
                # Malformed/legacy success cannot prove cancellation under the queued-only contract.
                updated = {**current, "status": "unknown"}
                self.runs[run_id] = updated
                try:
                    runner.save_active_run(updated, existing_only=True)
                except Exception:  # aqg: top-level boundary — best-effort UI persistence
                    pass
                return
            self.runs[run_id] = updated
            try:
                runner.save_active_run(updated, existing_only=True)
            except Exception:  # aqg: top-level boundary — registry persistence is best-effort UI state
                pass

    # -- loop --------------------------------------------------------------------------------
    def _tick(self) -> None:
        now = time.time()
        self._merge_disk()
        with self._lock:
            to_poll = [run_id for run_id, run in self.runs.items()
                       if _entry_is_active(run_id, run) and should_hub_poll(run)
                       and run_id not in self._polling]
            self._polling.update(to_poll)
            poll_epochs = {run_id: self._state_epochs.get(run_id, 0) for run_id in to_poll}
        for run_id in to_poll:
            threading.Thread(
                target=self._poll, args=(run_id, poll_epochs[run_id]), daemon=True,
            ).start()
        self._prune(now)
        self._render()
        if not self._visible_runs():
            self._no_runs_since = self._no_runs_since or now
            if now - self._no_runs_since >= IDLE_EXIT_S:
                self.root.destroy()
                return
        else:
            self._no_runs_since = None
        self.root.after(POLL_INTERVAL_MS, self._tick)

    # -- render ------------------------------------------------------------------------------
    def _create_row(self, row_key: str) -> Dict[str, Any]:
        tk = self._tk
        row = tk.Frame(self.body, bg=BG)
        # Build children while the row is still unpacked, then configure the real state before the
        # row becomes visible. This avoids the Windows default-button white flash on first paint.
        button = _RoundedActionButton(
            tk, row, command=lambda rid=row_key: self._stop(rid),
        )
        button.pack(side="left", padx=(0, 12), anchor="n")
        text = tk.Frame(row, bg=BG)
        text.pack(side="left", fill="x", expand=True)
        title = tk.Label(text, bg=BG, fg=FG_TEXT, font=(_UI, 15, "bold"),
                         anchor="w", justify="left")
        title.pack(anchor="w")
        audit_id_row = tk.Frame(text, bg=BG)
        audit_id_label = tk.Label(
            # Column label is fixed chrome (same for every row) → host locale, like the window title.
            audit_id_row, text=i18n.panel(i18n.resolve_locale(None))["id_label"],
            bg=BG, fg=FG_MUTED, font=(_UI, 10),
            anchor="w", justify="left",
        )
        audit_id_label.pack(side="left", padx=(0, 7))
        audit_id = tk.Entry(
            audit_id_row, bg=BG, readonlybackground=BG, fg=FG_MUTED,
            font=(_MONO, 10), relief="flat", bd=0, highlightthickness=0,
            selectbackground="#5B5A55", selectforeground=FG_TEXT,
            insertbackground=FG_TEXT, cursor="xterm", takefocus=True,
        )
        audit_id.pack(side="left", fill="x", expand=True)
        detail = tk.Label(text, bg=BG, font=(_UI, 12), anchor="w", justify="left")
        detail.pack(anchor="w")
        auditor_details = tk.Frame(text, bg=BG)
        return {
            "row": row, "button": button, "title": title,
            "auditor_details": auditor_details, "auditor_labels": [],
            "audit_id_row": audit_id_row, "audit_id_label": audit_id_label,
            "audit_id": audit_id, "detail": detail,
        }

    def _update_row(
        self, widgets: Dict[str, Any], row_key: str, run: Dict[str, Any], now: float,
    ) -> None:
        # The registry key drives every identity-sensitive helper, including the terminal duration
        # freeze cache. A malformed payload id cannot collide rows or redirect the STOP command.
        render_run = {**run, "run_id": row_key}
        active = _entry_is_active(row_key, run)
        run_status = str(render_run.get("status") or "").lower()
        if run_status == "cancelling":
            btn_text = action_text(render_run, "cancelling")
            btn_fg, btn_bg, btn_on = FG_MUTED, BTN_FINISHED, False
        elif run_status == "cancelled":
            btn_text = action_text(render_run, "cancelled")
            btn_fg, btn_bg, btn_on = FG_AMBER, BTN_FINISHED, False
        elif run_status == "queued" and active and row_key in self._server_verified:
            btn_text = action_text(render_run, "stop")
            btn_fg, btn_bg, btn_on = "#FFFFFF", FG_RED, True
        elif run_status == "queued" and active:
            btn_text = action_text(render_run, "checking")
            btn_fg, btn_bg, btn_on = FG_MUTED, BTN_FINISHED, False
        elif run_status == "running" and active:
            btn_text = action_text(render_run, "running")
            btn_fg, btn_bg, btn_on = "#FFFFFF", FG_RED, False
        elif active:
            btn_text = action_text(render_run, "checking")
            btn_fg, btn_bg, btn_on = FG_MUTED, BTN_FINISHED, False
        else:
            btn_text = action_text(render_run, "finished")
            btn_fg, btn_bg, btn_on = FG_MUTED, BTN_FINISHED, False
        widgets["button"].configure(
            text=btn_text, bg=btn_bg, fg=btn_fg, disabledforeground=btn_fg,
            activebackground=(BTN_RED_ACTIVE if btn_on else BTN_FINISHED), activeforeground=btn_fg,
            state=("normal" if btn_on else "disabled"), cursor=("hand2" if btn_on else ""),
            showicon=btn_on,
        )
        widgets["title"].configure(text=run_title(render_run))
        id_text = audit_id_text(render_run)
        audit_id = widgets["audit_id"]
        # The panel refreshes every second. Do not rewrite unchanged text: doing so clears the user's
        # live selection just as they try to copy it.
        if audit_id.get() != id_text:
            audit_id.configure(state="normal")
            audit_id.delete(0, "end")
            audit_id.insert(0, id_text)
            audit_id.configure(state="readonly")
        if id_text:
            widgets["audit_id_row"].pack(
                fill="x", anchor="w", pady=(4, 0), before=widgets["detail"],
            )
        else:
            widgets["audit_id_row"].pack_forget()
        widgets["detail"].configure(
            text="· " + depth_line_text(render_run, now, self._frozen),
            fg=depth_line_color(render_run, now),
        )
        self._update_auditor_details(widgets, render_run, now)

    def _update_auditor_details(
        self, widgets: Dict[str, Any], render_run: Dict[str, Any], now: float,
    ) -> None:
        details = _debug_auditor_details(render_run, now)
        frame = widgets["auditor_details"]
        labels = widgets["auditor_labels"]
        while len(labels) > len(details):
            labels.pop().destroy()
        for index, detail in enumerate(details):
            if index >= len(labels):
                label = self._tk.Label(
                    frame, bg=BG, font=(_UI, 12), anchor="w", justify="left",
                )
                label.pack(anchor="w")
                labels.append(label)
            labels[index].configure(text=detail["text"], fg=detail["color"])
        if details:
            frame.pack(fill="x", anchor="w", pady=(2, 0), after=widgets["detail"])
        else:
            frame.pack_forget()

    def _render(self) -> None:
        tk = self._tk
        now = time.time()
        entries = self._visible_runs()
        desired_order = [row_key for row_key, _run in entries]

        for row_key in set(self._rows) - set(desired_order):
            self._rows.pop(row_key)["row"].destroy()

        if not entries:
            self._row_order = []
            if self._empty_label is None:
                # No run in hand, so key the empty-state text off the host locale (the same single
                # decision point, with no explicit run tag) instead of a hardcoded language.
                empty_text = i18n.panel(i18n.resolve_locale(None))["empty"]
                self._empty_label = tk.Label(
                    self.body, text=empty_text, bg=BG, fg=FG_MUTED, font=(_UI, 13))
                self._empty_label.pack(anchor="w")
            self.body.after_idle(self._sync_scroll_region)
            return

        if self._empty_label is not None:
            self._empty_label.destroy()
            self._empty_label = None

        for row_key, run in entries:
            if row_key not in self._rows:
                self._rows[row_key] = self._create_row(row_key)
            self._update_row(self._rows[row_key], row_key, run, now)

        if desired_order != self._row_order:
            previous = None
            for row_key in desired_order:
                row = self._rows[row_key]["row"]
                packed = self.body.pack_slaves()
                if previous is None:
                    if not packed:
                        row.pack(fill="x", pady=6)
                    elif packed[0] is not row:
                        row.pack_configure(fill="x", pady=6, before=packed[0])
                elif row not in packed or packed.index(row) != packed.index(previous) + 1:
                    row.pack_configure(fill="x", pady=6, after=previous)
                previous = row
            self._row_order = list(desired_order)
        # Off-screen canvas windows may defer their Configure event after rows are removed.
        self.body.after_idle(self._sync_scroll_region)

    def run(self) -> None:
        self._tick()
        self.root.mainloop()


def main(argv: Optional[List[str]] = None) -> int:
    if os.getenv("DE_SKIP_STOPPER_LAUNCH") == "1":
        return 0
    if not acquire_single_instance():
        return 0  # another panel is already up
    try:
        import tkinter  # noqa: F401 — probe: no display / no Tk → nothing to show, exit cleanly
    except Exception:
        return 0
    try:
        StopPanelApp().run()
    except Exception as exc:  # a GUI panel must never become a visible crash for the customer
        sys.stderr.write("stop panel exited: %r\n" % exc)
        return 1
    return 0


def _auditor_model_label(auditor: Dict[str, Any]) -> str:
    return (
        str(auditor.get("model_id") or "").strip()
        or str(auditor.get("model_alias") or "").strip()
        or str(auditor.get("model") or "").strip()
        or str(auditor.get("provider") or "").strip()
        or "auditor"
    )


def _auditor_elapsed(auditor: Dict[str, Any], now: float) -> str:
    duration_ms = _num(auditor.get("duration_ms"))
    if duration_ms > 0:
        return elapsed_string(duration_ms / 1000.0)
    started_at = _num(auditor.get("started_at"))
    if started_at > 0:
        return elapsed_string(max(0.0, now - started_at))
    return "0s"


def _auditor_display_text(run: Dict[str, Any], auditor: Dict[str, Any], now: float) -> str:
    model = _auditor_model_label(auditor)
    status = str(auditor.get("status") or "pending").lower()
    label = _panel(run)["auditor_status"].get(status, status)
    dot = chr(0xB7)
    if status == "completed":
        elapsed = _auditor_elapsed(auditor, now)
        return "%s %s %s %s %s %s" % (model, dot, label, dot, elapsed, chr(0x2713))
    if status == "failed":
        return "%s %s %s" % (model, dot, label)
    if status == "running":
        elapsed = _auditor_elapsed(auditor, now)
        return "%s %s %s %s %s" % (model, dot, label, dot, elapsed)
    return "%s %s %s" % (model, dot, label)


if __name__ == "__main__":
    raise SystemExit(main())


