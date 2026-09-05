"""Native Cursor Windows evidence stays bounded, injectable, and redaction-safe."""

from __future__ import annotations

import ast
import ctypes
import os
import threading
import time
import pathlib
import unittest
import uuid
from dataclasses import replace
from datetime import date
from unittest import mock

from installer import client_host_runtime, cursor_windows_native, shim
from installer.client_host_runtime import (
    CursorProcessEvidence,
    VolatileResult,
)
from installer.cursor_windows_native import (
    CursorTerminalLease,
    CursorWindowsNativeProbes,
    WindowsNativeApi,
    redacted_capture_projection,
)


class _Handle:
    def __init__(self, pid):
        self.pid = pid
        self.closed = False

    def close(self):
        self.closed = True


class _FakeApi:
    def __init__(self):
        self.handles = {}
        self.volatile = VolatileResult.available()
        self.remote = False
        self.platform_name = "win32"
        self.session_id = 7
        self.active_session_id = 7

    def platform(self):
        return self.platform_name

    def remote_environment(self):
        return self.remote

    def current_pid(self):
        return 300

    def process_parents(self, _deadline):
        return {300: 200, 200: 100}

    def process_session_id(self, _pid):
        return self.session_id

    def active_console_session_id(self):
        return self.active_session_id

    def current_process_creation_time(self, _deadline):
        return 3000

    def runtime_architecture(self):
        return "x64"

    def known_roots(self, _deadline):
        return {"LocalAppData": r"C:\Users\Owner\AppData\Local"}

    def open_process(self, pid):
        handle = _Handle(pid)
        self.handles[pid] = handle
        return handle

    def inspect_process(self, handle, _deadline):
        if handle.pid == 200:
            return CursorProcessEvidence(
                pid=200,
                image_basename="node.exe",
                argv=(
                    "node.exe",
                    "--type=extensionHost",
                    "--auth-token=abc123",
                ),
                session_id=7,
                creation_time=2000,
            )
        return CursorProcessEvidence(
            pid=100,
            image_basename="Cursor.exe",
            argv=("Cursor.exe",),
            session_id=7,
            creation_time=1000,
            final_path=r"C:\Users\Owner\AppData\Local\Programs\Cursor\Cursor.exe",
            fixed_volume=True,
            product="Cursor",
            company="Anysphere, Inc.",
            file_version="3.3.30",
        )

    def volatile_check(self, handle, fixture, expected_session_id, deadline):
        self.last_volatile = (
            handle.pid,
            fixture.fixture_id,
            expected_session_id,
            deadline,
        )
        return self.volatile


def _fixture():
    fixture = client_host_runtime.PRODUCTION_CURSOR_WINDOWS_FIXTURE
    return replace(
        fixture,
        fixture_id="synthetic-native",
        enabled=True,
        expires_on=date.max,
        parent_image_basename="node.exe",
        required_argv_rules=(
            client_host_runtime.CursorArgvRule(
                "--type=extensionHost", "exact", "direct-parent"
            ),
        ),
        max_ancestor_hops=2,
        terminal_hop=2,
        window_class_names=("Chrome_WidgetWin_1",),
    )


class CursorWindowsNativeProbeTests(unittest.TestCase):
    def test_collect_retains_only_terminal_and_lease_drives_volatile_check(self):
        api = _FakeApi()
        probes = CursorWindowsNativeProbes(api=api)

        evidence = probes.collect(_fixture(), time.monotonic() + 1)

        self.assertEqual([item.pid for item in evidence.ancestors], [200, 100])
        self.assertTrue(api.handles[200].closed)
        self.assertFalse(api.handles[100].closed)
        self.assertIs(evidence.terminal_lease.handle, api.handles[100])
        self.assertEqual(evidence.terminal_lease.check_display().reason, "granted")
        evidence.terminal_lease.close()
        self.assertTrue(api.handles[100].closed)
        self.assertEqual(api.last_volatile[2], 7)

    def test_collect_walks_exactly_to_terminal_hop(self):
        api = _FakeApi()
        api.process_parents = mock.Mock(
            return_value={300: 200, 200: 100, 100: 50}
        )
        fixture = replace(
            _fixture(),
            max_ancestor_hops=3,
            terminal_hop=2,
        )

        evidence = CursorWindowsNativeProbes(api=api).collect(
            fixture, time.monotonic() + 1
        )
        try:
            self.assertEqual([item.pid for item in evidence.ancestors], [200, 100])
            self.assertNotIn(50, api.handles)
        finally:
            evidence.terminal_lease.close()

    def test_lease_revokes_expired_fixture_without_native_calls(self):
        handle = _Handle(100)
        api = mock.Mock()
        fixture = replace(_fixture(), expires_on=date(2000, 1, 1))
        lease = CursorTerminalLease(handle, api, fixture, session_id=7)

        result = lease.check_display()

        self.assertEqual(result, VolatileResult.revoked("fixture-expired"))
        api.volatile_check.assert_not_called()
        lease.close()

    def test_lease_serializes_close_with_inflight_native_check(self):
        api = _FakeApi()
        started = threading.Event()
        release = threading.Event()

        def blocked_check(*_args):
            started.set()
            release.wait(1)
            return VolatileResult.available()

        api.volatile_check = blocked_check
        handle = _Handle(100)
        lease = CursorTerminalLease(handle, api, _fixture(), session_id=7)
        observed = []
        worker = threading.Thread(
            target=lambda: observed.append(lease.check_display())
        )
        worker.start()
        self.assertTrue(started.wait(1))

        lease.close()
        self.assertFalse(handle.closed)
        self.assertEqual(
            lease.check_display(), VolatileResult.revoked("terminal-exit")
        )
        release.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(handle.closed)
        self.assertEqual(observed, [VolatileResult.revoked("terminal-exit")])

    def test_stalled_volatile_check_returns_bounded_and_does_not_duplicate(self):
        api = _FakeApi()
        started = threading.Event()
        release = threading.Event()
        call_count = 0

        def blocked_check(*_args):
            nonlocal call_count
            call_count += 1
            started.set()
            release.wait(1)
            return VolatileResult.available()

        api.volatile_check = blocked_check
        handle = _Handle(100)
        lease = CursorTerminalLease(handle, api, _fixture(), session_id=7)

        started_at = time.monotonic()
        result = lease.check_display()
        elapsed = time.monotonic() - started_at
        second = lease.check_display()

        self.assertTrue(started.is_set())
        self.assertEqual(result, VolatileResult.unavailable())
        self.assertEqual(second, VolatileResult.unavailable())
        self.assertLess(elapsed, 0.35)
        self.assertEqual(call_count, 1)
        lease.close()
        self.assertFalse(handle.closed)
        release.set()
        deadline = time.monotonic() + 1
        while not handle.closed and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(handle.closed)

    def test_volatile_window_matrix_requires_exact_visible_local_window(self):
        class Kernel32:
            @staticmethod
            def WaitForSingleObject(_handle, _timeout):
                return 0x102

        class User32:
            def __init__(self, state):
                self.state = state
                self.open_access = None
                self.closed = False

            def OpenInputDesktop(self, _flags, _inherit, access):
                self.open_access = access
                return 9

            def CloseDesktop(self, _desktop):
                self.closed = True
                return True

            @staticmethod
            def GetWindowThreadProcessId(_hwnd, pid_pointer):
                pid_pointer._obj.value = 100
                return 1

            def GetClassNameW(self, _hwnd, buffer, _length):
                buffer.value = self.state["class_name"]
                return len(buffer.value)

            def IsWindowVisible(self, _hwnd):
                return self.state["visible"]

            def IsIconic(self, _hwnd):
                return self.state["iconic"]

            def GetWindow(self, _hwnd, _relation):
                return self.state["owner"]

            @staticmethod
            def EnumDesktopWindows(_desktop, callback, _lparam):
                callback(10, 0)
                return True

        class DwmApi:
            def __init__(self, state):
                self.state = state

            def DwmGetWindowAttribute(
                self, _hwnd, _attribute, output, _size
            ):
                if self.state["dwm_exception"]:
                    raise OSError("synthetic DWM failure")
                output._obj.value = self.state["cloaked"]
                return self.state["dwm_status"]

        base = {
            "class_name": "Chrome_WidgetWin_1",
            "visible": True,
            "iconic": False,
            "owner": 0,
            "cloaked": 0,
            "dwm_status": 0,
            "dwm_exception": False,
        }
        denied_mutations = (
            {"class_name": "Other"},
            {"visible": False},
            {"iconic": True},
            {"owner": 1},
            {"cloaked": 1},
            {"dwm_status": 1},
            {"dwm_exception": True},
        )

        for mutation in ({}, *denied_mutations):
            state = {**base, **mutation}
            api = WindowsNativeApi.__new__(WindowsNativeApi)
            api.kernel32 = Kernel32()
            api.user32 = User32(state)
            api.dwmapi = DwmApi(state)
            api.process_session_id = lambda _pid: 7
            api.active_console_session_id = lambda: 7
            handle = _Handle(100)
            handle.value = 55

            # `volatile_check` builds its EnumWindows callback with `ctypes.WINFUNCTYPE`,
            # which exists only on Windows — the one non-portable symbol on a path whose
            # kernel32/user32/dwmapi are already faked here. The product looks it up on the
            # ctypes module at call time, so a patch restores this decision matrix off
            # Windows instead of skipping it (the module could not even import before, so
            # this coverage was running nowhere).
            with mock.patch.object(
                ctypes, "WINFUNCTYPE", lambda *a, **k: (lambda fn: fn), create=True
            ):
                result = api.volatile_check(
                    handle,
                    _fixture(),
                    expected_session_id=7,
                    deadline=time.monotonic() + 1,
                )

            with self.subTest(mutation=mutation):
                self.assertEqual(result.allowed, not mutation)
                self.assertEqual(
                    result.reason,
                    "granted"
                    if not mutation
                    else "display-context-unavailable",
                )
                self.assertEqual(api.user32.open_access, 0x0001)
                self.assertTrue(api.user32.closed)

    def test_path_properties_rejects_unc_and_detects_reparse_component(self):
        api = WindowsNativeApi.__new__(WindowsNativeApi)
        api.kernel32 = mock.Mock()
        api.kernel32.GetDriveTypeW.return_value = 3
        api.kernel32.GetFileAttributesW.side_effect = (
            lambda path: 0x400 if path.endswith("Programs") else 0
        )

        self.assertEqual(
            api._path_properties(
                r"\\server\share\Cursor.exe",
                time.monotonic() + 1,
            ),
            (False, True),
        )
        self.assertEqual(
            api._path_properties(
                r"C:\Programs\Cursor\Cursor.exe",
                time.monotonic() + 1,
            ),
            (True, True),
        )

    @unittest.skipUnless(os.name == "nt", "real Win32 bindings require Windows")
    def test_real_windows_bindings_inspect_current_process(self):
        api = WindowsNativeApi()
        deadline = time.monotonic() + 0.75
        current_pid = api.current_pid()
        parents = api.process_parents(deadline)
        handle = api.open_process(current_pid)
        try:
            evidence = api.inspect_process(handle, deadline)
            roots = api.known_roots(deadline)
        finally:
            handle.close()

        self.assertIn(current_pid, parents)
        self.assertTrue(evidence.image_basename)
        self.assertGreater(len(evidence.argv), 0)
        self.assertTrue(evidence.alive)
        self.assertTrue(evidence.fixed_volume)
        self.assertFalse(evidence.reparse_target)
        self.assertEqual(
            evidence.session_id,
            api.process_session_id(current_pid),
        )
        self.assertIn("LocalAppData", roots)

    def test_collection_failure_closes_every_opened_handle(self):
        api = _FakeApi()
        api.inspect_process = mock.Mock(
            side_effect=[
                CursorProcessEvidence(
                    pid=200,
                    image_basename="node.exe",
                    argv=("node.exe",),
                    session_id=7,
                    creation_time=2000,
                ),
                OSError("synthetic"),
            ]
        )

        with self.assertRaises(OSError):
            CursorWindowsNativeProbes(api=api).collect(
                _fixture(), time.monotonic() + 1
            )

        self.assertTrue(api.handles[200].closed)
        self.assertTrue(api.handles[100].closed)

    def test_remote_environment_short_circuits_before_process_handles(self):
        api = _FakeApi()
        api.remote = True
        api.process_parents = mock.Mock(side_effect=AssertionError)
        api.open_process = mock.Mock(side_effect=AssertionError)

        evidence = CursorWindowsNativeProbes(api=api).collect(
            _fixture(), time.monotonic() + 1
        )

        self.assertTrue(evidence.remote_environment)
        self.assertEqual(evidence.ancestors, ())
        self.assertIsNone(evidence.terminal_lease)
        api.process_parents.assert_not_called()
        api.open_process.assert_not_called()

    def test_unsupported_platform_short_circuits_before_windows_calls(self):
        api = _FakeApi()
        api.platform_name = "linux"
        api.current_pid = mock.Mock(side_effect=AssertionError)

        evidence = CursorWindowsNativeProbes(api=api).collect(
            _fixture(), time.monotonic() + 1
        )

        self.assertEqual(evidence.platform, "linux")
        self.assertEqual(evidence.ancestors, ())
        api.current_pid.assert_not_called()

    def test_session_mismatch_and_invalid_session_open_no_handles(self):
        for session_id, active_session_id in (
            (7, 8),
            (0xFFFFFFFF, 7),
        ):
            api = _FakeApi()
            api.session_id = session_id
            api.active_session_id = active_session_id
            api.process_parents = mock.Mock(side_effect=AssertionError)
            api.open_process = mock.Mock(side_effect=AssertionError)

            evidence = CursorWindowsNativeProbes(api=api).collect(
                _fixture(), time.monotonic() + 1
            )

            with self.subTest(
                session_id=session_id,
                active_session_id=active_session_id,
            ):
                self.assertEqual(evidence.ancestors, ())
                api.process_parents.assert_not_called()
                api.open_process.assert_not_called()

    def test_invalid_hop_bound_fails_before_native_calls(self):
        api = _FakeApi()
        api.current_pid = mock.Mock(side_effect=AssertionError)
        fixture = replace(
            _fixture(),
            max_ancestor_hops=9,
            terminal_hop=9,
        )

        with self.assertRaises(ValueError):
            CursorWindowsNativeProbes(api=api).collect(
                fixture, time.monotonic() + 1
            )

        api.current_pid.assert_not_called()

    def test_mid_collection_timeout_closes_acquired_handle(self):
        api = _FakeApi()

        def timeout(_handle, _deadline):
            raise TimeoutError("synthetic deadline")

        api.inspect_process = timeout

        with self.assertRaises(TimeoutError):
            CursorWindowsNativeProbes(api=api).collect(
                _fixture(), time.monotonic() + 1
            )

        self.assertTrue(api.handles[200].closed)

    def test_capture_projection_contains_no_raw_process_or_private_values(self):
        api = _FakeApi()
        evidence = CursorWindowsNativeProbes(api=api).collect(
            _fixture(), time.monotonic() + 1
        )
        try:
            projection = redacted_capture_projection(_fixture(), evidence)
            rendered = repr(projection)
        finally:
            evidence.terminal_lease.close()

        for forbidden in (
            "200",
            "100",
            r"C:\Users",
            "Owner",
            "node.exe', '--type",
            "abc123",
            "auth-token",
            "extensionHost",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(projection["ancestor_count"], 2)
        self.assertEqual(
            projection["ancestors"][0]["required_rule_matches"],
            1,
        )
        self.assertEqual(
            projection["ancestors"][0]["forbidden_rule_matches"],
            0,
        )

    def test_evaluator_denial_closes_collected_terminal_handle(self):
        api = _FakeApi()
        fixture = _fixture()
        evidence = CursorWindowsNativeProbes(api=api).collect(
            fixture, time.monotonic() + 1
        )

        result = client_host_runtime.evaluate_cursor_windows_preflight(
            replace(fixture, parent_image_basename="wrong.exe"),
            evidence,
        )

        self.assertEqual(result.reason, "process-role-mismatch")
        self.assertTrue(api.handles[100].closed)

    def test_abandoned_native_result_closes_terminal_handle(self):
        api = _FakeApi()
        native = CursorWindowsNativeProbes(api=api)
        collected = threading.Event()
        release = threading.Event()

        class DelayedProbe:
            def collect(self, fixture, deadline):
                evidence = native.collect(fixture, deadline)
                collected.set()
                release.wait(1)
                return evidence

        result = client_host_runtime.run_cursor_windows_preflight(
            fixture=_fixture(),
            probes=DelayedProbe(),
            platform="win32",
            timeout_s=0.03,
        )
        self.assertTrue(collected.is_set())
        self.assertEqual(result.reason, "preflight-timeout")
        release.set()
        deadline = time.monotonic() + 1
        while (
            not api.handles[100].closed
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        self.assertTrue(api.handles[100].closed)

    def test_shim_shared_cursor_gate_ignores_disabled_windows_fixture(self):
        fixture = replace(
            client_host_runtime.PRODUCTION_CURSOR_WINDOWS_FIXTURE,
            enabled=False,
        )
        probes = mock.Mock()
        with (
            mock.patch.object(
                shim, "PRODUCTION_CURSOR_WINDOWS_FIXTURE", fixture
            ),
            mock.patch.object(
                cursor_windows_native,
                "default_cursor_windows_probes",
                return_value=probes,
            ) as factory,
            mock.patch.object(
                shim,
                "run_cursor_windows_preflight",
                return_value=client_host_runtime.PreflightResult.denied(
                    "fixture-disabled"
                ),
            ) as preflight,
        ):
            gate = shim._capability_gate_for_host("cursor")

        factory.assert_not_called()
        preflight.assert_not_called()
        self.assertEqual(gate.reason, "static")
        self.assertTrue(gate.display_enabled)

    def test_shim_shared_cursor_gate_does_not_inject_windows_probe(self):
        fixture = _fixture()
        probes = mock.Mock()
        with (
            mock.patch.object(
                shim, "PRODUCTION_CURSOR_WINDOWS_FIXTURE", fixture
            ),
            mock.patch.object(
                cursor_windows_native,
                "default_cursor_windows_probes",
                return_value=probes,
            ) as factory,
            mock.patch.object(
                shim,
                "run_cursor_windows_preflight",
                return_value=client_host_runtime.PreflightResult.denied(
                    "os-probe-failed"
                ),
            ) as preflight,
        ):
            gate = shim._capability_gate_for_host("cursor")

        factory.assert_not_called()
        preflight.assert_not_called()
        self.assertEqual(gate.reason, "static")
        self.assertTrue(gate.followup_enabled)
        self.assertTrue(gate.stopper_enabled)

    def test_shim_shared_cursor_gate_is_not_affected_by_windows_probe_failure(self):
        fixture = _fixture()
        with (
            mock.patch.object(
                shim, "PRODUCTION_CURSOR_WINDOWS_FIXTURE", fixture
            ),
            mock.patch.object(
                cursor_windows_native,
                "default_cursor_windows_probes",
                side_effect=OSError("synthetic DLL failure"),
            ),
        ):
            gate = shim._capability_gate_for_host("cursor")

        self.assertEqual(gate.reason, "static")
        self.assertTrue(gate.display_enabled)

    def test_conditional_gate_uses_retained_native_lease_by_default(self):
        lease = mock.Mock()
        lease.check_display.return_value = VolatileResult.available()
        gate = client_host_runtime.TransportCapabilityGate.conditional(
            followup=False, stopper=False
        )
        gate.complete_preflight(
            client_host_runtime.PreflightResult.ready(lease)
        )
        gate.finalize_initialize(matches=True)

        result = gate.check_display(None)

        self.assertTrue(result.allowed)
        lease.check_display.assert_called_once_with()


class PortableStructLayoutTests(unittest.TestCase):
    """This module is imported by ``installer.shim`` on EVERY platform and it parses GUIDs at
    import time, so any struct that import touches must carry the Win32 layout everywhere —
    not only on Windows. It did not: ``wintypes.DWORD`` aliases ``c_ulong``, 8 bytes on
    64-bit macOS/Linux, so ``_GUID`` measured 24 bytes, ``from_buffer_copy(uuid.bytes_le)``
    raised ``ValueError: Buffer size too small (16 instead of at least 24 bytes)``, and the
    MCP server could not start on macOS from v0.2.29 on (these tests could not even load)."""

    def test_guid_has_the_win32_layout_on_this_platform(self):
        self.assertEqual(ctypes.sizeof(cursor_windows_native._GUID), 16)

    def test_guid_is_declared_with_fixed_width_types_not_wintypes(self):
        # The size checks above are satisfied on WINDOWS even by the buggy declaration
        # (there wintypes.DWORD is 4 bytes), so they cannot catch a revert on the ABI
        # platform. Asserting the declared types does, on every OS.
        self.assertEqual(
            [field_type for _, field_type in cursor_windows_native._GUID._fields_],
            [ctypes.c_uint32, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_ubyte * 8],
        )

    def test_guid_field_widths_do_not_depend_on_the_platform(self):
        widths = {
            name: ctypes.sizeof(field_type)
            for name, field_type in cursor_windows_native._GUID._fields_
        }
        self.assertEqual(widths, {"Data1": 4, "Data2": 2, "Data3": 2, "Data4": 8})

    def test_guid_parse_round_trips_a_known_folder_id(self):
        value = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"
        parsed = cursor_windows_native._GUID.parse(value)
        self.assertEqual(bytes(memoryview(parsed).cast("B")), uuid.UUID(value).bytes_le)

    def test_no_module_scope_struct_uses_platform_variable_field_types(self):
        """The invariant this whole fix rests on, enforced mechanically rather than by comment.

        A struct that is only DECLARED off Windows is harmless; one that is INSTANTIATED at
        import must be portable, because `installer.shim`'s importers run everywhere. A code
        comment is what failed last time — the module docstring claimed the imports were lazy
        while three GUIDs were being parsed at module scope.
        """
        source = pathlib.Path(cursor_windows_native.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        struct_names = {
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(base, ast.Attribute) and base.attr == "Structure"
                for base in node.bases
            )
        }
        used_at_import = set()
        for node in tree.body:  # module scope only — nested defs/classes are not executed here
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and inner.id in struct_names:
                    used_at_import.add(inner.id)

        self.assertIn("_GUID", used_at_import, "the known-folder GUIDs are parsed at import")
        fixed_width = {
            ctypes.c_int8, ctypes.c_uint8, ctypes.c_int16, ctypes.c_uint16,
            ctypes.c_int32, ctypes.c_uint32, ctypes.c_int64, ctypes.c_uint64,
            ctypes.c_char,
        }
        for name in sorted(used_at_import):
            struct = getattr(cursor_windows_native, name)
            for field_name, field_type in struct._fields_:
                # A simple ctypes type exposes `_type_` as a format CHARACTER; an array
                # exposes it as the element CLASS. Only the latter needs unwrapping.
                element = getattr(field_type, "_type_", None)
                candidate = element if isinstance(element, type) else field_type
                with self.subTest(struct=name, field=field_name):
                    self.assertIn(
                        candidate,
                        fixed_width,
                        "%s.%s is instantiated at import, so its type must be fixed-width "
                        "(wintypes aliases change size off Windows)" % (name, field_name),
                    )

    def test_known_folder_guids_are_parsed_at_import_on_this_platform(self):
        self.assertEqual(
            sorted(cursor_windows_native._KNOWN_FOLDER_GUIDS),
            ["LocalAppData", "ProgramFiles", "ProgramFilesX64"],
        )
        for guid in cursor_windows_native._KNOWN_FOLDER_GUIDS.values():
            self.assertIsInstance(guid, cursor_windows_native._GUID)


if __name__ == "__main__":
    unittest.main()
