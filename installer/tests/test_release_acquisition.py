"""Behavior locks for bounded, signed GitHub/Gitee release discovery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from installer import managed_install, release_acquisition, release_contract, updater


class ReleaseAcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = release_contract.ReleaseManifest(
            schema=1,
            repository_id="deeppatternai/decision-engine",
            channel="stable",
            release_sequence=23,
            version="0.2.3",
            tag="v0.2.3",
            commit="2" * 40,
            min_python="3.12",
            published_at="2026-07-22T00:00:00Z",
            key_id="test-key",
        )
        self.signature = release_contract.ReleaseSignature(
            schema=1,
            algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="test-key",
            signature="AA==",
        )
        self.state = updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=22,
            last_release_commit="1" * 40,
            last_manifest_sha256="3" * 64,
            last_version="0.2.2",
        )
        self.keys = {"test-key": mock.sentinel.public_key}

    def _document(self, kind: str) -> bytes:
        payload = self.manifest if kind == "manifest" else self.signature
        return json.dumps(asdict(payload)).encode("utf-8")

    def test_network_failure_falls_back_to_gitee_under_one_absolute_deadline(self):
        calls = []
        deadline = time.monotonic() + 10.0

        def fetch(source, kind, *, deadline):
            calls.append((source.name, kind, deadline))
            if source.name == "github":
                raise release_acquisition.ReleaseTransportError("network unavailable")
            return self._document(kind)

        with mock.patch.object(
            release_contract,
            "authorize_release",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ) as authorize:
            acquired = release_acquisition.discover_release(
                self.state,
                self.keys,
                deadline=deadline,
                fetch_document=fetch,
            )

        self.assertEqual(acquired.source.name, "gitee")
        self.assertEqual(acquired.manifest, self.manifest)
        self.assertFalse(acquired.uses_git_credentials)
        self.assertEqual(
            calls,
            [
                ("github", "manifest", deadline),
                ("gitee", "manifest", deadline),
                ("gitee", "signature", deadline),
            ],
        )
        authorize.assert_called_once()

    def _assert_tag_fetch_transport(self, *, uses_git_credentials: bool):
        acquired = release_acquisition.AcquiredRelease(
            release_acquisition.GITHUB_SOURCE,
            self.manifest,
            self.signature,
            uses_git_credentials=uses_git_credentials,
        )
        reader = mock.Mock()
        reader.git_executable = "/trusted/git"
        reader.environment = {"transport": "hardened"}
        commit_output = (0, (self.manifest.commit + "\n").encode("ascii"))
        reader.run.side_effect = lambda operation, **_values: (
            (0, b"") if operation == "unsafe_config" else commit_output
        )
        identity = mock.Mock()
        identity.canonical_root = Path("/managed/root")
        identity.remotes = dict(managed_install.OFFICIAL_REMOTE_URLS)
        ambient_environment = {"transport": "ambient-credentials"}

        with (
            mock.patch.object(
                release_acquisition.updater, "_GitReader", return_value=reader
            ),
            mock.patch.object(
                release_acquisition.updater,
                "_read_remotes",
                return_value=dict(managed_install.OFFICIAL_REMOTE_URLS),
            ),
            mock.patch.object(
                release_acquisition.managed_install,
                "validate_managed_identity",
                return_value=identity,
            ),
            mock.patch.object(
                release_acquisition.updater,
                "_ambient_git_environment",
                return_value=ambient_environment,
            ) as ambient,
            mock.patch.object(
                release_acquisition.updater,
                "_run_bounded_git",
                return_value=(0, b""),
            ) as run,
        ):
            release_acquisition.fetch_release_objects(
                Path("/managed/root"), acquired, deadline=1e18
            )

        argv, environment = run.call_args.args[:2]
        self.assertEqual(
            environment,
            ambient_environment if uses_git_credentials else reader.environment,
        )
        self.assertEqual(
            "credential.helper=" in argv,
            not uses_git_credentials,
        )
        self.assertIn("core.hooksPath=" + os.devnull, argv)
        if uses_git_credentials:
            ambient.assert_called_once_with(reader.git_executable)
        else:
            ambient.assert_not_called()

    def test_authenticated_git_release_uses_credentials_for_exact_tag_fetch(self):
        self._assert_tag_fetch_transport(uses_git_credentials=True)

    def test_anonymous_release_keeps_exact_tag_fetch_hardened(self):
        self._assert_tag_fetch_transport(uses_git_credentials=False)

    def test_tag_fetch_refuses_promisor_config_before_any_fetch(self):
        acquired = release_acquisition.AcquiredRelease(
            release_acquisition.GITHUB_SOURCE,
            self.manifest,
            self.signature,
            uses_git_credentials=False,
        )
        reader = mock.Mock()
        reader.run.return_value = (0, b"extensions.partialclone\x00")
        with (
            mock.patch.object(
                release_acquisition.updater, "_GitReader", return_value=reader
            ),
            mock.patch.object(
                release_acquisition.updater, "_run_bounded_git"
            ) as run,
        ):
            with self.assertRaisesRegex(
                release_acquisition.updater.UpdateInspectionError,
                "execution-capable",
            ):
                release_acquisition.fetch_release_objects(
                    Path("/managed/root"), acquired, deadline=1e18
                )
        run.assert_not_called()

    def test_tag_fetch_rejects_non_boolean_credential_provenance(self):
        acquired = release_acquisition.AcquiredRelease(
            release_acquisition.GITHUB_SOURCE,
            self.manifest,
            self.signature,
            uses_git_credentials=1,
        )
        with mock.patch.object(
            release_acquisition.updater, "_GitReader"
        ) as reader:
            with self.assertRaisesRegex(
                release_acquisition.ReleaseAcquisitionError,
                "source is invalid",
            ):
                release_acquisition.fetch_release_objects(
                    Path("/managed/root"), acquired, deadline=1e18
                )
        reader.assert_not_called()

    def test_malformed_or_untrusted_github_document_never_falls_back(self):
        calls = []
        deadline = time.monotonic() + 10.0

        def fetch(source, kind, *, deadline):
            calls.append((source.name, kind))
            return b"{}"

        with self.assertRaises(release_contract.ReleaseContractError):
            release_acquisition.discover_release(
                self.state,
                self.keys,
                deadline=deadline,
                fetch_document=fetch,
            )

        self.assertEqual(calls, [("github", "manifest")])

    def test_expired_shared_budget_stops_before_any_transport(self):
        fetch = mock.Mock()
        with mock.patch.object(release_acquisition.time, "monotonic", return_value=10.1):
            with self.assertRaisesRegex(
                release_acquisition.ReleaseBudgetExpired, "startup budget"
            ):
                release_acquisition.discover_release(
                    self.state,
                    self.keys,
                    deadline=10.0,
                    fetch_document=fetch,
                )
        fetch.assert_not_called()

    def test_empty_trust_store_refuses_without_network(self):
        fetch = mock.Mock()
        deadline = time.monotonic() + 10.0
        with self.assertRaisesRegex(
            release_acquisition.ReleaseTrustUnavailable, "trusted release key"
        ):
            release_acquisition.discover_release(
                self.state,
                {},
                deadline=deadline,
                fetch_document=fetch,
            )
        fetch.assert_not_called()

    def test_dirty_release_trust_store_is_never_used_as_authority(self):
        with self.subTest("worktree differs from HEAD"):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / release_acquisition.TRUST_STORE_RELATIVE_PATH
                path.parent.mkdir(parents=True)
                worktree = {
                    "schema": 1,
                    "keys": [
                        {
                            "key_id": "local-key",
                            "algorithm": release_contract.RSA_SHA256_ALGORITHM,
                            "modulus_hex": "f" * 512,
                            "exponent": 65537,
                            "revoked": False,
                        }
                    ],
                }
                tracked = {"schema": 1, "keys": []}
                path.write_text(json.dumps(worktree), encoding="utf-8")
                reader = mock.Mock()
                reader.run.return_value = (0, json.dumps(tracked).encode("utf-8"))
                with mock.patch.object(
                    release_acquisition.updater, "_GitReader", return_value=reader
                ):
                    with self.assertRaisesRegex(
                        release_acquisition.ReleaseTrustUnavailable, "differs"
                    ):
                        release_acquisition.load_trusted_release_keys(
                            root, expected_commit="1" * 40
                        )

    def test_trust_store_loader_uses_legacy_local_git_reader_mode(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / release_acquisition.TRUST_STORE_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            trust_store = {
                "schema": 1,
                "keys": [
                    {
                        "key_id": "local-key",
                        "algorithm": release_contract.RSA_SHA256_ALGORITHM,
                        "modulus_hex": "f" * 512,
                        "exponent": 65537,
                        "revoked": False,
                    }
                ],
            }
            payload = json.dumps(trust_store).encode("utf-8")
            path.write_bytes(payload)
            reader = mock.Mock()
            reader.run.side_effect = [
                (0, (b"1" * 40) + b"\n"),
                (0, payload),
            ]
            with mock.patch.object(
                release_acquisition.updater, "_GitReader", return_value=reader
            ) as git_reader:
                trusted = release_acquisition.load_trusted_release_keys(root)

        self.assertEqual(tuple(trusted), ("local-key",))
        git_reader.assert_called_once_with(
            root,
            deadline=None,
            trust_store_only=True,
        )

    @unittest.skipUnless(
        __import__("shutil").which("git"), "git is required for this fixture"
    )
    def test_repository_trust_store_admits_only_the_current_stable_key(self):
        source_root = Path(__file__).resolve().parents[2]
        source_path = source_root / release_acquisition.TRUST_STORE_RELATIVE_PATH
        payload = source_path.read_bytes()
        trust_store = json.loads(payload.decode("utf-8"))
        entries = {item["key_id"]: item for item in trust_store["keys"]}

        self.assertEqual(
            set(entries), {"test-stable-2026-07", "stable-2026-08"}
        )
        self.assertIs(entries["test-stable-2026-07"]["revoked"], True)
        self.assertIs(entries["stable-2026-08"]["revoked"], False)

        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp)
            root = fixture_root / "repository"
            path = root / release_acquisition.TRUST_STORE_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            path.write_bytes(payload)
            template = _empty_git_template(fixture_root)
            environment = _isolated_git_environment(fixture_root)
            subprocess.run(
                [
                    "git",
                    "init",
                    "-q",
                    "-b",
                    "stable",
                    "--template",
                    str(template),
                    str(root),
                ],
                check=True,
                env=environment,
            )
            _run_git(
                ["config", "user.email", "fixture@example.test"],
                root,
                env=environment,
            )
            _run_git(
                ["config", "user.name", "Fixture"], root, env=environment
            )
            _run_git(
                ["add", "installer/release-trust.json"],
                root,
                env=environment,
            )
            _run_git(
                ["commit", "-q", "-m", "trust store"],
                root,
                env=environment,
            )
            commit = _run_git(["rev-parse", "HEAD"], root, env=environment)
            trusted = release_acquisition.load_trusted_release_keys(
                root, expected_commit=commit
            )

        self.assertEqual(tuple(trusted), ("stable-2026-08",))
        self.assertEqual(
            hashlib.sha256(
                format(trusted["stable-2026-08"].modulus, "x").encode("ascii")
            ).hexdigest(),
            "e91d472ef8853c0a76dab741c0318a08"
            "e4deee7d71f89f79d5620429984d3b82",
        )
        self.assertEqual(trusted["stable-2026-08"].exponent, 65537)

        current_manifest = release_contract.parse_release_manifest(
            {
                "channel": "stable",
                "commit": (
                    "90cd36975573f97ca7e3"
                    "e72923d21488f65d2dff"
                ),
                "key_id": "stable-2026-08",
                "min_python": "3.12",
                "published_at": "2026-10-21T07:15:23Z",
                "release_sequence": 102,
                "repository_id": "deeppatternai/decision-engine",
                "schema": 1,
                "tag": "v0.2.107",
                "version": "0.2.107",
            }
        )
        current_signature = release_contract.parse_release_signature(
            {
                "algorithm": release_contract.RSA_SHA256_ALGORITHM,
                "key_id": "stable-2026-08",
                "schema": 1,
                "signature": (
                    "hupp3Y9M2IhmDkPKrSqYD2/S4Yr+mh3QC8xHDGJNxTG1DBFuNCY35z8HdBYpCNwn"
                    "UZi3mLstkcIgA+YTGk6pFAJTW7r7HF+svxZH+d6cx8Vas43yMN05Gx9FD3IcJzal"
                    "jl83WkO43m/xoV6R9MUM6fuCkqSkjV/KYs/3ceZaix+uHSrdxpNDlLd5B0UMh5Dvh"
                    "wDhIO/qbEP3Qof1/a4IfGHKuLqK6T2iB0aczPeF0U1OQyw/xdKd81CjJYV6EAq4m"
                    "0uRFMjGlwv58wi2eZltmxunNJs6uakFdCz9qpZWgPGr3EePPNv6mVsoCxXJb4om86"
                    "Fw+DxEi4PXwAgEHgUZWSolGzL+NnJtAcdG7CMoTyc95Hjj9+zxGUzuFYdeB628/vy1"
                    "cz25y6D0QoIkc8v6979RLG63GnmsIGDXxDIbvvj7bOx/1MEmHIj1o/Arpi+dKjxyN"
                    "XZz5SbmxSa3CDG9e/qq+07eJtk49sZsamRalHerM73snsTunizUPomhMULF"
                ),
            }
        )
        verified = release_contract.verify_release_signature(
            current_manifest,
            current_signature,
            trusted,
            expected_repository_id=managed_install.REPOSITORY_ID,
            expected_channel=managed_install.CHANNEL,
        )
        self.assertEqual(verified.key_id, "stable-2026-08")

        legacy_manifest = release_contract.parse_release_manifest(
            {
                "channel": "stable",
                "commit": (
                    "d746b09d2f904d7fe60a"
                    "cd4ec94b2ce685d86540"
                ),
                "key_id": "test-stable-2026-07",
                "min_python": "3.12",
                "published_at": "2026-08-18T02:54:27Z",
                "release_sequence": 49,
                "repository_id": "deeppatternai/decision-engine",
                "schema": 1,
                "tag": "v0.2.50",
                "version": "0.2.50",
            }
        )
        legacy_signature = release_contract.parse_release_signature(
            {
                "algorithm": release_contract.RSA_SHA256_ALGORITHM,
                "key_id": "test-stable-2026-07",
                "schema": 1,
                "signature": (
                    "iNvlOUYtxLfpFNk25MsWoHOzk4jSx1GjYE0strUrGxlRWqwJksyw5lswtCroXrtC38"
                    "vMaE1WaVCyFi+Z5WTf98l8BAPl1j8NPeslgCbPO+i7apSet0weaPg3K1rdzGMZjOT"
                    "jBJt5f9N8e4+PE6j5TAV36pNEGnuL8FVXwbM53DiF2ylcvUcFzhYIgwXFV201KIaw"
                    "2WOu1IrUx6AeZ8Hf8nfRA/z8/mzhlH9UMYAnqey+EAKYaQwTeqn2DWfi1yx2HoJ7V"
                    "6bo4XZ7gNYwiE+CrxrbnlksQ+2Uu7x0J5Nf3JO0x2g8Uaae2uPa0vBkxHt3T4S9zq"
                    "70sekTH7UbxcMLV1CHGJ9DZh//FJpqFL3HQpJPSnBhVhdNiW62VkZtPRt6f+dPJlKj"
                    "iND0JwiCKI1psHdZXxgzhRhzE9ZoPoSCjSmz0MHLi4fX8K1ushryabTMfpCju1jOZJ"
                    "6OPhaskJFvN6Ztxb7OLOCevpmXPu+Yyc5fz2zGAFKiVIT4/tsmv60j"
                ),
            }
        )
        legacy_entry = entries["test-stable-2026-07"]
        legacy_key = release_contract.RsaPublicKey(
            "test-stable-2026-07",
            int(legacy_entry["modulus_hex"], 16),
            legacy_entry["exponent"],
        )
        release_contract.verify_release_signature(
            legacy_manifest,
            legacy_signature,
            {legacy_key.key_id: legacy_key},
            expected_repository_id=managed_install.REPOSITORY_ID,
            expected_channel=managed_install.CHANNEL,
        )
        with self.assertRaisesRegex(
            release_contract.ReleaseContractError, "signing key is not trusted"
        ):
            release_contract.verify_release_signature(
                legacy_manifest,
                legacy_signature,
                trusted,
                expected_repository_id=managed_install.REPOSITORY_ID,
                expected_channel=managed_install.CHANNEL,
            )

    def test_fetch_child_does_not_inherit_custom_tls_trust_anchors(self):
        process = mock.Mock()
        process.communicate.return_value = (b"{}", b"")
        process.returncode = 0
        deadline = time.monotonic() + 10.0
        with (
            mock.patch.dict(
                os.environ,
                {"SSL_CERT_FILE": "malicious.pem", "SSL_CERT_DIR": "malicious-dir"},
            ),
            mock.patch.object(
                release_acquisition.subprocess, "Popen", return_value=process
            ) as popen,
        ):
            self.assertEqual(
                release_acquisition.fetch_document(
                    release_acquisition.GITHUB_SOURCE,
                    "manifest",
                    deadline=deadline,
                ),
                b"{}",
            )

        child_env = popen.call_args.kwargs["env"]
        self.assertNotIn("SSL_CERT_FILE", child_env)
        self.assertNotIn("SSL_CERT_DIR", child_env)
        self.assertIn("SameHostHttpsRedirect", popen.call_args.args[0][4])


def _empty_git_template(root: Path) -> Path:
    template = root / ".git-fixture-template"
    template.mkdir(exist_ok=True)
    return template


def _isolated_git_environment(root: Path) -> dict[str, str]:
    home = root / ".git-fixture-home"
    hooks = root / ".git-fixture-hooks"
    home.mkdir(exist_ok=True)
    hooks.mkdir(exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "commit.gpgsign",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "tag.gpgsign",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "core.hooksPath",
            "GIT_CONFIG_VALUE_2": str(hooks),
        }
    )
    return environment


def _run_git(args, cwd, *, env=None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env or {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.decode("utf-8", "replace").strip()


@unittest.skipUnless(
    __import__("shutil").which("git"), "git is required for this fixture"
)
class DiscoverInitialReleaseViaGitTests(unittest.TestCase):
    """The one-time, human-triggered bootstrap/activation fallback for a still-
    private repository — reads the tracked release/manifest.json + .sig.json
    via authenticated `git show` instead of anonymous HTTPS. Only
    bootstrap_managed_install / managed_activation reach *this* function (no
    anti-rollback prior); see DiscoverReleaseViaGitTests below for the
    separate, ongoing per-MCP-start counterpart installer.launcher uses."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", "-b", "stable", str(self.root)], check=True, env=env)
        _run_git(["config", "user.email", "fixture@example.test"], self.root)
        _run_git(["config", "user.name", "Fixture"], self.root)
        self.manifest = release_contract.ReleaseManifest(
            schema=1, repository_id=managed_install.REPOSITORY_ID, channel="stable",
            release_sequence=1, version="0.9.9", tag="v0.9.9", commit="9" * 40,
            min_python="3.12", published_at="2026-01-01T00:00:00Z", key_id="test-key",
        )
        self.signature = release_contract.ReleaseSignature(
            schema=1, algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="test-key", signature="AA==",
        )
        release_dir = self.root / "release"
        release_dir.mkdir()
        (release_dir / "manifest.json").write_text(
            json.dumps(asdict(self.manifest)), encoding="utf-8",
        )
        (release_dir / "manifest.sig.json").write_text(
            json.dumps(asdict(self.signature)), encoding="utf-8",
        )
        _run_git(["add", "-A"], self.root)
        _run_git(["commit", "-q", "-m", "release"], self.root)
        commit = _run_git(["rev-parse", "HEAD"], self.root)
        _run_git(["update-ref", "refs/remotes/github/stable", commit], self.root)
        self.keys = {"test-key": mock.sentinel.public_key}

    def test_reads_tracked_manifest_via_authenticated_git_show(self):
        with mock.patch.object(
            release_contract, "verify_release_signature",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ) as verify:
            acquired = release_acquisition.discover_initial_release_via_git(
                self.root, ("github",), self.keys,
            )
        self.assertEqual(acquired.manifest, self.manifest)
        self.assertEqual(acquired.source.name, "github")
        # Problem-2 regression: the git fallback must return a CANONICAL source
        # (GITHUB_SOURCE / GITEE_SOURCE) — the exact instances fetch_release_objects
        # gates on via `source in RELEASE_SOURCES`. A synthetic ReleaseSource here
        # was rejected with "authorized release source is invalid" on private repos.
        self.assertIn(acquired.source, release_acquisition.RELEASE_SOURCES)
        verify.assert_called_once()

    def test_uses_ambient_not_hardened_git_environment(self):
        # Regression guard: updater._git_environment strips HOME and points
        # GIT_CONFIG_GLOBAL at os.devnull, which makes a real HTTPS
        # credential helper / SSH agent unresolvable (confirmed live against
        # github.com during development — a private-repo clone failed with
        # "could not read Username for ... terminal prompts disabled"). This
        # function's whole purpose is authenticated access, so it must use
        # the ambient variant, never the hardened one.
        with mock.patch.object(
            release_contract, "verify_release_signature",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ):
            with (
                mock.patch.object(
                    release_acquisition.updater, "_ambient_git_environment",
                    wraps=release_acquisition.updater._ambient_git_environment,
                ) as ambient,
                mock.patch.object(
                    release_acquisition.updater, "_git_environment"
                ) as hardened,
            ):
                release_acquisition.discover_initial_release_via_git(
                    self.root, ("github",), self.keys,
                )
        ambient.assert_called_once()
        hardened.assert_not_called()

    def test_missing_remote_ref_raises_transport_error_not_silently_none(self):
        with self.assertRaises(release_acquisition.ReleaseTransportError):
            release_acquisition.discover_initial_release_via_git(
                self.root, ("no-such-remote",), self.keys,
            )

    def test_tries_remotes_in_order_and_uses_first_with_a_readable_ref(self):
        with mock.patch.object(
            release_contract, "verify_release_signature",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ):
            acquired = release_acquisition.discover_initial_release_via_git(
                self.root, ("no-such-remote", "github"), self.keys,
            )
        self.assertEqual(acquired.source.name, "github")
        # Problem-2 regression: the git fallback must return a CANONICAL source
        # (GITHUB_SOURCE / GITEE_SOURCE) — the exact instances fetch_release_objects
        # gates on via `source in RELEASE_SOURCES`. A synthetic ReleaseSource here
        # was rejected with "authorized release source is invalid" on private repos.
        self.assertIn(acquired.source, release_acquisition.RELEASE_SOURCES)

    def test_signature_failure_propagates_without_retrying_other_remotes(self):
        # A ReleaseContractError (not a ReleaseTransportError) must hard-stop —
        # silently trying another remote after a signature failure would let a
        # transport-layer retry loop mask a real security failure.
        with mock.patch.object(
            release_contract, "verify_release_signature",
            side_effect=release_contract.ReleaseContractError("bad signature"),
        ) as verify:
            with self.assertRaises(release_contract.ReleaseContractError):
                release_acquisition.discover_initial_release_via_git(
                    self.root, ("github", "github"), self.keys,
                )
        verify.assert_called_once()

    def test_no_trusted_keys_refuses_without_touching_git(self):
        with mock.patch.object(release_acquisition.updater, "_resolve_git_executable") as resolve:
            with self.assertRaisesRegex(
                release_acquisition.ReleaseTrustUnavailable, "trusted release key"
            ):
                release_acquisition.discover_initial_release_via_git(
                    self.root, ("github",), {},
                )
        resolve.assert_not_called()


@unittest.skipUnless(
    __import__("shutil").which("git"), "git is required for this fixture"
)
class DiscoverReleaseViaGitTests(unittest.TestCase):
    """The ongoing, unattended per-MCP-start fallback installer.launcher uses
    — the knowingly accepted private-beta-only exception to "never touch
    personal git credentials in the background" (see the function's own
    docstring). Fetches `stable` fresh from an actual configured remote (a
    local-path remote here) each call, then reads the tracked release files
    the same way DiscoverInitialReleaseViaGitTests does, but authorizes with
    the anti-rollback prior state real ongoing updates carry."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}

        self.upstream = Path(self.tmp.name) / "upstream"
        subprocess.run(["git", "init", "-q", "-b", "stable", str(self.upstream)], check=True, env=env)
        _run_git(["config", "user.email", "fixture@example.test"], self.upstream)
        _run_git(["config", "user.name", "Fixture"], self.upstream)
        self.manifest = release_contract.ReleaseManifest(
            schema=1, repository_id=managed_install.REPOSITORY_ID, channel="stable",
            release_sequence=2, version="0.9.10", tag="v0.9.10", commit="8" * 40,
            min_python="3.12", published_at="2026-01-02T00:00:00Z", key_id="test-key",
        )
        self.signature = release_contract.ReleaseSignature(
            schema=1, algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="test-key", signature="AA==",
        )
        release_dir = self.upstream / "release"
        release_dir.mkdir()
        (release_dir / "manifest.json").write_text(
            json.dumps(asdict(self.manifest)), encoding="utf-8",
        )
        (release_dir / "manifest.sig.json").write_text(
            json.dumps(asdict(self.signature)), encoding="utf-8",
        )
        _run_git(["add", "-A"], self.upstream)
        _run_git(["commit", "-q", "-m", "release"], self.upstream)

        self.root = Path(self.tmp.name) / "root"
        subprocess.run(["git", "init", "-q", "-b", "stable", str(self.root)], check=True, env=env)
        _run_git(["config", "user.email", "fixture@example.test"], self.root)
        _run_git(["config", "user.name", "Fixture"], self.root)
        _run_git(["remote", "add", "github", str(self.upstream)], self.root)

        self.keys = {"test-key": mock.sentinel.public_key}
        # An older prior state — the new manifest's higher sequence must pass
        # the anti-rollback check inside authorize_release.
        self.state = updater.UpdateState(
            schema=1,
            channel="stable",
            last_release_sequence=1,
            last_release_commit="7" * 40,
            last_manifest_sha256="0" * 64,
            last_version="0.9.9",
        )
        self.deadline = time.monotonic() + 30.0

    def test_fetches_and_reads_tracked_manifest_via_authenticated_git(self):
        with mock.patch.object(
            release_contract, "authorize_release",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ) as authorize:
            acquired = release_acquisition.discover_release_via_git(
                self.root, ("github",), self.state, self.keys, deadline=self.deadline,
            )
        self.assertEqual(acquired.manifest, self.manifest)
        self.assertEqual(acquired.source.name, "github")
        self.assertTrue(acquired.uses_git_credentials)
        # Problem-2 regression: the git fallback must return a CANONICAL source
        # (GITHUB_SOURCE / GITEE_SOURCE) — the exact instances fetch_release_objects
        # gates on via `source in RELEASE_SOURCES`. A synthetic ReleaseSource here
        # was rejected with "authorized release source is invalid" on private repos.
        self.assertIn(acquired.source, release_acquisition.RELEASE_SOURCES)
        authorize.assert_called_once()
        self.assertEqual(
            _run_git(["rev-parse", "--verify", "refs/remotes/github/stable"], self.root),
            _run_git(["rev-parse", "HEAD"], self.upstream),
        )

    def test_uses_ambient_not_hardened_git_environment(self):
        # Same regression guard as DiscoverInitialReleaseViaGitTests above —
        # this function additionally does a real `git fetch` (not just
        # `git show` of already-local objects), which is exactly the
        # operation that failed live against github.com under the hardened
        # environment.
        with mock.patch.object(
            release_contract, "authorize_release",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ):
            with (
                mock.patch.object(
                    release_acquisition.updater, "_ambient_git_environment",
                    wraps=release_acquisition.updater._ambient_git_environment,
                ) as ambient,
                mock.patch.object(
                    release_acquisition.updater, "_git_environment"
                ) as hardened,
            ):
                release_acquisition.discover_release_via_git(
                    self.root, ("github",), self.state, self.keys, deadline=self.deadline,
                )
        ambient.assert_called_once()
        hardened.assert_not_called()

    def test_missing_remote_raises_transport_error(self):
        with self.assertRaises(release_acquisition.ReleaseTransportError):
            release_acquisition.discover_release_via_git(
                self.root, ("no-such-remote",), self.state, self.keys, deadline=self.deadline,
            )

    def test_tries_remotes_in_order_and_uses_first_that_fetches(self):
        with mock.patch.object(
            release_contract, "authorize_release",
            return_value=release_contract.VerifiedRelease(self.manifest, "test-key"),
        ):
            acquired = release_acquisition.discover_release_via_git(
                self.root, ("no-such-remote", "github"), self.state, self.keys,
                deadline=self.deadline,
            )
        self.assertEqual(acquired.source.name, "github")
        # Problem-2 regression: the git fallback must return a CANONICAL source
        # (GITHUB_SOURCE / GITEE_SOURCE) — the exact instances fetch_release_objects
        # gates on via `source in RELEASE_SOURCES`. A synthetic ReleaseSource here
        # was rejected with "authorized release source is invalid" on private repos.
        self.assertIn(acquired.source, release_acquisition.RELEASE_SOURCES)

    def test_rollback_or_signature_failure_propagates_without_retrying(self):
        with mock.patch.object(
            release_contract, "authorize_release",
            side_effect=release_contract.ReleaseContractError("sequence rollback"),
        ) as authorize:
            with self.assertRaises(release_contract.ReleaseContractError):
                release_acquisition.discover_release_via_git(
                    self.root, ("github", "github"), self.state, self.keys,
                    deadline=self.deadline,
                )
        authorize.assert_called_once()

    def test_no_trusted_keys_refuses_without_touching_git(self):
        with mock.patch.object(release_acquisition.updater, "_resolve_git_executable") as resolve:
            with self.assertRaisesRegex(
                release_acquisition.ReleaseTrustUnavailable, "trusted release key"
            ):
                release_acquisition.discover_release_via_git(
                    self.root, ("github",), self.state, {}, deadline=self.deadline,
                )
        resolve.assert_not_called()


class CanonicalSourceTests(unittest.TestCase):
    """The git-fallback source-mapping helper behind the problem-2 fix: it must
    return the exact RELEASE_SOURCES singletons (so fetch_release_objects accepts
    them) and hard-fail an unofficial remote name rather than fabricating one."""

    def test_official_names_map_to_the_canonical_singletons(self):
        self.assertIs(
            release_acquisition._canonical_source("github"),
            release_acquisition.GITHUB_SOURCE,
        )
        self.assertIs(
            release_acquisition._canonical_source("gitee"),
            release_acquisition.GITEE_SOURCE,
        )
        # Both are members of the set fetch_release_objects gates on.
        self.assertIn(
            release_acquisition._canonical_source("github"),
            release_acquisition.RELEASE_SOURCES,
        )

    def test_unofficial_remote_name_is_a_hard_error_not_a_synthetic_source(self):
        with self.assertRaises(release_acquisition.ReleaseAcquisitionError):
            release_acquisition._canonical_source("origin")


if __name__ == "__main__":
    unittest.main()
