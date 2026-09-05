"""End-to-end (real Git, not fully mocked) behavior locks for the Phase 3
new-install bootstrap. This is the first test in the suite that actually
assembles a dual-remote, signed-tag checkout satisfying
``managed_activation.activate_prepared_install``'s full precondition set,
rather than mocking every Git/crypto boundary (see the internal design
notes on managed-git-update, Phase 3 plan, Q4).

Fixture design:
  - One local plain (non-bare) Git repo stands in for *both* official
    mirrors — content is identical either way, so there is no need to
    maintain two physical repos.
  - A fixed, test-only RSA-2048 keypair (generated once offline with PyCA
    ``cryptography``, hardcoded here as plain integers — matching the
    project's existing "computed once with cryptography, embedded as a
    known-answer constant" convention, e.g. ``de_private_repo.py`` tests)
    signs a manifest built *after* the real commit hash is known, using a
    pure stdlib PKCS#1 v1.5 SHA-256 implementation that mirrors
    ``release_contract.verify_release_signature`` exactly.
  - ``git -c url.<local path>.insteadOf=<official URL>`` (passed as an
    explicit ``-c`` flag, not written to any config file) transparently
    redirects the bootstrap module's real official-URL clone/fetch calls to
    the local fixture, while the *recorded* ``remote.<name>.url`` in the
    resulting checkout stays byte-identical to the official URL — satisfying
    ``managed_install._validate_official_remotes``'s exact-match check.
    ``-c`` flags are per-invocation and are not suppressed by
    ``updater._git_environment``'s ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_NOSYSTEM``
    hardening (those only block *ambient* config files); production code paths
    have no parameter that could set this to anything but ``()`` — only this
    test's ``mock.patch.object(bootstrap, "_test_only_extra_git_config", ...)``
    (see ``setUp``) ever makes it non-empty.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from unittest import mock

from installer import (
    bootstrap_managed_install as bootstrap,
    config,
    managed_install,
    mcp_config,
    permanent_setup,
    release_acquisition,
    release_contract,
    update_transaction,
)


# Test-only RSA-2048 keypair, generated once with PyCA cryptography 49.0.0.
# No production key material; never used to sign anything but this test's own
# fixture manifest. Split into short concatenated hex fragments (mirroring
# user-script/tests/test_de_private_repo.py's own external-vector convention)
# so no single line has a 40+ char contiguous hex/decimal run — otherwise
# installer/leak_scan.py's bearer-hex-token detector flags this fixture as a
# shipped secret (a real finding for real secrets; a false positive for a
# fake test-only key, which is exactly why the scanner has no blanket
# exemption for "looks like a key" and callers must avoid the shape instead).
_TEST_KEY_ID = "test-fixture-key-1"
_N = int(
    "c07b1b7fc476d7efe674775709c8f4d5"
    "3f93f69547e98b85d5b94d73bb759dde"
    "9c6f910726dffe39a64188617d94781a"
    "4f6593b1718d75d4061d3cd3d15f5b2f"
    "caa88adc97d898cd61ece5d1447748db"
    "e6474834d69bbab3ab4279732b31675f"
    "09e6b85c1b8dab3912eed4b4e7e7ecc3"
    "18d4b552090437568046696c8126d632"
    "ae39e85f3ee70f821024b2ecd78daca3"
    "88d5d0dda1ddb0437db603481dfc9883"
    "139bb7c028af558aec1bdbb557a9fe52"
    "363e8490a6d176be65a130b5d4c21d2a"
    "de95b4b3a264a7df35c463b7e8b41ea8"
    "89f142d1b08bc86f44764e9549a8bfaf"
    "404b2bfb772f5e8bdd214764c5e63a78"
    "58a5e9a82ab6a35f0ce1dc990e876c0b",
    16,
)
_E = 65537
_D = int(
    "2d14c85088fdc2be9bedbc43c4f56dae"
    "82790fc04ffaf9a42b75fe977418ec94"
    "467474f5c55ba209f39016b6aab87ad6"
    "c2b0ca423d93c1bc9a7fff8ea6d39c22"
    "59e756baaacfde9dffe901bc9d3a08f5"
    "03febf012d20853e6e869fb6632feb83"
    "9d688dd678d799fa409e72703a7e9dfb"
    "e5d56ff05a4f43fcbd0780ca9a873394"
    "7c0b8383db128bb756db45a8eeac8279"
    "5e87b3dfac121cfb9a53568954700311"
    "bcb725e8e77134406897e456d25773d0"
    "2d24357663f2041e5cc51fb01b0bfaba"
    "270d89075c89829b53d3ac440af1e852"
    "ab8cbcf75006023186267d23669cbe90"
    "851a09eacb63a234e6e78e945ed44dc4"
    "e04f6763c63a934e64b327fdcf8c750d",
    16,
)
_DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


def _sign(message: bytes) -> str:
    width = (_N.bit_length() + 7) // 8
    digest = hashlib.sha256(message).digest()
    encoded = _DIGEST_INFO_SHA256 + digest
    padding_len = width - len(encoded) - 3
    block = b"\x00\x01" + (b"\xff" * padding_len) + b"\x00" + encoded
    signature_int = pow(int.from_bytes(block, "big"), _D, _N)
    return base64.b64encode(signature_int.to_bytes(width, "big")).decode("ascii")


def _run_git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.decode("utf-8", "replace").strip()


class BootstrapTrustRootTests(unittest.TestCase):
    def test_default_trust_root_loads_keys_from_the_running_checkout(self):
        expected_root = Path(__file__).resolve().parents[2]
        trust_root = bootstrap._default_trust_root()

        self.assertEqual(trust_root, expected_root)
        self.assertTrue(
            (trust_root / release_acquisition.TRUST_STORE_RELATIVE_PATH).is_file()
        )
        self.assertTrue(release_acquisition.load_trusted_release_keys(trust_root))

    def test_public_bootstrap_api_has_no_trust_root_override(self):
        import inspect

        self.assertNotIn(
            "trust_root",
            inspect.signature(bootstrap.bootstrap_new_install).parameters,
        )


@unittest.skipUnless(
    __import__("shutil").which("git"), "git is required for this fixture"
)
class BootstrapManagedInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.canonical_root = self.home / ".deeppattern" / "decision-engine"

        self._patches = [
            mock.patch.object(managed_install.config, "DEFAULT_DEEPPATTERN_HOME", self.home / ".deeppattern"),
            mock.patch.dict(os.environ, {
                "DEEPPATTERN_HOME": str(self.home / ".deeppattern"),
                "CLAUDE_CODE_CONFIG": str(self.base / "claude-code.json"),
                "CLAUDE_DESKTOP_CONFIG": str(self.base / "claude-desktop.json"),
                "CODEX_CONFIG": str(self.base / "codex" / "config.toml"),
                "CODEX_AGENTS_MD": str(self.base / "codex" / "AGENTS.md"),
                "CURSOR_CONFIG": str(self.base / "cursor" / "mcp.json"),
                "QODER_CN_CONFIG": str(self.base / "qoder-cn" / "settings.json"),
                "QODER_CN_SKILLS_DIR": str(self.base / "no-qoder-cn" / "skills"),
                "QODER_CN_APP_ROOT": str(self.base / "no-qoder-cn" / "app"),
                "TRAE_CONFIG": str(self.base / "trae" / "User" / "mcp.json"),
                "TRAE_SKILLS_DIR": str(self.base / "no-trae" / "skills"),
                "TRAE_APP_ROOT": str(self.base / "no-trae" / "app"),
                "TRAE_CN_CONFIG": str(self.base / "trae-cn" / "User" / "mcp.json"),
                "TRAE_CN_SKILLS_DIR": str(self.base / "no-trae-cn" / "skills"),
                "TRAE_CN_APP_ROOT": str(self.base / "no-trae-cn" / "app"),
                "TRAE_WORK_CONFIG": str(
                    self.base / "trae-work" / "User" / "mcp.json"
                ),
                "TRAE_WORK_SKILLS_DIR": str(
                    self.base / "no-trae-work" / "skills"
                ),
                "TRAE_WORK_APP_ROOT": str(self.base / "no-trae-work" / "app"),
                "TRAE_WORK_CN_CONFIG": str(
                    self.base / "trae-work-cn" / "User" / "mcp.json"
                ),
                "TRAE_WORK_CN_SKILLS_DIR": str(
                    self.base / "no-trae-work-cn" / "skills"
                ),
                "TRAE_WORK_CN_APP_ROOT": str(
                    self.base / "no-trae-work-cn" / "app"
                ),
            }, clear=False),
            # The production module exposes no public parameter for this —
            # ``extra_git_config`` used to be a public keyword threaded through
            # ``bootstrap_new_install`` itself, which a deep audit flagged as a
            # standing supply-chain surface (an unrestricted `-c` key set,
            # reachable by any future caller). Only this private module
            # attribute, reachable exclusively via ``mock.patch.object`` from
            # test code, can produce a non-empty value now.
            mock.patch.object(
                bootstrap, "_test_only_extra_git_config",
                side_effect=lambda: self._extra_git_config(),
            ),
            # The real trust anchor is this bootstrap module's own DE_ROOT
            # (see _default_trust_root's docstring) -- never the freshly
            # cloned `stage`, which never carries it (stable is a
            # manifest-only metadata channel). In production this resolves
            # to the actual installer/release-trust.json next to this code;
            # here it must resolve to the fixture's own upstream checkout,
            # which is the one place in this test carrying the test key.
            mock.patch.object(
                bootstrap, "_default_trust_root",
                side_effect=lambda: self.upstream,
            ),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

        self.upstream = self._build_upstream_repo()

    def _build_upstream_repo(self) -> Path:
        upstream = self.base / "upstream"
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", "-b", "stable", str(upstream)], check=True, env=env)
        _run_git(["config", "user.email", "fixture@example.test"], upstream)
        _run_git(["config", "user.name", "Fixture"], upstream)
        (upstream / "VERSION").write_text("0.9.9\n", encoding="utf-8")
        trust_dir = upstream / "installer"
        trust_dir.mkdir()
        trust_store = {
            "schema": 1,
            "keys": [{
                "key_id": _TEST_KEY_ID,
                "algorithm": release_contract.RSA_SHA256_ALGORITHM,
                "modulus_hex": format(_N, "x"),
                "exponent": _E,
                "revoked": False,
            }],
        }
        import json
        (trust_dir / "release-trust.json").write_text(
            json.dumps(trust_store, sort_keys=True), encoding="utf-8"
        )
        _run_git(["add", "-A"], upstream)
        _run_git(["commit", "-q", "-m", "initial"], upstream)
        commit = _run_git(["rev-parse", "HEAD"], upstream)
        _run_git(["tag", "v0.9.9", commit], upstream)
        self.release_commit = commit
        return upstream

    def _acquired_release(self) -> release_acquisition.AcquiredRelease:
        manifest = release_contract.ReleaseManifest(
            schema=1, repository_id=managed_install.REPOSITORY_ID, channel="stable",
            release_sequence=1, version="0.9.9", tag="v0.9.9", commit=self.release_commit,
            min_python="3.12", published_at="2026-01-01T00:00:00Z", key_id=_TEST_KEY_ID,
        )
        message = release_contract.canonical_manifest_bytes(manifest)
        signature = release_contract.ReleaseSignature(
            schema=1, algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id=_TEST_KEY_ID, signature=_sign(message),
        )
        return release_acquisition.AcquiredRelease(
            source=release_acquisition.GITHUB_SOURCE, manifest=manifest, signature=signature,
        )

    def _stub_fetch_document(self, source, kind, *, deadline):
        import json
        acquired = self._acquired_release()
        if kind == "manifest":
            payload = {
                "schema": acquired.manifest.schema,
                "repository_id": acquired.manifest.repository_id,
                "channel": acquired.manifest.channel,
                "release_sequence": acquired.manifest.release_sequence,
                "version": acquired.manifest.version,
                "tag": acquired.manifest.tag,
                "commit": acquired.manifest.commit,
                "min_python": acquired.manifest.min_python,
                "published_at": acquired.manifest.published_at,
                "key_id": acquired.manifest.key_id,
            }
            return json.dumps(payload).encode("utf-8")
        if kind == "signature":
            payload = {
                "schema": acquired.signature.schema,
                "algorithm": acquired.signature.algorithm,
                "key_id": acquired.signature.key_id,
                "signature": acquired.signature.signature,
            }
            return json.dumps(payload).encode("utf-8")
        raise AssertionError("unexpected document kind: %s" % kind)

    def _extra_git_config(self):
        return tuple(
            "url.%s.insteadOf=%s" % (str(self.upstream), url)
            for _name, url in managed_install.OFFICIAL_REMOTE_URLS
        )

    def _patched_discover_initial_release(self):
        """Route the un-parameterized call inside ``managed_activation`` through
        the *real* verification logic with our local ``fetch_document`` — see
        module docstring for why this differs from patching ``fetch_document``
        directly (a bound default parameter is captured at def-time)."""
        real = release_acquisition.discover_initial_release
        stub_fetch = self._stub_fetch_document

        def _wrapped(trusted_keys, *, deadline, fetch_document=stub_fetch,
                     sources=release_acquisition.RELEASE_SOURCES):
            return real(trusted_keys, deadline=deadline, fetch_document=stub_fetch, sources=sources)

        return mock.patch.object(release_acquisition, "discover_initial_release", _wrapped)

    def test_fresh_install_prepares_updates_without_publishing_mcp(self):
        json_bytes = b'{"mcpServers":{"keep":{"command":"keep"}}}\n'
        host_files = {
            Path(os.environ["CLAUDE_CODE_CONFIG"]): json_bytes,
            Path(os.environ["CLAUDE_DESKTOP_CONFIG"]): json_bytes,
            Path(os.environ["CODEX_CONFIG"]): b'[mcp_servers.keep]\ncommand = "keep"\n',
            Path(os.environ["CODEX_AGENTS_MD"]): b"# keep this routing\n",
            Path(os.environ["CURSOR_CONFIG"]): json_bytes,
        }
        for path, content in host_files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        before = {path: path.read_bytes() for path in host_files}

        with self._patched_discover_initial_release():
            result = bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root,
                server_endpoint="https://example.test",
                api_key=None,
                device_name="fixture-device",
                clients=["claude-code", "claude-desktop", "codex", "cursor"],
            )

        self.assertEqual(result.version, "0.9.9")
        self.assertEqual(result.commit, self.release_commit)
        self.assertFalse(result.reused_existing)
        self.assertTrue((self.canonical_root / ".git").is_dir())
        self.assertTrue(managed_install.marker_path(self.canonical_root).exists())

        remotes = dict(managed_install.OFFICIAL_REMOTE_URLS)
        recorded = {}
        for name, _url in remotes.items():
            recorded[name] = _run_git(["remote", "get-url", name], self.canonical_root)
        self.assertEqual(recorded, remotes)

        protocol_marker = self.canonical_root / update_transaction.PROTOCOL_READY_RELATIVE_PATH
        self.assertTrue(protocol_marker.exists())

        self.assertEqual(
            {path: path.read_bytes() for path in host_files},
            before,
        )
        self.assertEqual(
            result.clients,
            ("claude-code", "claude-desktop", "codex", "cursor"),
        )

    def test_windows_policy_is_pinned_before_the_first_checkout(self):
        events = []
        real_checkout = bootstrap._checkout_release

        def record_pin(repo):
            events.append(("pin", repo))

        def record_checkout(stage, manifest):
            events.append(("checkout", stage))
            return real_checkout(stage, manifest)

        with (
            self._patched_discover_initial_release(),
            mock.patch.object(bootstrap, "_pin_windows_checkout_policy", side_effect=record_pin),
            mock.patch.object(bootstrap, "_checkout_release", side_effect=record_checkout),
        ):
            bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root, clients=["claude-code"],
            )

        first_pin = next(index for index, event in enumerate(events) if event[0] == "pin")
        checkout = next(index for index, event in enumerate(events) if event[0] == "checkout")
        self.assertLess(first_pin, checkout)

    def test_verified_release_uses_a_sandbox_compatible_shallow_ref(self):
        with self._patched_discover_initial_release():
            bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root, clients=["claude-code"],
            )

        refs = _run_git(
            ["for-each-ref", "--format=%(refname)"], self.canonical_root
        ).splitlines()
        verification_refs = [ref for ref in refs if "bootstrap-verified" in ref]
        self.assertEqual(verification_refs, ["refs/bootstrap-verified-v0.9.9"])
        self.assertEqual(
            _run_git(
                ["rev-parse", "--verify", "refs/tags/v0.9.9^{commit}"],
                self.canonical_root,
            ),
            self.release_commit,
        )

    def test_ambient_autocrlf_true_still_produces_a_clean_managed_checkout(self):
        (self.home / ".gitconfig").write_text(
            "[core]\n\tautocrlf = true\n", encoding="utf-8",
        )

        with (
            self._patched_discover_initial_release(),
            mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False),
            mock.patch.object(
                bootstrap, "_windows_checkout_policy_required", return_value=True,
            ),
        ):
            bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root, clients=["claude-code"],
            )

        self.assertEqual(
            _run_git(
                ["status", "--porcelain=v1", "--untracked-files=no"],
                self.canonical_root,
            ),
            "",
        )
        self.assertEqual(
            _run_git(["config", "--local", "--get", "core.autocrlf"], self.canonical_root),
            "false",
        )

    def test_rerun_against_an_already_activated_root_is_a_clean_no_op(self):
        with self._patched_discover_initial_release():
            first = bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root,
                clients=["claude-code"],
            )
            second = bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root,
                clients=["claude-code"],
            )
        self.assertTrue(second.reused_existing)
        self.assertEqual(first.commit, second.commit)
        self.assertEqual(first.version, second.version)

    def test_repair_wiring_rewires_an_already_activated_root(self):
        with self._patched_discover_initial_release():
            bootstrap.bootstrap_new_install(canonical_root=self.canonical_root, clients=["claude-code"])
        config_path = self.canonical_root / "config.json"
        device_config = config.load_json(config_path)
        device_config.update(
            {
                "server_endpoint": "https://owner.example",
                "device_id": "device-fixture",
                "access_token": "test-access-token",
            }
        )
        config.atomic_write_json(config_path, device_config)
        targets = bootstrap.repair_wiring(canonical_root=self.canonical_root, clients=["claude-code"])
        self.assertEqual(targets, ("claude-code",))

    def test_repair_wiring_refuses_core_ready_but_unactivated_root(self):
        with self._patched_discover_initial_release():
            bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root,
                clients=["claude-code"],
            )
        with self.assertRaisesRegex(bootstrap.BootstrapError, "device activation is incomplete"):
            bootstrap.repair_wiring(
                canonical_root=self.canonical_root,
                clients=["claude-code"],
            )

    def test_repair_wiring_refuses_a_marker_without_a_matching_identity(self):
        # A directory that merely *looks* activated (correct remotes, the
        # runtime protocol marker present) but was never actually taken
        # through activation has no ``.managed-install.json`` marker / paired
        # private registration — repair_wiring must not trust the runtime
        # marker alone (that was the audited gap: it used to check only
        # ``protocol_marker.exists()``).
        subprocess.run(
            ["git", "init", "-q", "-b", "stable", str(self.canonical_root)],
            check=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        for name, url in managed_install.OFFICIAL_REMOTE_URLS:
            _run_git(["remote", "add", name, url], self.canonical_root)
        protocol_marker = self.canonical_root / update_transaction.PROTOCOL_READY_RELATIVE_PATH
        protocol_marker.parent.mkdir(parents=True, exist_ok=True)
        protocol_marker.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            bootstrap.BootstrapError, "does not have a valid managed install identity"
        ):
            bootstrap.repair_wiring(canonical_root=self.canonical_root, clients=["claude-code"])

    def test_refuses_a_preexisting_directory_that_is_not_a_managed_checkout(self):
        self.canonical_root.mkdir(parents=True)
        (self.canonical_root / "some_file.txt").write_text("not a git repo", encoding="utf-8")
        with self.assertRaisesRegex(bootstrap.BootstrapError, "not a managed Decision Engine checkout"):
            bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root,
                clients=["claude-code"],
            )
        # Refusal must not have touched the pre-existing directory's contents.
        self.assertTrue((self.canonical_root / "some_file.txt").exists())
        self.assertFalse((self.canonical_root / ".git").exists())

    def test_signature_failure_quarantines_staging_and_leaves_canonical_root_absent(self):
        tampered_key_id = _TEST_KEY_ID + "-wrong"

        def _bad_fetch(source, kind, *, deadline):
            data = self._stub_fetch_document(source, kind, deadline=deadline)
            if kind == "signature":
                import json
                payload = json.loads(data)
                payload["key_id"] = tampered_key_id
                return json.dumps(payload).encode("utf-8")
            return data

        real = release_acquisition.discover_initial_release

        def _wrapped(trusted_keys, *, deadline, fetch_document=_bad_fetch,
                     sources=release_acquisition.RELEASE_SOURCES):
            return real(trusted_keys, deadline=deadline, fetch_document=_bad_fetch, sources=sources)

        with mock.patch.object(release_acquisition, "discover_initial_release", _wrapped):
            with self.assertRaises(Exception):
                bootstrap.bootstrap_new_install(
                    canonical_root=self.canonical_root,
                    clients=["claude-code"],
                    )
        self.assertFalse(self.canonical_root.exists())
        quarantined = list((self.home / ".deeppattern").glob(".decision-engine.bootstrapping.failed-*"))
        self.assertTrue(quarantined, "expected a quarantined staging directory to remain for inspection")

    def test_anonymous_https_unreachable_falls_back_to_authenticated_git(self):
        # Simulates the still-private-repository case: anonymous HTTPS mirrors
        # 404 for everyone regardless of the caller's own git credentials, but
        # this bootstrap's own `git clone` already authenticated with the
        # human's credentials, so the one-time first install must still
        # complete via the release/manifest.json this clone already has on
        # disk. Push a second commit onto the fixture's stable branch so the
        # tracked release files exist at the ref `_bootstrap_fresh_clone`
        # checks out (the tag from setUp still points at the original,
        # unrelated release commit — unaffected by this second commit).
        import json

        acquired = self._acquired_release()
        release_dir = self.upstream / "release"
        release_dir.mkdir()
        (release_dir / "manifest.json").write_text(
            json.dumps(asdict(acquired.manifest)), encoding="utf-8",
        )
        (release_dir / "manifest.sig.json").write_text(
            json.dumps(asdict(acquired.signature)), encoding="utf-8",
        )
        _run_git(["add", "-A"], self.upstream)
        _run_git(["commit", "-q", "-m", "publish release files"], self.upstream)

        with mock.patch.object(
            release_acquisition, "discover_initial_release",
            side_effect=release_acquisition.ReleaseTransportError("simulated anonymous 404"),
        ):
            result = bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root,
                clients=["claude-code"],
            )

        self.assertEqual(result.version, "0.9.9")
        self.assertEqual(result.commit, self.release_commit)
        self.assertTrue(managed_install.marker_path(self.canonical_root).exists())
        protocol_marker = self.canonical_root / update_transaction.PROTOCOL_READY_RELATIVE_PATH
        self.assertTrue(protocol_marker.exists())

    def test_clone_uses_ambient_not_hardened_git_environment(self):
        # Regression guard: this whole module's own clone/fetch/checkout
        # calls (_clone_staging, _fetch_and_verify_tag, _checkout_release)
        # must resolve the
        # caller's real credential helper / SSH agent —
        # updater._git_environment's stripped HOME/GIT_CONFIG_GLOBAL made a
        # real private-repo clone fail live against github.com with "could
        # not read Username ... terminal prompts disabled" during
        # development. (Other local, read-only inspection elsewhere in the
        # same flow, e.g. via updater._GitReader, may still legitimately use
        # the hardened environment — this only asserts the ambient one is
        # actually wired into this module's own git calls, not that the
        # hardened one is never used by anything else.)
        with (
            self._patched_discover_initial_release(),
            mock.patch.object(
                bootstrap.updater, "_ambient_git_environment",
                wraps=bootstrap.updater._ambient_git_environment,
            ) as ambient,
        ):
            bootstrap.bootstrap_new_install(
                canonical_root=self.canonical_root, clients=["claude-code"],
            )
        self.assertGreater(ambient.call_count, 0)


class BootstrapCliTests(unittest.TestCase):
    def test_permanent_setup_subprocess_keeps_secret_out_of_argv(self):
        secret = "owner_process_value"
        completed = subprocess.CompletedProcess([sys.executable], 0)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": secret,
                },
                clear=False,
            ),
            mock.patch.object(bootstrap.subprocess, "run", return_value=completed) as run,
        ):
            exit_code = bootstrap._run_permanent_setup_from_env(
                Path("/managed/decision-engine"),
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": secret,
                },
                clients=("codex", "cursor"),
            )

        self.assertEqual(exit_code, 0)
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                sys.executable,
                "-m",
                "installer.permanent_setup",
                "--from-env",
                "--client",
                "codex",
                "--client",
                "cursor",
            ],
        )
        self.assertNotIn(secret, command)
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["env"]["DE_ACTIVATION_SECRET"], secret)

    def test_core_bootstrap_defers_mcp_wiring_until_permanent_setup(self):
        root = Path("/managed/decision-engine")
        activation = bootstrap.managed_activation.ActivationResult(
            root,
            "0.9.9",
            "a" * 40,
            "github",
            ("codex",),
        )
        with (
            mock.patch.object(bootstrap.install_module, "install_lock"),
            mock.patch.object(bootstrap, "_bootstrap_fresh_clone"),
            mock.patch.object(bootstrap, "_pin_windows_checkout_policy"),
            mock.patch.object(bootstrap.install_module, "run_install"),
            mock.patch.object(
                bootstrap.managed_activation,
                "activate_prepared_install",
                return_value=activation,
            ) as activate,
        ):
            result = bootstrap.bootstrap_new_install(
                canonical_root=root,
                clients=["codex"],
            )

        self.assertEqual(result.clients, ("codex",))
        activate.assert_called_once()
        self.assertEqual(activate.call_args.kwargs["clients"], ["codex"])
        self.assertNotIn("wire_clients", activate.call_args.kwargs)

    def test_persistence_uncertainty_forbids_retry_advice(self):
        stderr = io.StringIO()
        completed = subprocess.CompletedProcess(
            [sys.executable], permanent_setup.RECOVERY_REQUIRED_EXIT_CODE
        )
        with (
            mock.patch.object(bootstrap.subprocess, "run", return_value=completed),
            redirect_stderr(stderr),
        ):
            exit_code = bootstrap._run_permanent_setup_from_env(
                Path("/managed/decision-engine"), {}
            )

        self.assertEqual(exit_code, permanent_setup.RECOVERY_REQUIRED_EXIT_CODE)
        message = stderr.getvalue()
        self.assertIn("do not retry automatically", message)
        self.assertIn("recovery marker was retained", message)
        self.assertIn("automatic updates remain ready", message)
        self.assertNotIn("Correct the owner values", message)
        message.encode("ascii")

    def test_permanent_setup_timeout_is_bounded_and_requires_safe_recovery(self):
        secret = "owner_process_value"
        stderr = io.StringIO()
        run = mock.Mock(
            side_effect=subprocess.TimeoutExpired(
                [sys.executable, "-m", "installer.permanent_setup", "--from-env"],
                timeout=bootstrap._PERMANENT_SETUP_TIMEOUT_SECONDS,
            )
        )
        with (
            mock.patch.object(bootstrap.subprocess, "run", run),
            redirect_stderr(stderr),
        ):
            exit_code = bootstrap._run_permanent_setup_from_env(
                Path("/managed/decision-engine"),
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": secret,
                },
            )

        self.assertEqual(exit_code, permanent_setup.RECOVERY_REQUIRED_EXIT_CODE)
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            bootstrap._PERMANENT_SETUP_TIMEOUT_SECONDS,
        )
        command = run.call_args.args[0]
        self.assertNotIn(secret, command)
        self.assertEqual(run.call_args.kwargs["env"]["DE_ACTIVATION_SECRET"], secret)
        message = stderr.getvalue()
        self.assertIn("timed out", message)
        self.assertIn("do not retry automatically", message)
        self.assertIn("Preserve any recovery marker", message)
        self.assertIn("owner-guided recovery", message)
        self.assertNotIn("recovery marker was retained", message)
        self.assertNotIn(secret, message)
        message.encode("ascii")

    def test_install_cli_hides_owner_secret_from_non_activation_steps(self):
        secret = "owner_process_value"
        result = bootstrap.BootstrapResult(
            root=Path("/managed/decision-engine"),
            version="0.9.9",
            commit="a" * 40,
            source="github",
            clients=("codex",),
            reused_existing=False,
        )
        events = []

        def core_install(**_kwargs):
            events.append(("core", "DE_ACTIVATION_SECRET" in os.environ))
            return result

        def activate_from_env(root, owner_environment, *, clients=()):
            events.append(
                (
                    "activation",
                    "DE_ACTIVATION_SECRET" in os.environ,
                    root,
                    owner_environment.get("DE_ACTIVATION_SECRET") == secret,
                    tuple(clients),
                )
            )
            return 0

        def prepare_gui(_python):
            events.append(("gui", "DE_ACTIVATION_SECRET" in os.environ))

        with (
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": secret,
                },
                clear=False,
            ),
            mock.patch.object(bootstrap, "bootstrap_new_install", side_effect=core_install),
            mock.patch.object(
                bootstrap, "_run_permanent_setup_from_env", side_effect=activate_from_env
            ),
            mock.patch.object(
                bootstrap.gui_setup, "prepare_gui_environment", side_effect=prepare_gui
            ),
        ):
            exit_code = bootstrap.main(["install", "--client", "codex"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            events,
            [
                ("core", False),
                ("activation", False, result.root, True, ("codex",)),
                ("gui", False),
            ],
        )

    def test_install_cli_prepares_gui_with_the_mcp_python(self):
        result = bootstrap.BootstrapResult(
            root=Path("/managed/decision-engine"),
            version="0.9.9",
            commit="a" * 40,
            source="github",
            clients=("codex",),
            reused_existing=False,
        )
        prepare_gui = mock.Mock()
        gui_module = mock.Mock(prepare_gui_environment=prepare_gui)
        activate_from_env = mock.Mock(return_value=0)
        with (
            mock.patch.object(bootstrap, "bootstrap_new_install", return_value=result),
            mock.patch.object(bootstrap, "gui_setup", gui_module, create=True),
            mock.patch.object(
                bootstrap,
                "_run_permanent_setup_from_env",
                activate_from_env,
                create=True,
            ),
        ):
            exit_code = bootstrap.main(["install", "--client", "codex"])

        self.assertEqual(exit_code, 0)
        prepare_gui.assert_called_once_with(sys.executable)
        self.assertEqual(activate_from_env.call_count, 1)
        self.assertEqual(activate_from_env.call_args.args[0], result.root)

    def test_repair_wiring_cli_prints_host_declared_follow_up(self):
        stdout = io.StringIO()
        notice = "host-specific manual follow-up"
        with (
            mock.patch.object(
                bootstrap, "repair_wiring", return_value=("workbuddy",)
            ),
            mock.patch.object(
                bootstrap.mcp_config,
                "post_mcp_write_notices",
                return_value=(notice,),
            ) as notices,
            redirect_stdout(stdout),
        ):
            exit_code = bootstrap.main(
                ["repair-wiring", "--client", "workbuddy"]
            )

        self.assertEqual(exit_code, 0)
        notices.assert_called_once_with(("workbuddy",))
        self.assertIn(notice, stdout.getvalue())

    def test_optional_gui_failure_does_not_undo_successful_core_install(self):
        result = bootstrap.BootstrapResult(
            root=Path("/managed/decision-engine"),
            version="0.9.9",
            commit="a" * 40,
            source="github",
            clients=("codex",),
            reused_existing=False,
        )
        with (
            mock.patch.object(bootstrap, "bootstrap_new_install", return_value=result),
            mock.patch.object(
                bootstrap, "_run_permanent_setup_from_env", return_value=0, create=True
            ),
            mock.patch.object(
                bootstrap.gui_setup,
                "prepare_gui_environment",
                side_effect=RuntimeError("synthetic optional failure"),
            ),
        ):
            exit_code = bootstrap.main(["install", "--client", "codex"])

        self.assertEqual(exit_code, 0)

    def test_rejected_owner_values_do_not_undo_successful_core_install(self):
        result = bootstrap.BootstrapResult(
            root=Path("/managed/decision-engine"),
            version="0.9.9",
            commit="a" * 40,
            source="github",
            clients=("codex",),
            reused_existing=False,
        )
        with (
            mock.patch.object(bootstrap, "bootstrap_new_install", return_value=result),
            mock.patch.object(
                bootstrap, "_run_permanent_setup_from_env", return_value=1
            ),
            mock.patch.object(
                bootstrap.gui_setup, "prepare_gui_environment"
            ),
        ):
            exit_code = bootstrap.main(["install", "--client", "codex"])

        self.assertEqual(exit_code, 0)


class PinWindowsCheckoutPolicyTests(unittest.TestCase):
    """Problem-1 fix: a managed checkout's LOCAL core.autocrlf/core.symlinks must
    be pinned on Windows, or updater._validate_local_checkout_policy makes every
    managed op exit 1 ('managed Windows checkout must pin local core.autocrlf and
    core.symlinks'). Git-for-Windows sets core.symlinks locally but core.autocrlf
    only globally, so `--local --get core.autocrlf` reads unset; nothing pinned
    it before this fix. The Windows path can't run on POSIX CI, so it is covered
    by mocking os.name / _git — locking the config commands + arg order + values;
    a real Windows machine still validates it end to end."""

    def test_windows_pins_both_local_configs_to_false(self):
        repo = Path("/managed/root")  # build before patching os.name (WindowsPath won't instantiate on POSIX)
        calls = []

        def fake_git(executable, environment, root, *args, timeout, extra_config=()):
            calls.append((root, args))
            return b""

        with (
            mock.patch.object(
                bootstrap, "_windows_checkout_policy_required", return_value=True,
            ),
            mock.patch.object(bootstrap.updater, "_resolve_git_executable", return_value="git"),
            mock.patch.object(bootstrap.updater, "_ambient_git_environment", return_value={}),
            mock.patch.object(bootstrap, "_git", side_effect=fake_git),
        ):
            bootstrap._pin_windows_checkout_policy(repo)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], ("config", "--local", "core.autocrlf", "false"))
        self.assertEqual(calls[1][1], ("config", "--local", "core.symlinks", "false"))
        self.assertTrue(all(root == repo for root, _ in calls))

    def test_clone_staging_defers_the_first_checkout(self):
        calls = []

        def fake_git(executable, environment, root, *args, timeout, extra_config=()):
            calls.append((root, args))
            return b""

        stage = Path("/managed/stage")
        with (
            mock.patch.object(bootstrap.updater, "_resolve_git_executable", return_value="git"),
            mock.patch.object(bootstrap.updater, "_ambient_git_environment", return_value={}),
            mock.patch.object(bootstrap, "_git", side_effect=fake_git),
        ):
            selected = bootstrap._clone_staging(
                stage, (("github", "https://example.invalid/repository.git"),),
            )

        self.assertEqual(selected, "github")
        self.assertEqual(calls[0][0], None)
        self.assertEqual(
            calls[0][1],
            (
                "clone", "--no-checkout", "--origin", "github", "--",
                "https://example.invalid/repository.git", str(stage),
            ),
        )

    def test_noop_off_windows(self):
        repo = Path("/managed/root")
        with (
            mock.patch.object(
                bootstrap, "_windows_checkout_policy_required", return_value=False,
            ),
            mock.patch.object(
                bootstrap, "_git",
                side_effect=AssertionError("must not touch git config off Windows"),
            ),
        ):
            bootstrap._pin_windows_checkout_policy(repo)  # returns early; no _git call


if __name__ == "__main__":
    unittest.main()
