"""Smoke test for the GE popup's frameless-header chrome in ``render_artifact_html``.

This repo is the SOURCE OF TRUTH for the popup chrome (the server-side mirror deliberately does NOT
carry it — see the server-side design notes on popup chrome drift convergence, Direction 1). That
split left a COVERAGE GAP: nothing pinned the frameless header / region-ask / screenshot / share /
``.ge-signature`` markers that users actually see, so a regression here would ship silently. This smoke
test closes that gap by asserting the chrome markers are present in the rendered popup body.

It is intentionally a marker-level smoke (not a pixel/behaviour test): it renders a bare artifact spec
headlessly (no pywebview / window) and checks the control surface + signature are wired into the HTML.

Run (stdlib only, from the repo root):

    python3 -m unittest client.popup.tests.test_frameless_chrome_smoke
"""

from __future__ import annotations

import unittest

from client.popup.launcher import PopupSpec, render_artifact_html

_SVG = "<svg xmlns=\"http://www.w3.org/2000/svg\"><rect width=\"10\" height=\"10\"/></svg>"
_REAL_GE_SHAPE_SVG = (
    "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 1280 440\">"
    "<rect width=\"100%\" height=\"100%\" fill=\"#fff\"/>"
    "<text x=\"640\" y=\"220\">real GE diagram shape</text></svg>"
)
# The full-wordmark signature (client PR #25). Kept as one literal so a truncation regression trips.
_SIGNATURE = "—— DeepPattern · Decision Engine · Graphic Explanation ——"


def _render(title="T"):
    return render_artifact_html(PopupSpec(kind="ge", title=title,
                                          artifact={"kind": "svg", "data": _SVG}))


class FramelessChromeSmoke(unittest.TestCase):
    def test_five_header_controls_present(self):
        html = _render()
        # The frameless header IS the window's control surface (there is no OS title bar): region-ask,
        # screenshot, share, hide, close — all wired to the injected js_api.
        for control_id in ("region-btn", "shot-btn", "share-btn", "hide-btn", "close-btn"):
            self.assertIn('id="%s"' % control_id, html, "missing frameless control: %s" % control_id)

    def test_signature_wordmark_present_and_full(self):
        html = _render()
        self.assertIn("ge-signature", html)          # the signature row (rides into every screenshot/share)
        self.assertIn(_SIGNATURE, html)              # the FULL wordmark, not a bare "Decision Engine"

    def test_region_ask_and_capture_wiring_present(self):
        html = _render()
        # region-ask arms a drag box on the visual; screenshot/share capture the .artifact rect.
        self.assertIn("ge-region-box", html)         # the drag-selection overlay region-ask draws
        self.assertIn("artifact", html)              # the captured visual pane (screenshot/share target)

    def test_windows_capture_requests_include_live_viewport_dimensions(self):
        html = _render()
        # The Windows compositor PNG can differ from CSS pixels at any DPI/window state. Both the whole
        # artifact and a selected region must send the live viewport dimensions used for strict scaling.
        self.assertIn("r.width, r.height, window.innerWidth, window.innerHeight", html)
        self.assertIn("r.w, r.h, window.innerWidth, window.innerHeight", html)

    def test_header_controls_use_one_uniform_gap(self):
        html = _render()
        self.assertIn("header.pywebview-drag-region{flex:0 0 auto;display:flex;align-items:center;gap:12px;", html)
        for selector in ("header .tool-btn", "header .hide-btn"):
            rule = html.split(selector + "{", 1)[1].split("}", 1)[0]
            self.assertNotIn("margin", rule, selector)

    def test_ge_svg_has_explicit_viewport_sizing_inside_visual_pane(self):
        html = render_artifact_html(PopupSpec(
            kind="ge", title="DE Diagram Smoke",
            artifact={"kind": "svg", "data": _REAL_GE_SHAPE_SVG},
        ))

        self.assertIn("viewBox=\"0 0 1280 440\"", html)
        self.assertIn(".ge-vis svg{display:block;width:100%;height:100%;max-width:100%;max-height:100%}", html)
        self.assertIn(".ge-vis img{display:block;max-width:100%;max-height:100%;height:auto}", html)
        self.assertNotIn(".ge-vis svg,.ge-vis img{max-width:100%;max-height:100%;height:auto}", html)

    def test_old_submit_dismiss_chrome_is_gone(self):
        html = _render()
        # #22 dropped the Submit/Dismiss buttons for the frameless header. Their absence is what makes
        # this the client SOT design (and distinguishes it from the server's retired mirror).
        self.assertNotIn("de-popup-submit", html)
        self.assertNotIn("de-popup-dismiss", html)

    def test_missing_artifact_still_fails_closed(self):
        # the render-failed contract (shared with the server seam) must survive the frameless chrome.
        with self.assertRaises(ValueError):
            render_artifact_html(PopupSpec(kind="ge", title="T"))  # artifact=None


if __name__ == "__main__":
    unittest.main()
