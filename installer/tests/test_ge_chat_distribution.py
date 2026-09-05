"""Release-integrity gates for the server-backed GE popup chat client."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_REQUIRED_BUILD_ROOT_INPUTS = (
    "pyproject.toml",
    "README.md",
    "LICENSE.md",
)
_KNOWN_TEST_CREDENTIALS = (
    b"ge-runtime-credential-7f29c1",
    b"synthetic-device-token",
    b"synthetic-desktop-token",
    b"synthetic-secret-value",
)
_GENERIC_TEST_CREDENTIAL = re.compile(
    rb"(?i)(?:synthetic|test)[-_][a-z0-9_-]{0,48}(?:token|credential|secret)|"
    rb"(?:token|credential|secret)[-_][a-z0-9_-]{0,48}(?:synthetic|test)"
)


def _archive_members(path: Path) -> list[tuple[str, bytes]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return [
                (name, archive.read(name))
                for name in archive.namelist()
                if not name.endswith("/")
            ]
    with tarfile.open(path, "r:gz") as archive:
        return [
            (member.name, archive.extractfile(member).read())
            for member in archive.getmembers()
            if member.isfile()
        ]


def _normalized_archive_name(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return None
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _optional_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


class GeChatDistributionContractTests(unittest.TestCase):
    def test_runtime_distribution_excludes_packaged_test_credentials(self):
        uv = shutil.which("uv")
        git = shutil.which("git")
        self.assertIsNotNone(
            uv, "distribution integrity tests require the project build tool: uv"
        )
        self.assertIsNotNone(
            git, "distribution integrity tests require a tracked source inventory"
        )
        tracked = subprocess.run(
            [git, "-C", str(_ROOT), "ls-files", "-z"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(tracked.returncode, 0, "tracked source inventory failed")
        tracked_names = tuple(
            name.decode("utf-8") for name in tracked.stdout.split(b"\0") if name
        )
        for filename in _REQUIRED_BUILD_ROOT_INPUTS:
            self.assertIn(filename, tracked_names)
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            source = temp_root / "source"
            source.mkdir()
            checked_out = subprocess.run(
                [
                    git,
                    "-C",
                    str(_ROOT),
                    "checkout-index",
                    "--all",
                    "--prefix=" + source.resolve().as_posix() + "/",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
            self.assertEqual(
                checked_out.returncode,
                0,
                "failed to materialize the exact staged source tree",
            )
            config = tomllib.loads(
                (source / "pyproject.toml").read_text(encoding="utf-8")
            )
            version = config["project"]["version"]
            package_find = config["tool"]["setuptools"]["packages"]["find"]
            self.assertEqual(package_find.get("include"), ["client*"])
            self.assertEqual(
                package_find.get("exclude"),
                ["client*.tests", "client*.tests.*"],
                "runtime wheels must not ship popup test fixtures or their synthetic credentials",
            )
            expected_runtime = {
                name: (source / Path(*name.split("/"))).read_bytes()
                for name in tracked_names
                if name.startswith("client/")
                and name.endswith(".py")
                and "/tests/" not in name.lower()
                and not Path(name).name.lower().startswith("test_")
            }
            self.assertIn("client/popup/native_shell.py", expected_runtime)
            dist = temp_root / "dist"
            root_uv_lock = _ROOT / "uv.lock"
            uv_lock_before = _optional_bytes(root_uv_lock)
            completed = subprocess.run(
                [
                    uv,
                    "build",
                    "--offline",
                    "--no-sources",
                    "--no-create-gitignore",
                    "--wheel",
                    "--sdist",
                    "--out-dir",
                    str(dist),
                    str(source),
                ],
                cwd=temp_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
            self.assertEqual(
                completed.returncode, 0, "offline clean-copy uv build failed"
            )
            self.assertTrue(
                _optional_bytes(root_uv_lock) == uv_lock_before,
                "clean-copy build mutated the worktree uv.lock",
            )
            distribution_stem = f"decision_engine_client-{version}"
            wheels = sorted(dist.glob(distribution_stem + "-*.whl"))
            sdists = sorted(dist.glob(distribution_stem + ".tar.gz"))
            self.assertEqual(len(wheels), 1, "build must emit exactly one wheel")
            self.assertEqual(len(sdists), 1, "build must emit exactly one sdist")
            roundtrip_dist = temp_root / "roundtrip-dist"
            roundtrip = subprocess.run(
                [
                    uv,
                    "build",
                    "--offline",
                    "--no-sources",
                    "--no-create-gitignore",
                    "--wheel",
                    "--out-dir",
                    str(roundtrip_dist),
                    str(sdists[0]),
                ],
                cwd=temp_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
            self.assertEqual(
                roundtrip.returncode, 0, "offline sdist-to-wheel build failed"
            )
            roundtrip_wheels = sorted(roundtrip_dist.glob(distribution_stem + "-*.whl"))
            self.assertEqual(
                len(roundtrip_wheels),
                1,
                "sdist round-trip must emit exactly one wheel",
            )
            artifacts = [wheels[0], sdists[0], roundtrip_wheels[0]]
            wheel_contracts: list[dict[str, bytes]] = []

            source_native = (
                source / "client" / "popup" / "native_shell.py"
            ).read_bytes()
            authored_paths = (
                str(_ROOT).encode(),
                str(_ROOT).replace("\\", "/").encode(),
                str(temp_root).encode(),
                str(temp_root).replace("\\", "/").encode(),
            )
            for artifact in artifacts:
                with self.subTest(artifact=artifact.name):
                    members = _archive_members(artifact)
                    normalized_members: list[tuple[str, bytes]] = []
                    for name, payload in members:
                        normalized = _normalized_archive_name(name)
                        self.assertIsNotNone(
                            normalized,
                            artifact.name + " contains an unsafe archive member path",
                        )
                        normalized_members.append((normalized, payload))
                    normalized_names = [name for name, _payload in normalized_members]
                    if artifact == sdists[0]:
                        for filename in _REQUIRED_BUILD_ROOT_INPUTS:
                            matches = [
                                payload
                                for name, payload in normalized_members
                                if name.endswith("/" + filename)
                            ]
                            self.assertEqual(
                                len(matches),
                                1,
                                "sdist omitted a tracked packaging root input",
                            )
                            self.assertEqual(
                                matches[0],
                                (source / filename).read_bytes(),
                                "sdist packaging root input differs from tracked reviewed source",
                            )
                    else:
                        self.assertEqual(
                            sum(
                                name.endswith(".dist-info/licenses/LICENSE.md")
                                for name in normalized_names
                            ),
                            1,
                            "wheel omitted the tracked license input",
                        )
                        wheel_contract: dict[str, bytes] = {}
                        for filename in ("METADATA", "entry_points.txt"):
                            matches = [
                                payload
                                for name, payload in normalized_members
                                if name.endswith(".dist-info/" + filename)
                            ]
                            self.assertEqual(
                                len(matches),
                                1,
                                "wheel omitted required distribution metadata",
                            )
                            wheel_contract[filename] = matches[0]
                        wheel_contracts.append(wheel_contract)
                    self.assertFalse(
                        [
                            name
                            for name in normalized_names
                            if "/tests/" in name.lower()
                            or Path(name).name.lower().startswith("test_")
                        ]
                    )
                    for _name, payload in normalized_members:
                        for forbidden in (*_KNOWN_TEST_CREDENTIALS, *authored_paths):
                            self.assertFalse(
                                forbidden in payload,
                                artifact.name + " contains forbidden release bytes",
                            )
                        self.assertFalse(
                            _GENERIC_TEST_CREDENTIAL.search(payload) is not None,
                            artifact.name + " contains a generic test credential",
                        )
                    packaged_runtime: dict[str, bytes] = {}
                    for name, payload in normalized_members:
                        client_index = name.find("client/")
                        if client_index < 0:
                            continue
                        runtime_name = name[client_index:]
                        if not runtime_name.endswith(".py"):
                            continue
                        self.assertNotIn(
                            runtime_name,
                            packaged_runtime,
                            artifact.name + " contains a duplicate runtime module",
                        )
                        packaged_runtime[runtime_name] = payload
                    self.assertEqual(
                        set(packaged_runtime),
                        set(expected_runtime),
                        artifact.name
                        + " runtime module manifest differs from tracked reviewed source",
                    )
                    for runtime_name, source_payload in expected_runtime.items():
                        self.assertTrue(
                            packaged_runtime[runtime_name] == source_payload,
                            artifact.name
                            + " runtime module differs from tracked reviewed source: "
                            + runtime_name,
                        )
                    self.assertTrue(
                        packaged_runtime["client/popup/native_shell.py"]
                        == source_native,
                        "wheel and sdist must contain the exact reviewed native client",
                    )

            self.assertEqual(
                wheel_contracts[0],
                wheel_contracts[1],
                "direct and sdist-round-trip wheel metadata contracts differ",
            )

            for index, wheel in enumerate((wheels[0], roundtrip_wheels[0])):
                target = temp_root / f"target-{index}"
                installed = subprocess.run(
                    [
                        uv,
                        "pip",
                        "install",
                        "--offline",
                        "--no-deps",
                        "--target",
                        str(target),
                        str(wheel),
                    ],
                    cwd=temp_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    installed.returncode, 0, "offline wheel target-install failed"
                )
                import_smoke = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        (
                            "import pathlib,sys;"
                            "sys.path.insert(0,sys.argv[1]);"
                            "from client import windows_security;"
                            "from client.popup import native_shell,session;"
                            "root=pathlib.Path(sys.argv[1]).resolve();"
                            "module=pathlib.Path(native_shell.__file__).resolve();"
                            "assert module.is_relative_to(root);"
                            "assert native_shell.PopupApi.__module__=='client.popup.native_shell';"
                            "assert session.windows_security is windows_security;"
                            "assert callable(windows_security.validate_private_data_acl)"
                        ),
                        str(target),
                    ],
                    cwd=temp_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    import_smoke.returncode,
                    0,
                    "installed native_shell import smoke failed",
                )


if __name__ == "__main__":
    unittest.main()
