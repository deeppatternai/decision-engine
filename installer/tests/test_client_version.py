"""Client-version single-source-of-truth + rollout re-stamp.

The version the device reports at activation gates the server's version-gated GE byte-isolation
(design §8: the server strips artifact bytes only for clients ≥ 0.2.0). So the reported version must
equal CLIENT_VERSION everywhere and be RE-STAMPED on every (re)install — a stale value preserved across
an upgrade would leave the byte-isolation dormant for that device.
"""
from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from client.runner import CLIENT_VERSION
from installer import install


class ClientVersionSotTest(unittest.TestCase):
    def test_pyproject_client_version_matches_client_version(self):
        # the client pyproject is `pyproject.client.toml` in the source repo and is flattened to
        # `pyproject.toml` in the published client shell — accept either so the test rides the flatten.
        root = Path(__file__).resolve().parents[2]
        pp = root / "pyproject.client.toml"
        if not pp.exists():
            pp = root / "pyproject.toml"
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["version"], CLIENT_VERSION)

    def test_config_example_client_version_matches(self):
        # the shipped example's informational client_version is kept in sync with CLIENT_VERSION (drift
        # guard) — activation ignores it (it reports the running CLIENT_VERSION), but a stale example
        # would still mislead a reader.
        root = Path(__file__).resolve().parents[2]
        example = json.loads((root / "installer" / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["client_version"], CLIENT_VERSION)

    def test_runner_reexports_the_leaf_version(self):
        # CLIENT_VERSION is defined once in the leaf client.version and re-exported by client.runner
        from client import version as leaf
        from client import runner
        self.assertEqual(runner.CLIENT_VERSION, leaf.CLIENT_VERSION)

    def test_write_de_config_restamps_over_a_stale_prior(self):
        # a reinstall loads the prior config; client_version must be RE-STAMPED to the current
        # CLIENT_VERSION (not preserved), so a 0.1.0 → 0.2.0 upgrade actually reports 0.2.0.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            saved = os.environ.get("DE_CONFIG_PATH")
            os.environ["DE_CONFIG_PATH"] = str(cfg_path)
            try:
                install.write_de_config(
                    server_endpoint="https://hub.example", api_key="inv", device_name="d",
                    prior={"client_version": "0.1.0", "device_id": "keep-me", "access_token": "tok"})
                written = json.loads(cfg_path.read_text(encoding="utf-8"))
            finally:
                if saved is None:
                    os.environ.pop("DE_CONFIG_PATH", None)
                else:
                    os.environ["DE_CONFIG_PATH"] = saved
        self.assertEqual(written["client_version"], CLIENT_VERSION)   # re-stamped, not the stale 0.1.0
        self.assertEqual(written["device_id"], "keep-me")             # unrelated prior fields preserved
        self.assertEqual(written["access_token"], "tok")


if __name__ == "__main__":
    unittest.main()
