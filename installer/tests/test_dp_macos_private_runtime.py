"""Contract locks for the macOS private Python runtime bootstrap."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "dp-install.sh"


def shell_function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index(f"\n{next_name}() {{", start)
    return source[start:end]


class MacPrivateRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_runtime_release_and_both_architectures_are_pinned(self) -> None:
        self.assertIn('PRIVATE_PYTHON_VERSION="3.13.15"', self.source)
        self.assertIn('PRIVATE_PYTHON_BUILD="20260901"', self.source)
        self.assertIn('PRIVATE_PYTHON_RELEASE="20260901"', self.source)
        self.assertIn("astral-sh/python-build-standalone/releases/download", self.source)
        self.assertIn('PRIVATE_RUNTIME_ARCH="aarch64"', self.source)
        self.assertIn('PRIVATE_RUNTIME_ARCH="x86_64"', self.source)

        spec = shell_function(
            self.source,
            "select_private_runtime_spec",
            "private_runtime_is_usable",
        )
        mac_spec = spec.split('if [ "$PLATFORM_FAMILY" = "macos" ]; then', 1)[1].split(
            "\n  else\n", 1
        )[0]
        digests = re.findall(r'PRIVATE_RUNTIME_SHA256="([0-9a-f]{64})"', mac_spec)
        sizes = re.findall(r'PRIVATE_RUNTIME_ASSET_SIZE="([0-9]+)"', mac_spec)
        self.assertEqual(len(digests), 2)
        self.assertEqual(len(set(digests)), 2)
        self.assertEqual(len(sizes), 2)
        self.assertTrue(all(int(size) > 20_000_000 for size in sizes))

    def test_download_is_visible_https_only_and_verified_before_extract(self) -> None:
        body = shell_function(
            self.source,
            "download_private_python_runtime",
            "select_fallback_python",
        )
        curl = body.index(
            "/usr/bin/curl --fail --location --show-error --progress-bar"
        )
        protocol = body.index("--proto '=https' --tlsv1.2", curl)
        checksum = body.index("/usr/bin/openssl dgst -sha256", protocol)
        size_check = body.index('"$actual_size" != "$PRIVATE_RUNTIME_ASSET_SIZE"', checksum)
        inspect = body.index("/usr/bin/tar -tzf", size_check)
        extract = body.index("/usr/bin/tar -xzf", inspect)
        activate = body.index(
            'clean_exec /bin/mv "$stage/runtime" "$PRIVATE_RUNTIME_DIR"', extract
        )
        verify = body.index("private_runtime_is_usable", activate)

        self.assertLess(curl, protocol)
        self.assertLess(protocol, checksum)
        self.assertLess(checksum, size_check)
        self.assertLess(size_check, inspect)
        self.assertLess(inspect, extract)
        self.assertLess(extract, activate)
        self.assertLess(activate, verify)
        self.assertIn("--connect-timeout 20 --retry 2", body)
        self.assertIn('${PRIVATE_RUNTIME_ASSET/+/%2B}', self.source)

    def test_fresh_download_is_automatic_but_invalid_state_needs_consent(self) -> None:
        body = shell_function(
            self.source,
            "download_private_python_runtime",
            "select_fallback_python",
        )
        self.assertEqual(
            body.count("confirm_dependency_install"),
            1,
            "only replacement of an existing invalid runtime should prompt",
        )
        self.assertIn(
            "The Deep Pattern private Python runtime is incomplete or invalid.", body
        )
        self.assertIn(
            "Downloading $PRIVATE_RUNTIME_DOWNLOAD_LABEL into $PRIVATE_RUNTIME_ROOT without changing system Python.",
            body,
        )
        self.assertIn('PRIVATE_RUNTIME_DOWNLOAD_LABEL="about 25 MB"', self.source)

    def test_every_install_uses_the_managed_python_entrypoint(self) -> None:
        selection_start = self.source.index('PYTHON_BIN=""\nif try_python "$MANAGED_PYTHON_BIN"')
        selection_end = self.source.index('\nPYTHON_DIR="$(dirname "$PYTHON_BIN")"', selection_start)
        flow = self.source[selection_start:selection_end]

        reuse = flow.index('try_python "$MANAGED_PYTHON_BIN"')
        download = flow.index("download_private_python_runtime", reuse)
        fallback = flow.index("select_fallback_python", download)
        create = flow.index('create_managed_python_environment "$PRIVATE_BASE_PYTHON"', fallback)
        self.assertLess(reuse, download)
        self.assertLess(download, fallback)
        self.assertLess(fallback, create)
        self.assertIn(
            "Reusing the private Deep Pattern Python environment at $MANAGED_PYTHON_ROOT.",
            flow,
        )

    def test_managed_environment_records_its_base_runtime(self) -> None:
        body = shell_function(
            self.source,
            "create_managed_python_environment",
            "managed_root_git_state_is_safe",
        )
        self.assertIn('"$base_python" -m venv "$MANAGED_PYTHON_ROOT"', body)
        self.assertIn('"base_python=$base_python"', body)
        self.assertIn("$MANAGED_PYTHON_MARKER_NAME", body)
        self.assertIn('/bin/chmod 600 "$MANAGED_PYTHON_ROOT/', body)


if __name__ == "__main__":
    unittest.main()
