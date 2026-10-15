"""English (en-US) UI strings. Chinese lives in `zh_CN.py` — never mix languages in one file.

Every table here has a key set identical to its `zh_CN.py` twin (a parity test recurses through all
tables and guards against drift). `%(name)s` / `%s` placeholders are filled by the caller with `%`.
Nothing here decides language; `client.i18n` does.
"""

# GE terminal-notice pages (client/popup/notice.py).
NOTICE = {
    "close": "Close",
    "failed": ("Graphic generation failed",
               "The server could not finish this graphic. Please try again later."),
    "cancelled": ("Graphic generation was cancelled",
                  "This graphic run was cancelled and has no artifact to display."),
    "pending": ("Graphic generation is still pending",
                "Automatic waiting ended. Keep the run ID to recover the result later."),
}

# GE follow-up-chat RUNTIME strings (client/popup/chat_backend.py). User-facing meta-refusal + error
# text. The model-facing CONTRACT / preheat text stays in chat_backend and is not i18n'd. A caller
# `strings` dict may still override per key at runtime; this is the default set chosen by locale.
CHAT_DEFAULTS = {
    "meta_refusal": ("That's not something I can share — I'm only here to help you understand "
                     "the diagram on the left. Anything in it you'd like to dig into?"),
    "timeout": "That follow-up timed out — the model didn't reply in time. Try again?",
    "error": "Follow-up failed — an internal error occurred. Please try again.",
    "model_error": "Follow-up failed — the model couldn't reply this time. Please try again.",
    "unavailable": "Follow-up is temporarily unavailable. Please try again later.",
    "missing_claude_cli": (
        "This device does not have Claude Code CLI installed, so right-side follow-up is "
        "unavailable. Please install and sign in to Claude Code CLI in a terminal, then "
        "reopen this window."),
    "missing_codex_cli": (
        "This device does not have Codex CLI installed, so right-side follow-up is unavailable. "
        "Please install and sign in to Codex CLI in a terminal, then reopen this window."),
    "missing_cursor_cli": (
        "This device does not have Cursor Agent CLI installed, so right-side follow-up is "
        "unavailable. Please install and sign in to Cursor Agent CLI in a terminal, then "
        "reopen this window."),
    "cursor_followup_disabled": (
        "Cursor Agent right-side follow-up is disabled by configuration. Enable it, then "
        "reopen this window."),
    "images_unsupported": (
        "Cursor follow-up does not support image attachments yet; please ask with text."),
}

# First-use device-activation form (client/popup/launcher.py :: render_activation_html).
ACTIVATION = {
    "secret_label": "Activation code",
    "name_label": "Device name (optional)",
    "submit": "Activate",
    "cancel": "Cancel",
}

# Stopper audit-list panel (client/stopper/panel.py). Templates carry `%s` slots filled by the
# caller; the honesty invariant (a local advisory NEVER reads as a passed cross-vendor audit) lives
# in the WORDING here — keep "reference only" / no ✓ on local lines exactly as-is. The Swift Stopper
# keeps its own copy; this table is the Python panel only.
PANEL = {
    "empty": "No audits in progress",
    # Static panel chrome (resolved against the HOST locale, like `empty`): the OS window title, the
    # per-row audit-ID column label, and the fallback title for a run whose own title is blank.
    "window_title": "Decision Engine",
    "id_label": "ID",
    "default_title": "Audit",
    "local_fallback_label": "🔶 Local fallback",
    "cross_vendor": "Cross-vendor audit",
    "tier": {"fast": "Fast", "standard": "Standard", "deep": "Deep"},
    "action": {
        "cancelling": "Cancelling…", "cancelled": "Cancelled", "stop": "STOP",
        "running": "Running", "checking": "Checking…", "finished": "Finished",
    },
    "reason": {
        "unactivated": "Unactivated", "subscription_expired": "Subscription expired",
        "credits_exhausted": "Insufficient credits", "rate_limited": "Rate limited",
        "service_unavailable": "Service unavailable", "mcp_unavailable": "MCP unavailable",
    },
    "de_lite": {
        "queued": "DE Lite · Local audit queued%s ⏱",
        "running": "DE Lite · Local audit in progress%s · %s ⏱",
        "cancelling": "DE Lite · Local audit stopping%s · %s ⏱",
        "cancelled": "DE Lite · Local audit cancelled%s · %s",
        "failed": "DE Lite · Local audit failed%s · %s ✗",
        "completed": "DE Lite · Local audit completed (reference only)%s · %s",
        "partial": "DE Lite · Local audit partially complete (reference only)%s · %s",
        "unknown": "DE Lite · Local audit status unknown (reference only)%s ⏱",
    },
    "local_head": "🔶 Local fallback · single-model, non-panel",
    "local_line": {
        "queued": "%s · Queued ⏱",
        "running": "%s · Local audit in progress · %s ⏱",
        "cancelling": "%s · Stopping · %s ⏱",
        "cancelled": "%s · Cancelled · %s",
        "failed": "%s · Failed · %s",
        "completed": "%s · Complete (reference only) · %s",
        "partial": "%s · Partially complete (reference only) · %s",
        "unknown": "%s · Unknown status (reference only) ⏱",
    },
    "hub_stale": "%s · Connection interrupted · Last known %s ⚠",
    "hub_queued": "%s · Queued ⏱",
    "hub": {
        "completed": "%s · Completed · %s ✓",
        "partial": "%s · Partially complete · %s ⚠",
        "failed": "%s · Failed · %s ✗",
        "cancelling": "%s · Stopping · %s ⏱",
        "cancelled": "%s · Cancelled · %s",
        "reviewing": "%s · Auditing · %s ⏱",
    },
    "auditor_status": {
        "queued": "Queued",
        "pending": "Pending",
        "running": "Auditing",
        "completed": "Completed",
        "partial": "Partially complete",
        "failed": "Failed",
        "cancelling": "Stopping",
        "cancelled": "Cancelled",
    },
}

# GE follow-up-chat page-side JS strings (launcher._GE_CHAT_JS). Python resolves the locale once and
# injects THIS dict as JSON into `var S`; the JS no longer reads document.documentElement.lang. Key
# shape mirrors zh_CN.GE_CHAT exactly (parity test recurses).
GE_CHAT = {
    "send": "Send",
    "retry": "Check again",
    "free": "Free",
    "credits": "credits",
    "image": {
        "dialog": "Selected image preview", "close": "Close image preview",
        "selectedPrefix": "Selected image ", "selectedSuffix": "",
        "viewPrefix": "View selected image ", "viewSuffix": "",
        "sentPrefix": "Sent image ", "sentSuffix": "",
        "sentViewPrefix": "View sent image ", "sentViewSuffix": "",
        "removePrefix": "Remove selected image ", "removeSuffix": "",
    },
    "error": {
        "capability_disabled": "Follow-up chat is disabled.",
        "server_unsupported": "This server does not support follow-up chat.",
        "client_unsupported": "This client does not support that action.",
        "auth_required": "Reopen this graphic from the host to continue.",
        "privacy_confirmation_required": "Follow-up chat is unavailable for this server configuration.",
        "invalid_request": "Check the question and images, then try again.",
        "input_too_large": "The question or images are too large.",
        "turn_in_flight": "Another follow-up is still running.",
        "conversation_busy": "This conversation is busy. Check again shortly.",
        "conversation_expired": "This follow-up conversation has expired.",
        "rate_limited": "Too many requests. Try again shortly.",
        "insufficient_credits": "There are not enough credits for this follow-up.",
        "model_unavailable": "The model is temporarily unavailable.",
        "timeout": "The follow-up timed out. Check again.",
        "init_timeout": "Initialization timed out. Please try again.",
        "cancelled": "The follow-up was cancelled.",
        "network_error": "The network is unavailable. Check again.",
        "bad_response": "The follow-up service returned an invalid response.",
        "not_found": "This follow-up conversation is no longer available.",
        "chat_unavailable": "Follow-up is unavailable.",
    },
    "state": {
        "capability_checking": "Checking availability", "unavailable": "Follow-up is unavailable.",
        "creating_conversation": "Starting follow-up", "restoring_history": "Restoring history",
        "ready": "Ready", "submitting": "Sending", "queued": "Queued", "running": "Working",
        "calling_model": "Working", "completed": "Completed", "failed": "Follow-up failed.",
        "cancelled": "Cancelled", "recovering": "Connection interrupted. Check again.",
    },
}

# GE header/region-chrome page-side JS strings (launcher._GE_CHROME_JS). Same injection model as GE_CHAT.
GE_CHROME = {
    "shot_ok": "Screenshot copied",
    "shot_na": "Screenshot is only available in the popup",
    "shot_err": "Screenshot failed — try again",
    "shot_uns": "Screenshot to clipboard is not supported here",
    "share_na": "Share is only available in the popup",
    "share_err": "Share failed — try again",
    "share_uns": "Share is not supported here",
    "arm": "Drag a box to ask about that part of the figure",
    "reg_na": "Region-ask is only available in the popup",
    "reg_err": "Couldn't capture the region — try again",
    "reg_uns": "Region-ask is not supported here",
    "reg_add": "Added to your follow-up — type a question to send",
    "reg_reject": "Image limit reached (max 5) — not added",
}

# Native window shell — frameless-header controls (Windows chrome) and the macOS menu-bar
# tray tooltip. `tray_toggle` takes the window title via %s.
SHELL = {
    "maximize": "Maximize",
    "restore": "Resize",
    "tray_toggle": "%s (Click: show / hide)",
    "surface_title": {
        "audit": "Decision Engine - Audit",
        "diagram": "Decision Engine - Graphic Explanation",
        "comic": "Decision Engine - Comic Explanation",
        "infographic": "Decision Engine - Infographic",
        "discussion_board": "Decision Engine - Discussion Board",
    },
}

# Agent-launched permanent device-setup form (installer/permanent_setup.py). ONE table for both the
# pywebview HTML, tkinter fallback, and stable top-level result copy so the backends never drift.
# `title` is the product brand (identical across languages by design); `window_title` is the OS
# titlebar. Every error/result string is self-authored user-facing copy — NEVER a server-reflected
# value — keyed by a stable slug that installer/permanent_setup.py looks up.
PERMANENT_SETUP = {
    # BCP-47 subtag for the rendered document's <html lang>; drives webview CJK font selection and
    # screen-reader pronunciation. NOT the resolver's "en-US"/"zh-CN" tag — it is the lang attribute.
    "html_lang": "en",
    "window_title": "Decision Engine permanent setup",
    "title": "Decision Engine",
    "subtitle": "Connect this device to your workspace",
    "endpoint_label": "Owner-issued endpoint",
    "endpoint_hint": "Use the HTTPS address provided by the Decision Engine owner.",
    "secret_label": "Activation key",
    "secret_hint": "Used once for activation and never stored on this device.",
    "secret_placeholder": "Enter activation key",
    "cancel": "Cancel",
    "activate": "Activate",
    "activating": "Activating…",
    "setup_failed_title": "Decision Engine setup failed",
    "setup_recovery_title": "Decision Engine setup needs recovery",
    "setup_success_title": "Decision Engine permanent setup",
    "setup_succeeded": (
        "Permanent setup succeeded. The activation key was not retained. "
        "Fully restart the Agent; future restarts require no repeated input."
    ),
    "already_activated_success": (
        "This device was already permanently activated. MCP wiring and Doctor are ready. "
        "Fully restart the Agent to load Decision Engine."
    ),
    "success_with_host_failures": (
        "Permanent setup succeeded for the other Agent clients. "
        "These clients still need repair: %(failed_hosts)s. Fully restart the successfully "
        "configured Agents."
    ),
    "doctor_failure": (
        "Device credentials and MCP wiring are saved, but Doctor still reports "
        "a failure. Fix the item reported in the terminal output, then rerun "
        "permanent setup."
    ),
    "activation_failed_safe": (
        "Activation failed. Check the endpoint, network, and activation key, then try again."
    ),
    "activation_incomplete": "Activation did not complete. Check the values and try again.",
    "activation_recovery_required": (
        "The service may already have accepted this device, but local credentials could not "
        "be saved. Preserve the installation and use the owner-guided recovery flow."
    ),
    "unexpected_setup_failure": (
        "Setup failed before it could finish. No credential details were shown. "
        "Review the terminal output, fix the reported item, then try again."
    ),
    "errors": {
        "endpoint_invalid": "Enter a valid HTTPS endpoint.",
        "endpoint_too_long": "Endpoint is too long.",
        "endpoint_required": "Endpoint is required.",
        "secret_invalid": "Enter a valid activation key.",
        "secret_too_long": "Activation key is too long.",
        "secret_required": "Activation key is required.",
        "secret_rejected": "Activation key is invalid or expired. Check the key and try again.",
        "rate_limited": "Too many activation attempts. Wait a moment and try again.",
        "rejected": "Activation was rejected. Check the endpoint and activation key.",
        "service_unavailable": "The activation service is temporarily unavailable. Try again later.",
        "activation_failed": "Activation failed. Check the endpoint, network, and activation key, then try again.",
        "activation_incomplete": "Activation did not complete. Check the values and try again.",
        "in_progress": "Activation is already in progress.",
        "bridge_error": "Unable to complete activation. Check the values and try again.",
        "generic": "Check the values and try again.",
    },
}

# MCP tool DESCRIPTIONS for the installer shim (installer/shim.py). The shim owns each tool's
# STRUCTURE (name / inputSchema types / enum / required); this table owns only the human-readable
# `description` + per-param descriptions it overlays at tools/list assembly. Keys are the wire tool
# names and are NOT translated; enum values / numeric bounds / {status:...} tokens live in the
# schema, never here. zh_CN.MCP_TOOLS mirrors this shape (parity test recurses).
MCP_TOOLS = {
    "activation_required": {
        "description": "Activate this device before using Decision Engine tools.",
        "params": {},
    },
    "audit_skill_submit": {
        "description": "Start an advisory-only DE Lite local defect review in the current interactive agent session. This does not contact Decision Engine Hub.",
        "params": {},
    },
    "audit_skill_complete": {
        "description": "Finish the current DE Lite local advisory. This updates only the local stopper state, does not contact Decision Engine Hub, and never closes an audit gate.",
        "params": {},
    },
    "service_unavailable": {
        "description": "The Decision Engine Hub is unreachable. Reconnect and retry.",
        "params": {},
    },
    "open_ge_popup": {
        "description": "Open the server-rendered graphic-explanation artifact (by run_id) in a native popup on the user's machine. Returns {status, popup_id}. The image bytes are fetched client-side and never returned to you — do not inline.",
        "params": {
            "run_id": "The visual_render run_id.",
            "title": "Short window title (optional).",
            "context": "Conversation context for the in-popup follow-up chat (optional; slice ③).",
        },
    },
    "open_ge": {
        "description": "Open a server-rendered graphic-explanation in a native popup in ONE call: for comic, infographic, or diagram, this tool submits the render, waits for it, fetches the finished artifact client-side, and opens the popup — you do NOT poll or call open_ge_popup. Returns {status, popup_id} when opened; the artifact bytes are never returned to you — do not inline. This call waits up to 10 minutes for a terminal server status. Only if the run is still pending after that ceiling does it return {status:'scheduled', run_id} and continue in a bounded best-effort worker. If submission outcome is unknown, it returns {status:'request_outcome_unknown', retryable:true, client_request_id}; retry only with that exact client_request_id. After submission, any failed result includes run_id for diagnosis and recovery.",
        "params": {
            "mode": "The server render mode; every mode uses the one-call open_ge flow.",
            "spec": "The user-content spec for the mode — see the visual_render tool's inputSchema.",
            "client_request_id": "Optional DE idempotency key; surrounding whitespace is removed before first use. After an uncertain submit, reuse the exact normalized value returned by the response.",
            "title": "Short window title (optional).",
            "context": "Conversation context for the in-popup follow-up chat (optional).",
        },
    },
    "open_db_board": {
        "description": "Open a discussion board (user-content spec) as an interactive native popup for hand-editing. Returns {status, popup_id}; poll db_board_result for the edited board.",
        "params": {
            "spec": "The board spec (columns/notes/etc.) — user content only.",
            "title": "Short window title (optional).",
        },
    },
    "db_board_result": {
        "description": "Bounded poll (≤55s) for a discussion-board popup's outcome. Returns {status:'open'} (poll again), {status:'done', board} (the user's edits), {status:'dismissed'}, or {status:'unknown'}. The board stays retrievable by popup_id until it expires.",
        "params": {
            "wait_s": "Max seconds to block (default 50, capped < 60).",
        },
    },
}

# JSON-RPC -32001 error.message prose for the installer shim (installer/shim.py). ONLY the two
# model-facing sentences live here; the terse machine slugs ("explicit audit topic required",
# "local audit completion rejected") stay English in the shim because they are paired with a
# structured ``{status, reason}`` payload and are read by code, not by a person.
#   * Keys are internal slugs, never wire values.
#   * ``activation_required`` keeps its ``activation_required:`` prefix VERBATIM in every language —
#     it is the same token the accompanying ``data.status`` carries, so callers may match on it.
#   * error CODES live in the shim, never here.
MCP_ERRORS = {
    "service_unavailable": "Decision Engine service unavailable — reconnect and retry",
    "activation_required": (
        "activation_required: activate this device before using Decision Engine tools"
    ),
}
