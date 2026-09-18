"""Unit tests for the pure (AppKit-free) helpers behind the GE popup's Screenshot / Share / region-ask
capture. The Cocoa work in native_shell is lazy-imported inside functions, so this module imports and
exercises the pure math + the ObjC-callback crash-safety boundary WITHOUT pywebview / pyobjc / a window.

Run (stdlib only, from the repo root):

    python3 -m unittest client.popup.tests.test_visual_capture
"""

from __future__ import annotations

import base64
import struct
import threading
import sys
import tempfile
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

from client.popup import native_shell as ns


class NormRectTests(unittest.TestCase):
    """`_norm_rect`: a page-supplied [x,y,w,h] → validated float tuple, or None (→ full-view snapshot)."""

    def test_valid_rect_becomes_float_tuple(self):
        self.assertEqual(ns._norm_rect([10, 20, 100, 50]), (10.0, 20.0, 100.0, 50.0))

    def test_numeric_strings_are_coerced(self):
        # the JS bridge may deliver stringified numbers; float() accepts them
        self.assertEqual(ns._norm_rect(["10", "20", "30", "40"]), (10.0, 20.0, 30.0, 40.0))

    def test_negative_origin_is_allowed(self):
        # a region can start off the top-left of the view (clamped later); only w/h must be positive
        self.assertEqual(ns._norm_rect([-5, -8, 12, 12]), (-5.0, -8.0, 12.0, 12.0))

    def test_zero_or_negative_size_is_rejected(self):
        for bad in ([0, 0, 0, 10], [0, 0, 10, 0], [0, 0, -3, 10], [0, 0, 10, -3]):
            self.assertIsNone(ns._norm_rect(bad), bad)

    def test_malformed_input_is_none_not_raise(self):
        for bad in (None, [1, 2, 3], [], [1, 2, "x", 4], "nope", 42, {"x": 1}):
            self.assertIsNone(ns._norm_rect(bad), bad)

    def test_trailing_windows_viewport_values_do_not_change_macos_rect(self):
        self.assertEqual(
            ns._norm_rect([10, 20, 100, 50, 900, 640]),
            (10.0, 20.0, 100.0, 50.0),
        )


class WindowsCaptureRequestTests(unittest.TestCase):
    """Windows captures require page viewport dimensions so DPI is derived from real pixels."""

    def test_valid_request_includes_rect_and_viewport(self):
        self.assertEqual(
            ns._norm_windows_capture_request([10, 20, 100, 50, 900, 640]),
            ((10.0, 20.0, 100.0, 50.0), (900.0, 640.0)),
        )

    def test_rejects_missing_nonfinite_tiny_or_out_of_bounds_request(self):
        bad_requests = (
            [10, 20, 100, 50],
            [10, 20, 100, 50, 0, 640],
            [10, 20, float("nan"), 50, 900, 640],
            [10, 20, 7.99, 50, 900, 640],
            [-1, 20, 100, 50, 900, 640],
            [850, 20, 100, 50, 900, 640],
            [10, 620, 100, 50, 900, 640],
        )
        for request in bad_requests:
            with self.subTest(request=request):
                self.assertIsNone(ns._norm_windows_capture_request(request))

    def test_edge_aligned_request_is_valid(self):
        self.assertEqual(
            ns._norm_windows_capture_request([800, 590, 100, 50, 900, 640]),
            ((800.0, 590.0, 100.0, 50.0), (900.0, 640.0)),
        )


class WindowsPixelMappingTests(unittest.TestCase):
    def test_css_rect_maps_from_actual_png_dimensions_at_common_dpi_scales(self):
        rect = (80.0, 40.0, 320.0, 200.0)
        viewport = (800.0, 600.0)
        expected = {
            1.0: (80, 40, 400, 240),
            1.25: (100, 50, 500, 300),
            1.5: (120, 60, 600, 360),
            2.0: (160, 80, 800, 480),
        }
        for scale, box in expected.items():
            with self.subTest(scale=scale):
                self.assertEqual(
                    ns._windows_crop_box(rect, viewport, (round(800 * scale), round(600 * scale))),
                    box,
                )

    def test_fractional_edges_shrink_inward_to_preserve_privacy(self):
        self.assertEqual(
            ns._windows_crop_box((0.5, 1.25, 10.2, 8.1), (100.0, 80.0), (125, 100)),
            (1, 2, 13, 11),
        )

    def test_nonuniform_compositor_scale_fails_closed(self):
        self.assertIsNone(
            ns._windows_crop_box((10, 10, 80, 60), (800, 600), (1000, 740))
        )

    def test_invalid_or_out_of_bounds_mapping_fails_closed(self):
        for rect, viewport, pixels in (
            ((-1, 0, 8, 8), (100, 100), (100, 100)),
            ((95, 0, 8, 8), (100, 100), (100, 100)),
            ((0, 0, 8, 8), (0, 100), (100, 100)),
            ((0, 0, 8, 8), (100, 100), (0, 100)),
        ):
            with self.subTest(rect=rect, viewport=viewport, pixels=pixels):
                self.assertIsNone(ns._windows_crop_box(rect, viewport, pixels))


class WindowsLiveViewportTests(unittest.TestCase):
    def test_hung_page_evaluation_is_poisoned_until_late_completion(self):
        release = threading.Event()
        win = mock.Mock()
        win.evaluate_js.side_effect = lambda _script: release.wait() or [1]
        self.assertIsNone(ns._evaluate_windows_js_bounded(win, "1", timeout=0.005))
        started = time.monotonic()
        self.assertIsNone(ns._evaluate_windows_js_bounded(win, "1", timeout=0.1))
        self.assertLess(time.monotonic() - started, 0.03)
        release.set()
        deadline = time.monotonic() + 0.2
        while ns._WINDOWS_EVAL_POISONED and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertFalse(ns._WINDOWS_EVAL_POISONED)

    def test_live_artifact_contract_matches_exact_or_contained_request(self):
        win = mock.Mock()
        win.evaluate_js.return_value = [10, 20, 500, 400, 900, 640]
        artifact = ((10.0, 20.0, 500.0, 400.0), (900.0, 640.0))
        region = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        chat = ((700.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        self.assertTrue(ns._windows_page_capture_matches(win, artifact, artifact_only=True))
        self.assertTrue(ns._windows_page_capture_matches(win, region, artifact_only=False))
        self.assertFalse(ns._windows_page_capture_matches(win, chat, artifact_only=False))
        win.evaluate_js.return_value = [11, 20, 500, 400, 900, 640]
        self.assertFalse(ns._windows_page_capture_matches(win, artifact, artifact_only=True))
        win.evaluate_js.side_effect = RuntimeError("page closed")
        self.assertFalse(ns._windows_page_capture_matches(win, artifact, artifact_only=True))

    def test_privacy_mask_preserves_requested_outer_box_for_content_box_artifacts(self):
        win = mock.Mock()
        win.evaluate_js.return_value = True
        request = ((22.0, 105.0, 482.0, 476.0), (900.0, 640.0))
        self.assertTrue(ns._install_windows_capture_privacy_mask(win, request))
        script = win.evaluate_js.call_args.args[0]
        self.assertIn("a.style.boxSizing='border-box'", script)

    def test_region_artifact_request_keeps_live_artifact_separate_from_crop(self):
        win = mock.Mock()
        win.evaluate_js.return_value = [
            10, 20, 500, 400,
            10, 20, 500, 400,
            900, 640,
        ]
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))

        self.assertEqual(
            ns._windows_region_artifact_request(win, crop),
            (
                (10.0, 20.0, 500.0, 400.0),
                (10.0, 20.0, 500.0, 400.0),
                (900.0, 640.0),
            ),
        )

    def test_region_artifact_request_rejects_crop_outside_live_artifact(self):
        win = mock.Mock()
        win.evaluate_js.return_value = [
            10, 20, 500, 400,
            10, 20, 500, 400,
            900, 640,
        ]
        crop = ((700.0, 40.0, 100.0, 80.0), (900.0, 640.0))

        self.assertIsNone(ns._windows_region_artifact_request(win, crop))

    def test_region_artifact_request_rejects_crop_clipped_by_ancestor(self):
        win = mock.Mock()
        win.evaluate_js.return_value = [
            10, 20, 500, 400,
            10, 100, 500, 300,
            900, 640,
        ]
        crop = ((30.0, 40.0, 100.0, 40.0), (900.0, 640.0))

        self.assertIsNone(ns._windows_region_artifact_request(win, crop))

    def test_region_artifact_request_accepts_visible_crop_in_partially_offscreen_artifact(self):
        win = mock.Mock()
        win.evaluate_js.return_value = [
            -10, -40, 500, 700,
            0, 0, 490, 640,
            900, 640,
        ]
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))

        self.assertEqual(
            ns._windows_region_artifact_request(win, crop),
            (
                (-10.0, -40.0, 500.0, 700.0),
                (0.0, 0.0, 490.0, 640.0),
                (900.0, 640.0),
            ),
        )

    def test_region_capture_state_requires_live_mask_top_layer_clear_and_exact_geometry(self):
        win = mock.Mock()
        artifact = (
            (10.0, 20.0, 500.0, 400.0),
            (10.0, 20.0, 500.0, 400.0),
            (900.0, 640.0),
        )
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        win.evaluate_js.return_value = [
            10, 20, 500, 400,
            10, 20, 500, 400,
            900, 640, True,
        ]

        self.assertTrue(ns._windows_region_capture_matches(win, artifact, crop))
        win.evaluate_js.return_value = [
            10.1, 20, 500, 400,
            10, 20, 500, 400,
            900, 640, True,
        ]
        self.assertFalse(ns._windows_region_capture_matches(win, artifact, crop))
        win.evaluate_js.return_value = [
            10, 20, 500, 400,
            10, 20.1, 500, 400,
            900, 640, True,
        ]
        self.assertFalse(ns._windows_region_capture_matches(win, artifact, crop))
        win.evaluate_js.return_value = [
            10, 20, 500, 400,
            10, 20, 500, 400,
            900, 640, False,
        ]
        self.assertFalse(ns._windows_region_capture_matches(win, artifact, crop))

    def test_region_privacy_mask_covers_outside_without_reparenting_artifact(self):
        win = mock.Mock()
        win.evaluate_js.return_value = True
        artifact = (
            (22.0, 85.0, 482.0, 516.0),
            (22.0, 105.0, 482.0, 476.0),
            (900.0, 640.0),
        )

        self.assertTrue(ns._install_windows_region_capture_privacy_mask(win, artifact))
        script = win.evaluate_js.call_args.args[0]
        self.assertNotIn("root.appendChild(a)", script)
        self.assertNotIn("a.style.", script)
        self.assertIn("document.fullscreenElement", script)
        self.assertIn("dialog[open]", script)
        self.assertIn("function cover", script)
        self.assertIn("background:#fff", script)
        self.assertIn("setTimeout", script)

        win.reset_mock()
        win.evaluate_js.return_value = True
        self.assertTrue(ns._remove_windows_region_capture_privacy_mask(win))
        remove_script = win.evaluate_js.call_args.args[0]
        self.assertIn("setProperty('display','none','important')", remove_script)

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_resize_during_capture_drops_crop_instead_of_risking_chat_leak(self):
        request = ((10.0, 20.0, 100.0, 50.0), (900.0, 640.0))
        with (
            mock.patch.object(ns, "_windows_page_capture_matches", side_effect=[True, False]),
            mock.patch.object(ns, "_install_windows_capture_privacy_mask", return_value=True),
            mock.patch.object(ns, "_remove_windows_capture_privacy_mask") as remove,
            mock.patch.object(ns, "_capture_windows_webview_png", return_value=b"full"),
            mock.patch.object(ns, "_png_dimensions", return_value=(900, 640)),
            mock.patch.object(ns, "_windows_crop_box", return_value=(10, 20, 110, 70)),
            mock.patch.object(ns, "_crop_windows_png", return_value=b"crop"),
        ):
            self.assertIsNone(ns._snapshot_windows_png(
                object(), request, threading.Lock(), artifact_only=False))
        remove.assert_called_once()


class WindowsPngValidationTests(unittest.TestCase):
    def test_png_dimensions_accept_real_png_header(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (12).to_bytes(4, "big") + (34).to_bytes(4, "big")
        self.assertEqual(ns._png_dimensions(png), (12, 34))

    def test_png_dimensions_reject_truncated_or_absurd_payload(self):
        for payload in (b"", b"not-png", b"\x89PNG\r\n\x1a\n", b"\x89PNG\r\n\x1a\n" + b"x" * 40):
            with self.subTest(payload=payload):
                self.assertIsNone(ns._png_dimensions(payload))

    def test_png_dimensions_reject_decoded_area_above_capture_budget(self):
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
               (10000).to_bytes(4, "big") + (5000).to_bytes(4, "big"))
        self.assertIsNone(ns._png_dimensions(png))

    @unittest.skipUnless(sys.platform == "win32", "System.Drawing PNG validation is Windows-only")
    def test_crop_accepts_visible_pixel_outside_sparse_sample_points(self):
        import clr
        clr.AddReference("System.Drawing")
        from System.Drawing import Bitmap, Color
        from System.Drawing.Imaging import ImageFormat, PixelFormat
        from System.IO import MemoryStream

        bitmap = Bitmap(8, 8, PixelFormat.Format32bppArgb)
        bitmap.SetPixel(1, 1, Color.FromArgb(255, 12, 34, 56))
        stream = MemoryStream()
        try:
            bitmap.Save(stream, ImageFormat.Png)
            png = bytes(stream.ToArray())
        finally:
            stream.Dispose()
            bitmap.Dispose()
        self.assertIsNotNone(ns._crop_windows_png(png, (0, 0, 8, 8)))


class WindowsPlatformDispatchTests(unittest.TestCase):
    def setUp(self):
        self.api = ns.PopupApi("/tmp/de-popup-test-result.json")
        self.api._win = object()

    @mock.patch.object(ns, "_IS_MAC", False)
    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_snapshot_region_returns_bounded_data_url(self):
        with mock.patch.object(ns, "_snapshot_windows_region_png", return_value=b"png-bytes") as capture:
            result = self.api.snapshot_region([10, 20, 100, 50, 900, 640])
        self.assertTrue(result["ok"])
        self.assertEqual(result["image"], "data:image/png;base64,cG5nLWJ5dGVz")
        capture.assert_called_once_with(
            self.api._win,
            ((10.0, 20.0, 100.0, 50.0), (900.0, 640.0)),
            self.api._window_action_lock,
        )

    @mock.patch.object(ns, "_IS_MAC", False)
    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_share_keeps_original_artifact_capture_path(self):
        with (
            mock.patch.object(ns, "_snapshot_windows_png", return_value=b"png-bytes") as capture,
            mock.patch.object(ns, "_present_windows_share", return_value=True) as share,
        ):
            result = self.api.share_visual_image([10, 20, 100, 50, 900, 640])
        self.assertEqual(result, {"ok": True})
        capture.assert_called_once_with(
            self.api._win,
            ((10.0, 20.0, 100.0, 50.0), (900.0, 640.0)),
            self.api._window_action_lock,
            artifact_only=True,
        )
        share.assert_called_once_with(self.api._win, b"png-bytes")

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_hires_keeps_original_capture_path(self):
        with (
            mock.patch.object(ns, "_snapshot_windows_hires_png", return_value=b"png-bytes") as capture,
            mock.patch.object(ns, "_copy_windows_png_to_clipboard", return_value=True) as copy,
        ):
            result = self.api.copy_visual_image_hires(
                [10, 20, 100, 50, 900, 640], factor=2
            )
        self.assertEqual(result, {"ok": True})
        capture.assert_called_once_with(
            self.api._win,
            ((10.0, 20.0, 100.0, 50.0), (900.0, 640.0)),
            self.api._window_action_lock,
            2,
        )
        copy.assert_called_once_with(self.api._win, b"png-bytes")

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_region_snapshot_crops_selection_from_unscaled_artifact(self):
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        artifact = ((10.0, 20.0, 500.0, 400.0), (900.0, 640.0))
        with (
            mock.patch.object(ns, "_windows_region_artifact_request", return_value=artifact),
            mock.patch.object(ns, "_install_windows_region_capture_privacy_mask", return_value=True) as install,
            mock.patch.object(
                ns, "_remove_windows_region_capture_privacy_mask", return_value=True
            ) as remove,
            mock.patch.object(ns, "_windows_region_capture_matches", return_value=True),
            mock.patch.object(ns, "_capture_windows_webview_png", return_value=b"full"),
            mock.patch.object(ns, "_png_dimensions", return_value=(900, 640)),
            mock.patch.object(ns, "_windows_crop_box", return_value=(30, 40, 130, 120)) as box,
            mock.patch.object(ns, "_crop_windows_png", return_value=b"crop"),
        ):
            self.assertEqual(
                ns._snapshot_windows_region_png(object(), crop, threading.Lock()), b"crop"
            )
        install.assert_called_once_with(mock.ANY, artifact)
        box.assert_called_once_with(crop[0], crop[1], (900, 640))
        remove.assert_called_once()

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_region_snapshot_discards_pixels_when_post_capture_privacy_state_changes(self):
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        artifact = ((10.0, 20.0, 500.0, 400.0), (900.0, 640.0))
        with (
            mock.patch.object(ns, "_windows_region_artifact_request", return_value=artifact),
            mock.patch.object(ns, "_install_windows_region_capture_privacy_mask", return_value=True),
            mock.patch.object(
                ns, "_remove_windows_region_capture_privacy_mask", return_value=True
            ) as remove,
            mock.patch.object(
                ns, "_windows_region_capture_matches", side_effect=[True, False]
            ) as matches,
            mock.patch.object(ns, "_capture_windows_webview_png", return_value=b"full"),
            mock.patch.object(ns, "_png_dimensions", return_value=(900, 640)),
            mock.patch.object(ns, "_crop_windows_png", return_value=b"crop") as crop_png,
        ):
            self.assertIsNone(
                ns._snapshot_windows_region_png(object(), crop, threading.Lock())
            )
        self.assertEqual(matches.call_count, 2)
        crop_png.assert_not_called()
        remove.assert_called_once()

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_region_snapshot_restores_mask_when_capture_fails(self):
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        artifact = ((10.0, 20.0, 500.0, 400.0), (900.0, 640.0))
        with (
            mock.patch.object(ns, "_windows_region_artifact_request", return_value=artifact),
            mock.patch.object(ns, "_install_windows_region_capture_privacy_mask", return_value=True),
            mock.patch.object(
                ns, "_remove_windows_region_capture_privacy_mask", return_value=True
            ) as remove,
            mock.patch.object(ns, "_windows_region_capture_matches", return_value=True),
            mock.patch.object(ns, "_capture_windows_webview_png", return_value=None),
            mock.patch.object(ns, "_png_dimensions", return_value=None),
        ):
            self.assertIsNone(
                ns._snapshot_windows_region_png(object(), crop, threading.Lock())
            )
        remove.assert_called_once()

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_region_snapshot_attempts_cleanup_after_ambiguous_install_failure(self):
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        artifact = ((10.0, 20.0, 500.0, 400.0), (900.0, 640.0))
        with (
            mock.patch.object(ns, "_windows_region_artifact_request", return_value=artifact),
            mock.patch.object(ns, "_install_windows_region_capture_privacy_mask", return_value=False),
            mock.patch.object(
                ns, "_remove_windows_region_capture_privacy_mask", return_value=True
            ) as remove,
        ):
            self.assertIsNone(
                ns._snapshot_windows_region_png(object(), crop, threading.Lock())
            )
        remove.assert_called_once()

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_region_snapshot_retries_cleanup_once(self):
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        artifact = ((10.0, 20.0, 500.0, 400.0), (900.0, 640.0))
        with (
            mock.patch.object(ns, "_windows_region_artifact_request", return_value=artifact),
            mock.patch.object(ns, "_install_windows_region_capture_privacy_mask", return_value=True),
            mock.patch.object(
                ns, "_remove_windows_region_capture_privacy_mask",
                side_effect=[False, True],
            ) as remove,
            mock.patch.object(ns, "_windows_region_capture_matches", return_value=True),
            mock.patch.object(ns, "_capture_windows_webview_png", return_value=b"full"),
            mock.patch.object(ns, "_png_dimensions", return_value=(900, 640)),
            mock.patch.object(ns, "_windows_crop_box", return_value=(30, 40, 130, 120)),
            mock.patch.object(ns, "_crop_windows_png", return_value=b"crop"),
        ):
            self.assertEqual(
                ns._snapshot_windows_region_png(object(), crop, threading.Lock()), b"crop"
            )
        self.assertEqual(remove.call_count, 2)

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_region_snapshot_fails_closed_when_mask_restore_fails(self):
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        artifact = ((10.0, 20.0, 500.0, 400.0), (900.0, 640.0))
        with (
            mock.patch.object(ns, "_windows_region_artifact_request", return_value=artifact),
            mock.patch.object(ns, "_install_windows_region_capture_privacy_mask", return_value=True),
            mock.patch.object(
                ns, "_remove_windows_region_capture_privacy_mask", return_value=False
            ) as remove,
            mock.patch.object(ns, "_windows_region_capture_matches", return_value=True),
            mock.patch.object(ns, "_capture_windows_webview_png", return_value=b"full"),
            mock.patch.object(ns, "_png_dimensions", return_value=(900, 640)),
            mock.patch.object(ns, "_windows_crop_box", return_value=(30, 40, 130, 120)),
            mock.patch.object(ns, "_crop_windows_png", return_value=b"crop"),
        ):
            self.assertIsNone(
                ns._snapshot_windows_region_png(object(), crop, threading.Lock())
            )
        self.assertEqual(remove.call_count, 2)

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_region_snapshot_contains_unexpected_helper_exception(self):
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        with (
            mock.patch.object(
                ns, "_windows_region_artifact_request", side_effect=RuntimeError("closed")
            ),
            mock.patch.object(
                ns, "_remove_windows_region_capture_privacy_mask", return_value=True
            ) as remove,
        ):
            self.assertIsNone(
                ns._snapshot_windows_region_png(object(), crop, threading.Lock())
            )
        remove.assert_called()

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_region_snapshot_contains_cleanup_exception_while_lock_is_held(self):
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))

        class TrackingLock:
            held = False

            def __enter__(self):
                self.held = True
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                self.held = False

        lock = TrackingLock()

        def fail_cleanup(_win):
            self.assertTrue(lock.held, "region cleanup escaped window_action_lock")
            raise RuntimeError("cleanup failed")

        with (
            mock.patch.object(
                ns, "_windows_region_artifact_request", side_effect=RuntimeError("closed")
            ),
            mock.patch.object(
                ns, "_remove_windows_region_capture_privacy_mask", side_effect=fail_cleanup
            ) as remove,
        ):
            self.assertIsNone(
                ns._snapshot_windows_region_png(object(), crop, lock)
            )
        self.assertEqual(remove.call_count, 2)

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_region_snapshot_does_not_swallow_keyboard_interrupt(self):
        crop = ((30.0, 40.0, 100.0, 80.0), (900.0, 640.0))
        with (
            mock.patch.object(
                ns, "_windows_region_artifact_request", side_effect=KeyboardInterrupt
            ),
            mock.patch.object(
                ns, "_remove_windows_region_capture_privacy_mask", return_value=True
            ) as remove,
        ):
            with self.assertRaises(KeyboardInterrupt):
                ns._snapshot_windows_region_png(object(), crop, threading.Lock())
        remove.assert_called_once()

    @mock.patch.object(ns, "_IS_MAC", False)
    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_snapshot_region_rejects_base64_over_chat_limit(self):
        oversized = b"A" * (ns._MAX_CAPTURE_IMAGE_B64 + 1)
        with (
            mock.patch.object(ns, "_snapshot_windows_region_png", return_value=b"png-bytes"),
            mock.patch.object(
                ns.base64, "b64encode", return_value=oversized
            ) as encode,
        ):
            result = self.api.snapshot_region([10, 20, 100, 50, 900, 640])
        self.assertEqual(result, {"ok": False})
        encode.assert_called_once_with(b"png-bytes")

    @mock.patch.object(ns, "_IS_MAC", False)
    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_copy_requires_successful_native_image_clipboard_write(self):
        with (
            mock.patch.object(ns, "_snapshot_windows_png", return_value=b"png-bytes") as capture,
            mock.patch.object(ns, "_copy_windows_png_to_clipboard", return_value=True) as copy,
        ):
            result = self.api.copy_visual_image([10, 20, 100, 50, 900, 640])
        self.assertEqual(result, {"ok": True})
        capture.assert_called_once_with(
            self.api._win,
            ((10.0, 20.0, 100.0, 50.0), (900.0, 640.0)),
            self.api._window_action_lock,
            artifact_only=True,
        )
        copy.assert_called_once_with(self.api._win, b"png-bytes")

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_snapshot_masks_everything_outside_requested_artifact(self):
        request = ((10.0, 20.0, 100.0, 50.0), (900.0, 640.0))
        with (
            mock.patch.object(ns, "_windows_page_capture_matches", return_value=True),
            mock.patch.object(ns, "_install_windows_capture_privacy_mask", return_value=True) as install,
            mock.patch.object(ns, "_remove_windows_capture_privacy_mask") as remove,
            mock.patch.object(ns, "_capture_windows_webview_png", return_value=b"full"),
            mock.patch.object(ns, "_png_dimensions", return_value=(900, 640)),
            mock.patch.object(ns, "_windows_crop_box", return_value=(10, 20, 110, 70)),
            mock.patch.object(ns, "_crop_windows_png", return_value=b"crop"),
        ):
            self.assertEqual(ns._snapshot_windows_png(
                object(), request, threading.Lock(), artifact_only=True), b"crop")
        install.assert_called_once_with(mock.ANY, request)
        remove.assert_called_once()

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_windows_snapshot_removes_privacy_mask_when_capture_fails(self):
        request = ((10.0, 20.0, 100.0, 50.0), (900.0, 640.0))
        with (
            mock.patch.object(ns, "_windows_page_capture_matches", return_value=True),
            mock.patch.object(ns, "_install_windows_capture_privacy_mask", return_value=True),
            mock.patch.object(ns, "_remove_windows_capture_privacy_mask") as remove,
            mock.patch.object(ns, "_capture_windows_webview_png", return_value=None),
        ):
            self.assertIsNone(ns._snapshot_windows_png(
                object(), request, threading.Lock(), artifact_only=True))
        remove.assert_called_once()

    @mock.patch.object(ns, "_IS_MAC", True)
    @mock.patch.object(ns, "_IS_WINDOWS", False)
    def test_macos_snapshot_path_remains_nsdata_based(self):
        nsdata = mock.Mock()
        nsdata.base64EncodedStringWithOptions_.return_value = "bWFj"
        with (
            mock.patch.object(ns, "_visual_capture_supported", return_value=True),
            mock.patch.object(ns, "_snapshot_png_data", return_value=nsdata) as capture,
        ):
            result = self.api.snapshot_region([10, 20, 100, 50, 900, 640])
        self.assertEqual(result, {"ok": True, "image": "data:image/png;base64,bWFj"})
        capture.assert_called_once()


class WindowsNativeTimeoutTests(unittest.TestCase):
    @staticmethod
    def _minimal_png():
        return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
                (12).to_bytes(4, "big") + (34).to_bytes(4, "big"))

    @staticmethod
    def _fake_module(name, **attributes):
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        return module

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_clipboard_callback_queued_past_timeout_cannot_write(self):
        queued = []
        writes = []

        class FakeNative:
            InvokeRequired = True

            @staticmethod
            def BeginInvoke(callback):
                queued.append(callback)

        class FakeDisposable:
            def __init__(self, *_args):
                pass

            def Dispose(self):
                pass

        class FakeClipboard:
            @staticmethod
            def SetImage(_image):
                writes.append("write")

            @staticmethod
            def ContainsImage():
                return True

        modules = {
            "System": self._fake_module("System", Action=lambda callback: callback),
            "System.Drawing": self._fake_module("System.Drawing", Bitmap=FakeDisposable),
            "System.IO": self._fake_module("System.IO", MemoryStream=FakeDisposable),
            "System.Windows.Forms": self._fake_module("System.Windows.Forms", Clipboard=FakeClipboard),
        }
        win = types.SimpleNamespace(native=FakeNative())
        with mock.patch.dict(sys.modules, modules):
            self.assertFalse(ns._copy_windows_png_to_clipboard(win, self._minimal_png(), timeout=0.001))
            self.assertEqual(len(queued), 1)
            queued[0]()
        self.assertEqual(writes, [])

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_capture_stream_is_disposed_when_capture_start_raises(self):
        streams = []

        class FakeStream:
            def __init__(self):
                self.disposed = False
                streams.append(self)

            def Dispose(self):
                self.disposed = True

        class FakeWebView:
            class Core:
                @staticmethod
                def CapturePreviewAsync(_format, _stream):
                    raise RuntimeError("capture start failed")

            CoreWebView2 = Core()

        class FakeNative:
            InvokeRequired = True
            webview = FakeWebView()

            @staticmethod
            def BeginInvoke(callback):
                callback()

        class FakeAction:
            def __new__(cls, callback):
                return callback

        modules = {
            "System": self._fake_module("System", Action=FakeAction),
            "System.IO": self._fake_module("System.IO", MemoryStream=FakeStream),
            "System.Threading": self._fake_module("System.Threading"),
            "System.Threading.Tasks": self._fake_module("System.Threading.Tasks", Task=object),
            "Microsoft": self._fake_module("Microsoft"),
            "Microsoft.Web": self._fake_module("Microsoft.Web"),
            "Microsoft.Web.WebView2": self._fake_module("Microsoft.Web.WebView2"),
            "Microsoft.Web.WebView2.Core": self._fake_module(
                "Microsoft.Web.WebView2.Core", CoreWebView2CapturePreviewImageFormat=types.SimpleNamespace(Png=1)),
        }
        win = types.SimpleNamespace(native=FakeNative())
        with mock.patch.dict(sys.modules, modules):
            self.assertIsNone(ns._capture_windows_webview_png(win, timeout=0.01))
        self.assertEqual(len(streams), 1)
        self.assertTrue(streams[0].disposed)

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_timed_out_capture_poison_fails_fast_until_late_completion(self):
        callbacks = []
        png = self._minimal_png()

        class FakeStream:
            def ToArray(self):
                return png

            def Dispose(self):
                pass

        class FakeTask:
            IsCanceled = False
            IsFaulted = False

            @staticmethod
            def ContinueWith(callback):
                callbacks.append(callback)

        task = FakeTask()

        class FakeWebView:
            class Core:
                @staticmethod
                def CapturePreviewAsync(_format, _stream):
                    return task

            CoreWebView2 = Core()

        class FakeNative:
            InvokeRequired = True
            webview = FakeWebView()

            @staticmethod
            def BeginInvoke(callback):
                callback()

        class FakeAction:
            def __new__(cls, callback):
                return callback

            @classmethod
            def __class_getitem__(cls, _item):
                return lambda callback: callback

        modules = {
            "System": self._fake_module("System", Action=FakeAction),
            "System.IO": self._fake_module("System.IO", MemoryStream=FakeStream),
            "System.Threading": self._fake_module("System.Threading"),
            "System.Threading.Tasks": self._fake_module("System.Threading.Tasks", Task=FakeTask),
            "Microsoft": self._fake_module("Microsoft"),
            "Microsoft.Web": self._fake_module("Microsoft.Web"),
            "Microsoft.Web.WebView2": self._fake_module("Microsoft.Web.WebView2"),
            "Microsoft.Web.WebView2.Core": self._fake_module(
                "Microsoft.Web.WebView2.Core", CoreWebView2CapturePreviewImageFormat=types.SimpleNamespace(Png=1)),
        }
        win = types.SimpleNamespace(native=FakeNative())
        with mock.patch.dict(sys.modules, modules):
            self.assertIsNone(ns._capture_windows_webview_png(win, timeout=0.005))
            started = time.monotonic()
            self.assertIsNone(ns._capture_windows_webview_png(win, timeout=0.1))
            self.assertLess(time.monotonic() - started, 0.03)
            self.assertEqual(len(callbacks), 1)
            callbacks[0](task)
        self.assertFalse(ns._WINDOWS_CAPTURE_LOCK.locked())

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_capture_completion_at_timeout_boundary_does_not_leave_poison(self):
        callbacks = []
        png = self._minimal_png()

        class FakeEvent:
            def __init__(self):
                self.signaled = False

            def set(self):
                self.signaled = True

            def is_set(self):
                return self.signaled

            def wait(self, _timeout=None):
                callbacks[0](task)
                return False

        class FakeStream:
            def ToArray(self):
                return png

            def Dispose(self):
                pass

        class FakeTask:
            IsCanceled = False
            IsFaulted = False

            @staticmethod
            def ContinueWith(callback):
                callbacks.append(callback)

        task = FakeTask()

        class FakeWebView:
            CoreWebView2 = types.SimpleNamespace(
                CapturePreviewAsync=lambda _format, _stream: task)

        class FakeNative:
            InvokeRequired = True
            webview = FakeWebView()

            @staticmethod
            def BeginInvoke(callback):
                callback()

        class FakeAction:
            def __new__(cls, callback):
                return callback

            @classmethod
            def __class_getitem__(cls, _item):
                return lambda callback: callback

        modules = {
            "System": self._fake_module("System", Action=FakeAction),
            "System.IO": self._fake_module("System.IO", MemoryStream=FakeStream),
            "System.Threading.Tasks": self._fake_module("System.Threading.Tasks", Task=FakeTask),
            "Microsoft.Web.WebView2.Core": self._fake_module(
                "Microsoft.Web.WebView2.Core", CoreWebView2CapturePreviewImageFormat=types.SimpleNamespace(Png=1)),
        }
        win = types.SimpleNamespace(native=FakeNative())
        with (
            mock.patch.dict(sys.modules, modules),
            mock.patch.object(ns.threading, "Event", FakeEvent),
        ):
            self.assertIsNone(ns._capture_windows_webview_png(win, timeout=0.001))
        self.assertFalse(ns._WINDOWS_CAPTURE_POISONED)
        self.assertFalse(ns._WINDOWS_CAPTURE_LOCK.locked())


class WindowsShareLifecycleTests(unittest.TestCase):
    def tearDown(self):
        ns._cleanup_windows_share_at_exit()

    @staticmethod
    def _minimal_png():
        return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (12).to_bytes(4, "big") + (34).to_bytes(4, "big")

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_share_start_failure_removes_temporary_png(self):
        captured = {}

        def fail_start(_win, state):
            captured["path"] = Path(state["path"])
            return False

        with mock.patch.object(ns, "_start_windows_share_ui", side_effect=fail_start):
            self.assertFalse(ns._present_windows_share(object(), self._minimal_png(), timeout=0.01))
        self.assertFalse(captured["path"].exists())
        self.assertIsNone(ns._WINDOWS_SHARE_ACTIVE)

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_share_timeout_removes_temporary_png(self):
        captured = {}

        def never_shown(_win, state):
            captured["path"] = Path(state["path"])
            return True

        with mock.patch.object(ns, "_start_windows_share_ui", side_effect=never_shown):
            self.assertFalse(ns._present_windows_share(object(), self._minimal_png(), timeout=0.01))
        self.assertFalse(captured["path"].exists())
        self.assertIsNone(ns._WINDOWS_SHARE_ACTIVE)

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_share_temp_write_failure_removes_partial_file(self):
        real_mkstemp = ns.tempfile.mkstemp
        captured = {}

        def create_in_test_dir(*, prefix, suffix):
            descriptor, path = real_mkstemp(prefix=prefix, suffix=suffix)
            captured["path"] = Path(path)
            return descriptor, path

        with (
            mock.patch.object(ns.tempfile, "mkstemp", side_effect=create_in_test_dir),
            mock.patch.object(ns.os, "fsync", side_effect=OSError("disk write failed")),
        ):
            self.assertFalse(ns._present_windows_share(object(), self._minimal_png(), timeout=0.01))
        self.assertFalse(captured["path"].exists())
        self.assertIsNone(ns._WINDOWS_SHARE_ACTIVE)

    def test_share_cleanup_detaches_event_handler_once(self):
        calls = []
        state = {
            "path": str(Path(self.id()).with_suffix(".png")),
            "keep": [object()],
            "lock": threading.Lock(),
            "cleaned": False,
            "detach": lambda: calls.append("detached"),
        }
        ns._cleanup_windows_share(state)
        ns._cleanup_windows_share(state)
        self.assertEqual(calls, ["detached"])
        self.assertEqual(state["keep"], [])

    def test_share_cleanup_unsubscribes_every_retained_native_event(self):
        descriptor, path = tempfile.mkstemp(prefix="de-ge-share-cleanup-", suffix=".png")
        ns.os.close(descriptor)
        source = object()
        handler = object()
        state = {
            "path": path,
            "keep": [source, handler],
            "subscriptions": [(source, "DataRequested", handler)],
            "native": types.SimpleNamespace(InvokeRequired=False),
            "lock": threading.Lock(),
            "cleaned": False,
        }
        with mock.patch.object(ns, "_unsubscribe_windows_event") as unsubscribe:
            ns._cleanup_windows_share(state)
            ns._cleanup_windows_share(state)
        unsubscribe.assert_called_once_with(source, "DataRequested", handler)
        self.assertFalse(Path(path).exists())

    def test_share_temp_unlink_retries_boundedly(self):
        path = Path(self.id()).with_suffix(".png")
        with (
            mock.patch.object(Path, "unlink", side_effect=[PermissionError(), PermissionError(), None]) as unlink,
            mock.patch.object(ns.time, "sleep") as sleep,
        ):
            self.assertTrue(ns._unlink_windows_share_path(path, delays=(0.0, 0.01, 0.02)))
        self.assertEqual(unlink.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_share_cleanup_tracks_persistent_unlink_failure(self):
        path = str(Path(self.id()).with_suffix(".png"))
        state = {
            "path": path,
            "keep": [],
            "subscriptions": [],
            "lock": threading.Lock(),
            "cleaned": False,
        }
        with (
            mock.patch.object(ns, "_unlink_windows_share_path", return_value=False),
            mock.patch.object(ns, "_schedule_windows_share_unlink_retry") as schedule,
        ):
            ns._cleanup_windows_share(state)
        schedule.assert_called_once_with(path)

    def test_share_unlink_retry_registry_forgets_path_only_after_success(self):
        path = str(Path(self.id()).with_suffix(".png"))

        class ImmediateTimer:
            def __init__(self, _delay, callback):
                self.callback = callback
                self.daemon = False

            def start(self):
                self.callback()

            def cancel(self):
                pass

        with (
            mock.patch.object(ns, "_unlink_windows_share_path", side_effect=[False, True]) as unlink,
            mock.patch.object(ns.threading, "Timer", ImmediateTimer),
        ):
            ns._schedule_windows_share_unlink_retry(path, delays=(0.0,))
        self.assertEqual(unlink.call_count, 2)
        self.assertNotIn(path, ns._WINDOWS_SHARE_PENDING_UNLINKS)

    @unittest.skipUnless(sys.platform == "win32", "WinRT metadata reflection is Windows-only")
    def test_winrt_completion_events_belong_to_data_package_not_manager(self):
        import clr
        import webview.platforms.winforms  # loads the WinRT projection assemblies used in production
        clr.AddReference("System.Windows.Forms")
        from System import Threading
        from System.Windows.Forms import Form

        observed = {}

        def inspect():
            form = Form()
            try:
                form.CreateControl()
                manager, factory, release, _show = ns._windows_share_manager(form.Handle.ToInt64())
                try:
                    assembly = manager.GetType().Assembly
                    package_type = assembly.GetType(
                        "Windows.ApplicationModel.DataTransfer.DataPackage", True)
                    observed["manager"] = {str(event.Name) for event in manager.GetType().GetEvents()}
                    observed["package"] = {str(event.Name) for event in package_type.GetEvents()}
                    try:
                        bridge, handler, token = ns._subscribe_windows_event(
                            manager, "DataRequested", lambda *_: None)
                        ns._unsubscribe_windows_event(manager, "DataRequested", token)
                        observed["subscription_round_trip"] = bool(bridge and handler and token)
                    except BaseException as exc:  # aqg: top-level boundary - report STA probe failure
                        observed["subscription_error"] = type(exc).__name__
                finally:
                    release(factory)
            finally:
                form.Dispose()

        thread = Threading.Thread(Threading.ThreadStart(inspect))
        thread.SetApartmentState(Threading.ApartmentState.STA)
        thread.Start()
        thread.Join()
        self.assertNotIn("ShareCompleted", observed["manager"])
        self.assertNotIn("ShareCanceled", observed["manager"])
        self.assertIn("ShareCompleted", observed["package"])
        self.assertIn("ShareCanceled", observed["package"])
        self.assertNotIn("subscription_error", observed)
        self.assertTrue(observed["subscription_round_trip"])

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_process_exit_cleanup_removes_active_share_file(self):
        def shown(_win, state):
            state["ok"] = True
            state["shown"].set()
            return True

        with mock.patch.object(ns, "_start_windows_share_ui", side_effect=shown):
            self.assertTrue(ns._present_windows_share(object(), self._minimal_png(), timeout=0.01))
        path = Path(ns._WINDOWS_SHARE_ACTIVE["path"])
        self.assertTrue(path.exists())
        ns._cleanup_windows_share_at_exit()
        self.assertFalse(path.exists())
        self.assertIsNone(ns._WINDOWS_SHARE_ACTIVE)

    @unittest.skipUnless(sys.platform == "win32", "WinRT event bridge is Windows-only")
    def test_typed_event_bridge_invokes_and_unsubscribes_exact_delegate(self):
        import clr
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import Button

        calls = []
        button = Button()
        bridge, handler = ns._windows_event_handler(button, "Click", lambda *_: calls.append("event"))
        # The generic helper uses the same exact delegate construction as WinRT. A WinForms Click event is
        # raised locally so this test proves the Python callback survives the typed .NET bridge.
        button.Click += handler
        button.PerformClick()
        button.Click -= handler
        button.PerformClick()
        self.assertIsNotNone(bridge)
        self.assertEqual(calls, ["event"])

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_repeated_share_request_is_idempotent_while_session_is_active(self):
        win = types.SimpleNamespace(native=types.SimpleNamespace(Enabled=False))

        def shown(_win, state):
            state["ok"] = True
            state["phase"] = "shown"
            state["shown"].set()
            return True

        with mock.patch.object(ns, "_start_windows_share_ui", side_effect=shown) as start:
            self.assertTrue(ns._present_windows_share(win, self._minimal_png(), timeout=0.01))
            self.assertTrue(ns._present_windows_share(win, self._minimal_png(), timeout=0.01))
        self.assertEqual(start.call_count, 1)
        path = Path(ns._WINDOWS_SHARE_ACTIVE["path"])
        self.assertTrue(path.exists())
        ns._cleanup_windows_share(ns._WINDOWS_SHARE_ACTIVE)
        self.assertFalse(path.exists())

    def test_share_cancel_retires_ui_before_delayed_cleanup(self):
        state = {"share": "active"}
        with (
            mock.patch.object(ns, "_cleanup_windows_share") as cleanup,
            mock.patch.object(ns, "_retire_windows_share", return_value=True) as retire,
            mock.patch.object(ns, "_schedule_windows_share_cleanup") as schedule,
        ):
            ns._finish_windows_share(state, cancelled=True)
        retire.assert_called_once_with(state)
        schedule.assert_called_once_with(state)
        cleanup.assert_not_called()

    def test_share_completion_keeps_file_alive_briefly(self):
        state = {"share": "active"}
        with (
            mock.patch.object(ns, "_cleanup_windows_share") as cleanup,
            mock.patch.object(ns, "_retire_windows_share") as retire,
            mock.patch.object(ns, "_schedule_windows_share_cleanup") as schedule,
        ):
            ns._finish_windows_share(state, cancelled=False)
        retire.assert_called_once_with(state)
        schedule.assert_called_once_with(state)
        cleanup.assert_not_called()

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_share_completion_releases_active_slot_before_file_cleanup(self):
        win = types.SimpleNamespace(native=types.SimpleNamespace(Enabled=False))
        states = []

        def shown(_win, state):
            states.append(state)
            state["ok"] = True
            state["phase"] = "shown"
            state["shown"].set()
            return True

        with (
            mock.patch.object(ns, "_start_windows_share_ui", side_effect=shown) as start,
            mock.patch.object(ns, "_schedule_windows_share_cleanup"),
        ):
            self.assertTrue(ns._present_windows_share(win, self._minimal_png(), timeout=0.01))
            first = ns._WINDOWS_SHARE_ACTIVE
            ns._finish_windows_share(first, cancelled=False)
            self.assertIsNone(ns._WINDOWS_SHARE_ACTIVE)
            self.assertTrue(any(state is first for state in ns._WINDOWS_SHARE_RETAINED))
            self.assertTrue(Path(first["path"]).exists())
            self.assertTrue(ns._present_windows_share(win, self._minimal_png(), timeout=0.01))
        self.assertEqual(start.call_count, 2)
        self.assertEqual(len(states), 2)

    @mock.patch.object(ns, "_IS_WINDOWS", True)
    def test_closed_modal_without_event_is_retired_before_new_share(self):
        win = types.SimpleNamespace(native=types.SimpleNamespace(Enabled=False))

        def shown(_win, state):
            state["ok"] = True
            state["phase"] = "shown"
            state["shown"].set()
            return True

        with (
            mock.patch.object(ns, "_start_windows_share_ui", side_effect=shown) as start,
            mock.patch.object(ns, "_schedule_windows_share_cleanup"),
        ):
            self.assertTrue(ns._present_windows_share(win, self._minimal_png(), timeout=0.01))
            first = ns._WINDOWS_SHARE_ACTIVE
            win.native.Enabled = True
            self.assertTrue(ns._present_windows_share(win, self._minimal_png(), timeout=0.01))
        self.assertEqual(start.call_count, 2)
        self.assertTrue(any(state is first for state in ns._WINDOWS_SHARE_RETAINED))


class VisualCaptureCapabilityTests(unittest.TestCase):
    @staticmethod
    def _png_bytes(width=12, height=34):
        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        rows = b"".join(b"\x00" + (b"\x00\x00\x00" * width) for _ in range(height))
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

    @classmethod
    def _png_data_url(cls):
        png = cls._png_bytes()
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    def test_reports_unsupported_without_attempting_a_capture(self):
        api = ns.PopupApi("/tmp/de-popup-test-result.json")
        with mock.patch.object(ns, "_visual_capture_supported", return_value=False):
            self.assertEqual(
                api.visual_capture_capabilities(),
                {"ok": True, "supported": False, "reason": "unsupported"},
            )

    def test_reports_supported_when_native_backend_exists(self):
        api = ns.PopupApi("/tmp/de-popup-test-result.json")
        with mock.patch.object(ns, "_visual_capture_supported", return_value=True):
            self.assertEqual(api.visual_capture_capabilities(), {"ok": True, "supported": True})

    @mock.patch.object(ns, "_IS_MAC", False)
    @mock.patch.object(ns, "_IS_WINDOWS", False)
    @mock.patch.object(ns, "_IS_LINUX", True)
    def test_linux_copy_visual_image_data_url_dispatches_to_clipboard_helper(self):
        api = ns.PopupApi("/tmp/de-popup-test-result.json")
        with mock.patch.object(ns, "_copy_linux_png_to_clipboard", return_value=True) as copy:
            self.assertEqual(api.copy_visual_image_data_url(self._png_data_url()), {"ok": True})
        self.assertTrue(copy.call_args.args[0].startswith(b"\x89PNG\r\n\x1a\n"))

    @mock.patch.object(ns, "_IS_MAC", False)
    @mock.patch.object(ns, "_IS_WINDOWS", False)
    @mock.patch.object(ns, "_IS_LINUX", True)
    def test_linux_share_visual_image_data_url_dispatches_to_share_helper(self):
        api = ns.PopupApi("/tmp/de-popup-test-result.json")
        with mock.patch.object(ns, "_present_linux_share", return_value=True) as share:
            self.assertEqual(api.share_visual_image_data_url(self._png_data_url()), {"ok": True})
        self.assertTrue(share.call_args.args[0].startswith(b"\x89PNG\r\n\x1a\n"))

    def test_visual_image_data_url_rejects_non_png_payloads(self):
        api = ns.PopupApi("/tmp/de-popup-test-result.json")
        bad = "data:image/jpeg;base64," + base64.b64encode(b"not-png").decode("ascii")
        self.assertEqual(api.copy_visual_image_data_url(bad), {"ok": False})
        self.assertEqual(api.share_visual_image_data_url("data:image/png;base64,%%%%"), {"ok": False})

    @mock.patch.object(ns, "_IS_MAC", False)
    @mock.patch.object(ns, "_IS_WINDOWS", False)
    @mock.patch.object(ns, "_IS_LINUX", True)
    def test_visual_image_data_url_rejects_truncated_png_before_helper(self):
        truncated = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (12).to_bytes(4, "big") + (34).to_bytes(4, "big")
        data_url = "data:image/png;base64," + base64.b64encode(truncated).decode("ascii")
        api = ns.PopupApi("/tmp/de-popup-test-result.json")
        with mock.patch.object(ns, "_copy_linux_png_to_clipboard", return_value=True) as copy:
            self.assertEqual(api.copy_visual_image_data_url(data_url), {"ok": False})
        copy.assert_not_called()

    def test_visual_image_data_url_rejects_png_dimensions_over_page_capture_budget(self):
        api = ns.PopupApi("/tmp/de-popup-test-result.json")
        too_wide = "data:image/png;base64," + base64.b64encode(self._png_bytes(width=8193, height=1)).decode("ascii")
        self.assertEqual(api.copy_visual_image_data_url(too_wide), {"ok": False})


class SubviewFrameTests(unittest.TestCase):
    """`_subview_frame_in_content`: map a CSS (top-left) rect to an NSView frame ((x,y),(w,h))."""

    def test_flipped_view_passes_y_through(self):
        # a flipped NSView already has a top-left origin — no y flip
        self.assertEqual(ns._subview_frame_in_content(5, 30, 100, 40, 500, True), ((5.0, 30.0), (100.0, 40.0)))

    def test_nonflipped_view_flips_y(self):
        # bottom-left origin: y = content_h - css_y - h = 500 - 30 - 40
        self.assertEqual(ns._subview_frame_in_content(5, 30, 100, 40, 500, False), ((5.0, 430.0), (100.0, 40.0)))

    def test_size_clamps_to_at_least_one(self):
        # a zero/negative measured rect must never yield a <1 (or negative) frame size
        (x, y), (w, h) = ns._subview_frame_in_content(0, 0, 0, -3, 100, True)
        self.assertEqual((w, h), (1.0, 1.0))

    def test_clamped_height_feeds_the_nonflipped_y(self):
        # h clamps to 1 BEFORE the y flip, so y uses the clamped h: 100 - 10 - 1
        (x, y), (w, h) = ns._subview_frame_in_content(0, 10, 20, 0, 100, False)
        self.assertEqual((y, h), (89.0, 1.0))


class SettleObjcResultTests(unittest.TestCase):
    """`_settle_objc_result`: the WKWebView completion-handler body. Invariants: `store` runs ONLY when
    `error` is None; the `done` Event is ALWAYS set; NOTHING escapes (a raise inside an ObjC callback
    SIGABRTs the whole popup)."""

    def test_store_runs_and_done_set_on_success(self):
        done = threading.Event()
        ran = []
        ok = ns._settle_objc_result(done, lambda: ran.append(True), None)
        self.assertTrue(ok)
        self.assertEqual(ran, [True])
        self.assertTrue(done.is_set())

    def test_store_skipped_when_error_present(self):
        done = threading.Event()
        ran = []
        ok = ns._settle_objc_result(done, lambda: ran.append(True), object())  # truthy error
        self.assertTrue(ok)            # skipping store for an error is a clean (not failed) settle
        self.assertEqual(ran, [])      # store must NOT run when the snapshot errored
        self.assertTrue(done.is_set())

    def test_raising_store_is_swallowed_and_done_still_set(self):
        done = threading.Event()

        def boom():
            raise RuntimeError("callback blew up")

        ok = ns._settle_objc_result(done, boom, None)
        self.assertFalse(ok)           # a raised store → False (swallowed + logged), never propagated
        self.assertTrue(done.is_set())  # the caller's bounded wait must still be released

    def test_base_exception_in_store_does_not_escape(self):
        done = threading.Event()

        def boom():
            raise KeyboardInterrupt()  # a BaseException — must also be caught at the ObjC boundary

        ok = ns._settle_objc_result(done, boom, None)   # must not raise out of here
        self.assertFalse(ok)
        self.assertTrue(done.is_set())


class SnapshotRegionFailClosedTests(unittest.TestCase):
    """`PopupApi.snapshot_region` MUST fail closed — a missing/malformed rect returns {ok:False} with NO
    image key, never a full-window shot (which would leak the chat column / chrome the user never framed).
    This branch is reachable without pyobjc (it returns before any Cocoa call), so it is a durable test."""

    def setUp(self):
        self.api = ns.PopupApi("/tmp/de-popup-test-result.json")   # no window is created by __init__
        self._orig_supported = ns._visual_capture_supported

    def tearDown(self):
        ns._visual_capture_supported = self._orig_supported

    def test_none_rect_is_fail_closed_no_image(self):
        # force the platform gate OPEN so we exercise the rect-validation branch, not the off-mac short-circuit
        ns._visual_capture_supported = lambda: True
        r = self.api.snapshot_region(None)
        self.assertEqual(r.get("ok"), False)
        self.assertNotIn("image", r)

    def test_malformed_or_zero_rect_is_fail_closed_no_image(self):
        ns._visual_capture_supported = lambda: True
        for bad in ([1, 2, 3], [0, 0, 0, 0], [0, 0, -1, 10], ["x", 1, 2, 3], "nope", 42):
            r = self.api.snapshot_region(bad)
            self.assertEqual(r.get("ok"), False, bad)
            self.assertNotIn("image", r, bad)

    def test_unsupported_platform_reports_reason_no_image(self):
        # off-macOS (or no WebKit): a valid rect still short-circuits to an honest 'unsupported' — never a
        # bare {ok:False} that the page would render as "try again" (advice that can never succeed).
        ns._visual_capture_supported = lambda: False
        r = self.api.snapshot_region([10, 10, 40, 40])
        self.assertEqual(r.get("ok"), False)
        self.assertEqual(r.get("reason"), "unsupported")
        self.assertNotIn("image", r)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
