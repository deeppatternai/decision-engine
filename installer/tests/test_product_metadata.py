"""Behavior tests for bounded desktop product metadata probes."""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from installer.client_hosts.product_metadata import (
    bounded_plist_metadata,
    bounded_product_metadata,
)


class ProductMetadataTestCase(unittest.TestCase):
    def test_default_app_version_field_remains_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp) / "product.json"
            product.write_text(
                '{"applicationName":"trae-solo","appVersion":"0.1.49"}',
                encoding="utf-8",
            )
            probe = bounded_product_metadata(
                product,
                expected_application_name="trae-solo",
            )

        self.assertEqual(probe.status, "matched")
        self.assertEqual(probe.version, (0, 1, 49))

    def test_explicit_version_field_supports_qoder_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp) / "product.json"
            product.write_text(
                '{"applicationName":"qoder","version":"1.106.3"}',
                encoding="utf-8",
            )
            probe = bounded_product_metadata(
                product,
                expected_application_name="qoder",
                version_field="version",
            )

        self.assertEqual(probe.status, "matched")
        self.assertEqual(probe.version, (1, 106, 3))

    def test_explicit_version_field_still_rejects_duplicates_and_invalid_semver(self):
        cases = (
            '{"applicationName":"qoder","version":"1.106.3","version":"1.106.3"}',
            '{"applicationName":"qoder","version":"latest"}',
        )
        for raw in cases:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                product = Path(tmp) / "product.json"
                product.write_text(raw, encoding="utf-8")
                probe = bounded_product_metadata(
                    product,
                    expected_application_name="qoder",
                    version_field="version",
                )
                self.assertEqual(probe.status, "malformed")
                self.assertIsNone(probe.version)

    def test_explicit_version_field_uses_only_top_level_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp) / "product.json"
            product.write_text(
                '{"applicationName":"qoder","version":"1.106.3",'
                '"component":{"version":"9.9.9"}}',
                encoding="utf-8",
            )
            probe = bounded_product_metadata(
                product,
                expected_application_name="qoder",
                version_field="version",
            )

        self.assertEqual(probe.status, "matched")
        self.assertEqual(probe.version, (1, 106, 3))

    def test_identity_only_product_metadata_supports_split_bundle_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp) / "product.json"
            product.write_text('{"productId":"qoder"}', encoding="utf-8")
            probe = bounded_product_metadata(
                product,
                expected_application_name="qoder",
                identity_field="productId",
                version_field=None,
            )

        self.assertEqual(probe.status, "matched")
        self.assertIsNone(probe.version)

    def test_bounded_plist_metadata_reads_bundle_identity_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = Path(tmp) / "Info.plist"
            info.write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.qoder.app",
                        "CFBundleShortVersionString": "0.1.3",
                    }
                )
            )
            probe = bounded_plist_metadata(
                info,
                expected_bundle_identifier="com.qoder.app",
            )

        self.assertEqual(probe.status, "matched")
        self.assertEqual(probe.version, (0, 1, 3))

    def test_bounded_plist_metadata_rejects_symlinked_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.plist"
            target.write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.qoder.app",
                        "CFBundleShortVersionString": "0.1.3",
                    }
                )
            )
            info = root / "Info.plist"
            info.symlink_to(target)
            probe = bounded_plist_metadata(
                info,
                expected_bundle_identifier="com.qoder.app",
            )

        self.assertEqual(probe.status, "unavailable")

    def test_utf8_bom_remains_compatible_for_default_and_explicit_version_fields(self):
        cases = (
            ("trae-solo", "appVersion", "0.1.49", (0, 1, 49)),
            ("qoder", "version", "1.106.3", (1, 106, 3)),
        )
        for name, field, version, expected in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                product = Path(tmp) / "product.json"
                product.write_text(
                    '{"applicationName":"%s","%s":"%s"}'
                    % (name, field, version),
                    encoding="utf-8-sig",
                )
                probe = bounded_product_metadata(
                    product,
                    expected_application_name=name,
                    version_field=field,
                )

                self.assertEqual(probe.status, "matched")
                self.assertEqual(probe.version, expected)

    def test_extremely_long_numeric_version_is_malformed_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp) / "product.json"
            product.write_text(
                '{"applicationName":"qoder","version":"%s.1.1"}'
                % ("9" * 5000),
                encoding="utf-8",
            )
            probe = bounded_product_metadata(
                product,
                expected_application_name="qoder",
                version_field="version",
            )

        self.assertEqual(probe.status, "malformed")
        self.assertIsNone(probe.version)


if __name__ == "__main__":
    unittest.main()
