"""Cross-platform (Windows / Linux) audit Stop Panel for the Decision Engine client.

macOS ships a native Swift panel (`desktop/macos/DecisionEngineStopper.swift`); this package
is the tkinter equivalent for platforms without it. It mirrors the Swift panel's COLLAPSED
render (audit tier + overall status + live elapsed; per-voice detail is deliberately hidden —
the panel size / voice roster is a privacy surface) and reuses ``client.runner`` for all hub
I/O (poll + cancel) and config / active-run access.
"""
