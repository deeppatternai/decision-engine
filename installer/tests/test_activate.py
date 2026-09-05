"""Behavior tests for the device activation client (installer.activate).

Exercises the real HTTP round-trip against a tiny in-process mock of the
server's ``POST /v1/devices/activate`` endpoint (a real socket, real urllib),
plus config resolution / persistence and the fail-closed guards. No real
network, no server package import — the shell stays self-contained.
"""

from __future__ import annotations

import json
import io
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from client import windows_security
from client.runner import CLIENT_VERSION
from installer import activate, config


class _MockActivateHandler(BaseHTTPRequestHandler):
    # Set per-test on the server instance: (status, body_dict). A 3xx status with
    # a ``redirect_to`` attribute emits a Location header instead of a JSON body.
    def do_POST(self):  # noqa: N802 (stdlib naming)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        self.server.last_request = json.loads(raw)  # type: ignore[attr-defined]
        self.server.last_path = self.path  # type: ignore[attr-defined]
        status, body = self.server.response  # type: ignore[attr-defined]
        redirect_to = getattr(self.server, "redirect_to", None)
        if redirect_to:
            self.send_response(status)
            self.send_header("Location", redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 (stdlib naming)
        # Where a FOLLOWED redirect would land (urllib rewrites a 301/302/303 POST into a
        # body-less GET). Nothing should ever get here — the redirect tests assert exactly
        # that by checking last_path stayed None — so this exists to make a regression
        # visible: if the guard ever fails open, the target records the hit.
        self.server.last_path = self.path  # type: ignore[attr-defined]
        _status, body = self.server.response  # type: ignore[attr-defined]
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):  # silence
        pass


class _MockServer:
    def __init__(self, status, body, redirect_to=None):
        self.httpd = HTTPServer(("127.0.0.1", 0), _MockActivateHandler)
        self.httpd.response = (status, body)  # type: ignore[attr-defined]
        self.httpd.redirect_to = redirect_to  # type: ignore[attr-defined]
        self.httpd.last_request = None  # type: ignore[attr-defined]
        self.httpd.last_path = None  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class ActivateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.json"
        self._saved = os.environ.get("DE_CONFIG_PATH")
        os.environ["DE_CONFIG_PATH"] = str(self.cfg_path)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("DE_CONFIG_PATH", None)
        else:
            os.environ["DE_CONFIG_PATH"] = self._saved
        self.tmp.cleanup()

    def _write_cfg(self, **overrides):
        cfg = {
            "server_endpoint": "",  # set per test (localhost mock)
            "api_key": "act_secret_xyz",
            "device_id": "",
            "access_token": "",
            "device_name": "test-dev",
            "device_fingerprint": "fp_fixed",
            "client_version": "9.9.9",   # a sentinel distinct from CLIENT_VERSION → proves the config value is IGNORED
        }
        cfg.update(overrides)
        config.atomic_write_json(self.cfg_path, cfg)

    @unittest.skipUnless(os.name == "nt", "Windows DACL behavior")
    @unittest.skipUnless(shutil.which("icacls"), "icacls is required for ACL integration")
    def test_atomic_writer_hardens_windows_config_acl_before_writing(self):
        granted = subprocess.run(
            ["icacls", str(self.cfg_path.parent), "/grant", "*S-1-1-0:(OI)(CI)RX"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(granted.returncode, 0)
        config.atomic_write_json(self.cfg_path, {"access_token": "test-token"})
        windows_security.validate_private_data_acl(self.cfg_path.parent)
        windows_security.validate_private_data_acl(self.cfg_path)

    @unittest.skipUnless(os.name == "nt", "Windows DACL behavior")
    @unittest.skipUnless(shutil.which("icacls"), "icacls is required for ACL integration")
    def test_existing_windows_config_acl_is_repaired_without_rewriting(self):
        self.cfg_path.write_text('{"access_token":"keep-exactly"}', encoding="utf-8")
        before = self.cfg_path.read_bytes()
        granted = subprocess.run(
            ["icacls", str(self.cfg_path), "/grant", "*S-1-1-0:(R)"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(granted.returncode, 0)

        self.assertTrue(config.harden_existing_windows_config(self.cfg_path))

        self.assertEqual(self.cfg_path.read_bytes(), before)
        windows_security.validate_private_data_acl(self.cfg_path.parent)
        windows_security.validate_private_data_acl(self.cfg_path)

    def test_atomic_writer_refuses_a_preexisting_random_temp_path(self):
        token = "a" * 16
        collision = self.cfg_path.with_name(
            ".%s.%d.%s.tmp" % (self.cfg_path.name, os.getpid(), token)
        )
        collision.write_text("do-not-overwrite", encoding="utf-8")

        with (
            mock.patch.object(config.secrets, "token_hex", return_value=token),
            self.assertRaises(FileExistsError),
        ):
            config.atomic_write_json(self.cfg_path, {"access_token": "secret"})

        self.assertEqual(collision.read_text(encoding="utf-8"), "do-not-overwrite")
        self.assertFalse(self.cfg_path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows DACL behavior")
    def test_atomic_writer_fails_closed_when_windows_acl_hardening_fails(self):
        with (
            mock.patch.object(
                config.windows_security,
                "harden_private_data_acl",
                side_effect=windows_security.WindowsSecurityError("denied"),
            ),
            self.assertRaisesRegex(config.ShellError, "secure the Windows config directory"),
        ):
            config.atomic_write_json(self.cfg_path, {"access_token": "secret"})

        self.assertFalse(self.cfg_path.exists())

    # --- happy path -------------------------------------------------------
    def test_activate_writes_token_and_sends_contract(self):
        server = _MockServer(
            201,
            {"device_id": "dev_abc", "access_token": "tok_live", "refresh_token": "ref_live", "expires_in": 0},
        )
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)

        summary = activate.activate(timeout_s=5)

        # Request body matches the server contract (api_key -> activation_secret).
        req = server.httpd.last_request
        self.assertEqual(server.httpd.last_path, "/v1/devices/activate")
        self.assertEqual(req["activation_secret"], "act_secret_xyz")
        self.assertEqual(req["device_name"], "test-dev")
        self.assertEqual(req["device_fingerprint"], "fp_fixed")
        # activate reports the RUNNING client version, ignoring the config sentinel (audit 988bd8e1 4/4)
        self.assertEqual(req["client_version"], CLIENT_VERSION)

        # Token persisted back into the SAME config, device_name/fingerprint kept.
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["access_token"], "tok_live")
        self.assertEqual(cfg["device_id"], "dev_abc")
        self.assertEqual(cfg["refresh_token"], "ref_live")
        self.assertEqual(cfg["device_name"], "test-dev")  # preserved
        # The one-time API key is dropped once tokens are issued.
        self.assertNotIn("api_key", cfg)
        self.assertTrue(summary["activated"])
        # POSIX mode bits are meaningful on POSIX; Windows security is ACL-
        # based and reports compatibility mode bits that are not an ACL check.
        # Windows confidentiality is ACL-based; st_mode cannot assert it.
        # Activation preserves the existing per-user config location and writer.
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.cfg_path.stat().st_mode), 0o600)

    def test_server_success_with_local_write_failure_has_a_distinct_error(self):
        server = _MockServer(
            201,
            {"device_id": "dev_abc", "access_token": "tok_live"},
        )
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)

        with mock.patch.object(
            activate, "atomic_write_json", side_effect=OSError("synthetic write failure")
        ):
            with self.assertRaises(activate.ActivationPersistenceError):
                activate.activate(timeout_s=5)

        self.assertEqual(server.httpd.last_path, "/v1/devices/activate")

    def test_from_env_cli_keeps_secret_out_of_argv_and_descendant_environment(self):
        secret = "owner_process_value"

        def activate_from_memory(endpoint, activation_secret, **_kwargs):
            self.assertEqual(endpoint, "https://owner.example")
            self.assertEqual(activation_secret, secret)
            self.assertNotIn("DE_ENDPOINT", os.environ)
            self.assertNotIn("DE_ACTIVATION_SECRET", os.environ)
            return {"activated": True, "device_id": "device-1", "config": str(self.cfg_path)}

        with (
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": secret,
                },
                clear=False,
            ),
            mock.patch.object(
                activate, "activate_with_credentials", side_effect=activate_from_memory
            ) as activate_device,
        ):
            exit_code = activate.main(["--from-env"])

        self.assertEqual(exit_code, 0)
        activate_device.assert_called_once()

    def test_from_env_cli_persistence_failure_returns_recovery_status_without_secret(self):
        secret = "owner_process_value"
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": secret,
                },
                clear=False,
            ),
            mock.patch.object(
                activate,
                "activate_with_credentials",
                side_effect=activate.ActivationPersistenceError("reflected " + secret),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = activate.main(["--from-env"])

        self.assertEqual(exit_code, activate.RECOVERY_REQUIRED_EXIT_CODE)
        self.assertIn("do not retry automatically", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())

    def test_activate_with_credentials_persists_permanent_tokens_not_invite_secret(self):
        server = _MockServer(
            201,
            {
                "device_id": "dev_permanent",
                "access_token": "tok_permanent",
                "refresh_token": "ref_permanent",
            },
        )
        self.addCleanup(server.close)
        config.atomic_write_json(
            self.cfg_path,
            {
                "device_id": "",
                "access_token": "",
                "device_name": "test-dev",
                "device_fingerprint": "fp_fixed",
            },
        )

        summary = activate.activate_with_credentials(
            server.base,
            "owner_invite_value",
            config_path=self.cfg_path,
            timeout_s=5,
            allow_plaintext_local=True,
        )

        self.assertTrue(summary["activated"])
        self.assertEqual(server.httpd.last_request["activation_secret"], "owner_invite_value")
        persisted = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["server_endpoint"], server.base)
        self.assertEqual(persisted["device_id"], "dev_permanent")
        self.assertEqual(persisted["access_token"], "tok_permanent")
        self.assertEqual(persisted["refresh_token"], "ref_permanent")
        self.assertNotIn("api_key", persisted)
        self.assertNotIn("owner_invite_value", self.cfg_path.read_text(encoding="utf-8"))

    def test_activate_with_credentials_failure_does_not_persist_endpoint_or_secret(self):
        server = _MockServer(403, {"error": "activation_secret_invalid"})
        self.addCleanup(server.close)
        original = {
            "device_id": "",
            "access_token": "",
            "device_name": "test-dev",
            "device_fingerprint": "fp_fixed",
        }
        config.atomic_write_json(self.cfg_path, original)

        with self.assertRaises(config.ShellError):
            activate.activate_with_credentials(
                server.base,
                "owner_invite_value",
                config_path=self.cfg_path,
                timeout_s=5,
                allow_plaintext_local=True,
            )

        self.assertEqual(json.loads(self.cfg_path.read_text(encoding="utf-8")), original)
        self.assertNotIn("owner_invite_value", self.cfg_path.read_text(encoding="utf-8"))

    def test_activate_reports_running_version_over_stale_config(self):
        # audit 988bd8e1 (4/4, blocking): a STALE explicit config client_version must NOT be reported —
        # the running CLIENT_VERSION is authoritative, so a 0.2.0 client can never advertise < 0.2.0 and
        # leave the server's version-gated GE byte-isolation dormant.
        server = _MockServer(
            201, {"device_id": "d", "access_token": "t", "refresh_token": "r", "expires_in": 0})
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base, client_version="0.1.0")  # a stale pre-gate value
        activate.activate(timeout_s=5)
        self.assertEqual(server.httpd.last_request["client_version"], CLIENT_VERSION)  # not "0.1.0"

    def test_activated_config_lets_shim_resolve(self):
        server = _MockServer(201, {"device_id": "d1", "access_token": "tok_live"})
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)
        activate.activate(timeout_s=5)
        # The whole point: after activation the shim can build a Forwarder.
        from installer import shim

        fwd = shim.Forwarder.from_config()
        self.assertEqual(fwd.token, "tok_live")

    # --- failure modes ----------------------------------------------------
    def test_403_maps_server_error_and_writes_no_token(self):
        server = _MockServer(403, {"error": "activation_secret_invalid"})
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)
        with self.assertRaises(activate.ActivationRefusedError) as ctx:
            activate.activate(timeout_s=5)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("activation_secret_invalid", str(ctx.exception))
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["access_token"], "")  # untouched, fail closed

    def test_missing_endpoint_raises(self):
        self._write_cfg(server_endpoint="")
        with self.assertRaises(config.ShellError):
            activate.activate(timeout_s=5)

    def test_missing_api_key_raises(self):
        self._write_cfg(server_endpoint="http://localhost:1", api_key="")
        with self.assertRaises(config.ShellError):
            activate.activate(timeout_s=5)

    def test_refuses_plaintext_http_with_api_key(self):
        # A real (non-loopback) http:// endpoint must be rejected before any send.
        self._write_cfg(server_endpoint="http://hub.example.com")
        with self.assertRaises(config.ShellError) as ctx:
            activate.activate(timeout_s=5)
        self.assertIn("plaintext", str(ctx.exception))

    # --- idempotency ------------------------------------------------------
    def test_already_activated_is_noop_without_force(self):
        server = _MockServer(201, {"device_id": "d2", "access_token": "tok_new"})
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base, access_token="tok_existing", device_id="d1")
        summary = activate.activate(timeout_s=5)
        self.assertFalse(summary["activated"])
        self.assertTrue(summary["already_activated"])
        self.assertIsNone(server.httpd.last_request)  # never called the server
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["access_token"], "tok_existing")  # unchanged
        self.assertNotIn("api_key", cfg)

    def test_already_activated_noop_is_structured_and_scrubs_staged_api_key(self):
        server = _MockServer(201, {"device_id": "d2", "access_token": "tok_new"})
        self.addCleanup(server.close)
        self._write_cfg(
            server_endpoint=server.base,
            access_token="tok_existing",
            device_id="d1",
            api_key="legacy_invitation_value",
        )

        summary = activate.activate_with_credentials(
            server.base,
            "unused_new_invitation",
            config_path=self.cfg_path,
            timeout_s=5,
            allow_plaintext_local=True,
        )

        self.assertFalse(summary["activated"])
        self.assertTrue(summary["already_activated"])
        self.assertIsNone(server.httpd.last_request)
        persisted = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["access_token"], "tok_existing")
        self.assertEqual(persisted["device_id"], "d1")
        self.assertNotIn("api_key", persisted)

    def test_partial_token_only_config_does_not_discard_new_activation(self):
        server = _MockServer(201, {"device_id": "d2", "access_token": "tok_new"})
        self.addCleanup(server.close)
        self._write_cfg(
            server_endpoint="",
            access_token="tok_stale",
            device_id="",
            api_key=None,
        )

        summary = activate.activate_with_credentials(
            server.base,
            "new_invitation_value",
            config_path=self.cfg_path,
            timeout_s=5,
            allow_plaintext_local=True,
        )

        self.assertTrue(summary["activated"])
        self.assertEqual(server.httpd.last_request["activation_secret"], "new_invitation_value")
        persisted = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["server_endpoint"], server.base)
        self.assertEqual(persisted["access_token"], "tok_new")
        self.assertEqual(persisted["device_id"], "d2")

    def test_in_memory_credentials_are_https_only_by_default(self):
        server = _MockServer(201, {"device_id": "d2", "access_token": "tok_new"})
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint="", api_key=None)

        with self.assertRaises(config.ShellError) as ctx:
            activate.activate_with_credentials(
                server.base,
                "owner_invite_value",
                config_path=self.cfg_path,
                timeout_s=5,
            )

        self.assertIn("plaintext", str(ctx.exception))
        self.assertIsNone(server.httpd.last_request)

    def test_force_reactivates(self):
        server = _MockServer(201, {"device_id": "d2", "access_token": "tok_new"})
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base, access_token="tok_existing", device_id="d1")
        summary = activate.activate(force=True, timeout_s=5)
        self.assertTrue(summary["activated"])
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["access_token"], "tok_new")
        self.assertEqual(cfg["device_id"], "d2")

    def test_201_without_access_token_is_error(self):
        server = _MockServer(201, {"device_id": "d3"})  # malformed: no token
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)
        with self.assertRaises(config.ShellError):
            activate.activate(timeout_s=5)

    def test_201_without_device_id_is_error(self):
        # Contract 201 carries device_id; a token-only body is rejected fail-closed.
        server = _MockServer(201, {"access_token": "tok_only"})
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)
        with self.assertRaises(config.ShellError):
            activate.activate(timeout_s=5)
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["access_token"], "")  # nothing written

    def test_malformed_response_error_does_not_echo_token(self):
        # A body missing access_token but carrying a refresh_token must NOT leak it
        # into the error text (public-shell leak surface, §14).
        server = _MockServer(200, {"refresh_token": "leaky_secret_value", "device_id": "d"})
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)
        with self.assertRaises(config.ShellError) as ctx:
            activate.activate(timeout_s=5)
        self.assertNotIn("leaky_secret_value", str(ctx.exception))

    def test_non_dict_json_error_body_maps_cleanly(self):
        # A 403 whose body is a JSON array (not object) must still map to a clean
        # ShellError, not an AttributeError from .get().
        server = _MockServer(403, ["not", "a", "dict"])
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)
        with self.assertRaises(config.ShellError):
            activate.activate(timeout_s=5)

    def test_stale_refresh_token_cleared_on_reactivate(self):
        # --force re-activation whose response omits refresh_token must clear the
        # old one rather than leave a mismatched credential pair.
        server = _MockServer(201, {"device_id": "d2", "access_token": "tok_new"})
        self.addCleanup(server.close)
        self._write_cfg(
            server_endpoint=server.base,
            access_token="tok_old",
            device_id="d1",
            refresh_token="ref_old",
        )
        activate.activate(force=True, timeout_s=5)
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["access_token"], "tok_new")
        self.assertEqual(cfg["device_id"], "d2")
        self.assertNotIn("refresh_token", cfg)  # stale token cleared

    def test_refuses_cross_origin_redirect_and_persists_no_foreign_token(self):
        # The exposure that IS real here (the API key itself never rides a redirect —
        # urllib drops the POST body on 301/302/303 and won't follow 307/308 on a POST):
        # a followed redirect lets the TARGET answer the activation, so the client would
        # persist an access_token minted by a host that is not the configured hub.
        # (Supersedes the old test_refuses_https_to_http_redirect, which despite its name
        # ran entirely over http and only ever exercised a cross-HOST redirect.)
        elsewhere = _MockServer(200, {"access_token": "attacker_tok", "device_id": "d_evil"})
        self.addCleanup(elsewhere.close)
        server = _MockServer(302, {}, redirect_to=elsewhere.base + "/v1/devices/activate")
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)
        with self.assertRaises(config.ShellError):
            activate.activate(timeout_s=5)
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["access_token"], "")          # no foreign token written
        self.assertEqual(elsewhere.httpd.last_path, None)  # the target was never contacted

    def test_refuses_a_same_origin_redirect_too(self):
        # Refuse-all, matching the shim's artifact fetch. Following even a same-origin
        # redirect could never complete an activation anyway: urllib rewrites a 301/302/303
        # POST into a body-less GET, so the activation_secret is gone by hop 2 and no real
        # hub could mint a token from it. Refusing turns a confusing failure into a clear one.
        server = _MockServer(302, {"access_token": "tok_ok", "device_id": "d1"},
                             redirect_to="/v1/devices/activate/")
        self.addCleanup(server.close)
        self._write_cfg(server_endpoint=server.base)
        with self.assertRaises(config.ShellError):
            activate.activate(timeout_s=5)
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["access_token"], "")   # nothing written
        self.assertEqual(server.httpd.last_path, "/v1/devices/activate")  # hop 2 never happened

    def test_no_redirect_code_is_followed_and_none_reaches_the_target(self):
        # The whole 3xx matrix, locked: none of them may contact the target or write a token.
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                elsewhere = _MockServer(200, {"access_token": "attacker_tok", "device_id": "d_evil"})
                self.addCleanup(elsewhere.close)
                server = _MockServer(code, {}, redirect_to=elsewhere.base + "/v1/devices/activate")
                self.addCleanup(server.close)
                self._write_cfg(server_endpoint=server.base)
                with self.assertRaises(config.ShellError):
                    activate.activate(timeout_s=5)
                cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(cfg["access_token"], "")
                self.assertIsNone(elsewhere.httpd.last_path)


if __name__ == "__main__":
    unittest.main()
