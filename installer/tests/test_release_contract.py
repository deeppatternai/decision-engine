"""Contract tests for signed, monotonic Decision Engine release manifests."""

from __future__ import annotations

import base64
import hashlib
import json
import unittest

from installer import release_contract


_TEST_N = int(
    "a3720881ca0d968786f56f431b0daed7"
    "54dbcbad2299b90bdd61392a86acade1"
    "7fc8f45fd335c77ec3e05aacd7a140db"
    "af7504657503624d1a67989f24f313a7"
    "8e2f64e101f701db31a854df0c69060d"
    "6c75d7060ea4fe1c804733d4e6915ea"
    "5d2388770293a03b972fc0c9ca31dcae"
    "7a305496bade53c8392eab6546a74bb3"
    "91c4c91757140064ee25615599bdb529"
    "8a287be231df27084deaaece006bbcde7"
    "45f53216d2838951ac91cfbdc06aff6e"
    "830f275d6288344b3ffb50e3aa38d609"
    "5ec55e633c6f2a9da0bb6e10f133ae9"
    "57f43ed6be2c608bfa8558a0eb25a4add"
    "0881f7fa41dfa8a84149eeef6dfc4803"
    "c5ba33441e948fa72bb16bc0918fbde5",
    16,
)
_TEST_D = int(
    "87788cf486b49c7fd8acb57bb980adda"
    "6ccb81161007ba08cb853a0cb5738aeb"
    "cd2e92de9a018948d8b1ac767683816e"
    "950f38859f671ea329af9420d44f658b"
    "0b9edcce630288d7556160773fa20d12"
    "3343b8e59c5dc5017a9189b47d27207b"
    "f0b24a0ffd1bc8da7d23cc9047e2f9b"
    "ae4b40d16e31b8dfcc0592aa6ed55b39"
    "75f75d9bdca151aca07e16e0a3b75d4"
    "adb96c7dd1dacfb91dccdaf7de67a99ec"
    "485ba8e5561af825c2ff05fb00f491b8"
    "d3e394b743e343d3f1cff9dbc2aa2fc2"
    "2ab009490b087660d53bdd3d30c1ed05"
    "f02a37da417ce97fa2ebd3ca876dc838"
    "2b176db6ece89307f287797b0c860113"
    "089f775c4004237c1ee0082be15ef3a1",
    16,
)
_TEST_E = 65537
_DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")

# Independent known-answer vector generated once with PyCA cryptography 49.0.0:
# RSA-2048 / PKCS1v15 / SHA-256 over canonical_manifest_bytes(). The private
# key was ephemeral and is neither stored here nor used by the test helper.
_EXTERNAL_VECTOR_N = int(
    "cd929bef4e5eaa37b3fe47d5bb4cd08b"
    "ccd9307be395b62fbb3ed6046ca8c35b"
    "656052657c76d42d4ff2e7a05f2650c"
    "c114909622e0fac1f751c5b948ddae148"
    "ba374875518c407aaf497f99a15bbd08"
    "7d4b12358cb6baee701aafab383ade31"
    "c5bcae4102de5d05d06e13f92f1efdf"
    "08afd3caedf6b9b31525a9652ab85861"
    "012a7c70bac871f3ebde92cd15df3a7f"
    "5db0c15f741d4d3d4396618bad2e9d4"
    "f568368c5600001f2b5b2f666d5bbdb"
    "341d2ad4116e72a458c45990cd63d04b"
    "11d7500cb7278f0259649527009f37bd"
    "38095b912d3422c63c1a9a3eba34c624"
    "bf9477186ab8d45a86aaea15a91d3eb"
    "779e7cfcfc0dc282cf493b030592e5b2"
    "1041",
    16,
)
_EXTERNAL_VECTOR_SIGNATURE = (
    "HlaWzh/f1vpm6FCJhB/mWW0VcMd2r68JI+MsQuKqXDvfleG0Y/h56u2a8qHgixwq"
    "QSxtHfy6YOrIfTZqzpHbwc5xb/4MvzpoXfdbPiBsU6vMd9eK0vri0Pj7IhAphmM"
    "lAY6vrLa7HvYVyxq0bsQZnu2krQ/4fIxnS1zuB1M2BELaEljW6ID4D6sMTk24Li"
    "UOyWjWq/e8ozNVwBeVzPLuVwwNzXfxzAjx/5VJ8wjwAzo91wCuERXxkq1rMHdL4"
    "YpjBKUbcPJBqEr44t9LlNAy3V8YQDLrFeqByh8HxcgIbTZi2E94iiEdhipZq5bdE"
    "OWSHK7qK2nNFhi4FPkADbA+tg=="
)


def _manifest_dict(**changes):
    payload = {
        "schema": 1,
        "repository_id": "deeppatternai/decision-engine",
        "channel": "stable",
        "release_sequence": 23,
        "version": "0.2.3",
        "tag": "v0.2.3",
        "commit": "a" * 40,
        "min_python": "3.12",
        "published_at": "2026-07-21T00:00:00Z",
        "key_id": "test-release-key",
    }
    payload.update(changes)
    return payload


def _sign(manifest):
    digest = hashlib.sha256(release_contract.canonical_manifest_bytes(manifest)).digest()
    encoded = _DIGEST_INFO_SHA256 + digest
    width = (_TEST_N.bit_length() + 7) // 8
    em = b"\x00\x01" + (b"\xff" * (width - len(encoded) - 3)) + b"\x00" + encoded
    signature = pow(int.from_bytes(em, "big"), _TEST_D, _TEST_N).to_bytes(width, "big")
    return release_contract.ReleaseSignature(
        schema=1,
        algorithm=release_contract.RSA_SHA256_ALGORITHM,
        key_id="test-release-key",
        signature=base64.b64encode(signature).decode("ascii"),
    )


class ReleaseManifestSchemaTests(unittest.TestCase):
    def test_valid_manifest_has_one_exact_canonical_utf8_representation(self):
        manifest = release_contract.parse_release_manifest(_manifest_dict())

        canonical = release_contract.canonical_manifest_bytes(manifest)

        expected = (
            b'{"channel":"stable","commit":"'
            + (b"a" * 40)
            + b'","key_id":"test-release-key","min_python":"3.12",'
            + b'"published_at":"2026-07-21T00:00:00Z","release_sequence":23,'
            + b'"repository_id":"deeppatternai/decision-engine","schema":1,'
            + b'"tag":"v0.2.3","version":"0.2.3"}'
        )
        self.assertEqual(canonical, expected)

    def test_unknown_missing_or_wrongly_typed_fields_fail_closed(self):
        cases = []
        extra = _manifest_dict(unexpected=True)
        cases.append(extra)
        missing = _manifest_dict()
        missing.pop("commit")
        cases.append(missing)
        cases.extend(
            [
                _manifest_dict(schema=2),
                _manifest_dict(release_sequence=True),
                _manifest_dict(repository_id="not-a-repository-id"),
                _manifest_dict(commit="ABC"),
                _manifest_dict(version="01.2.3", tag="v01.2.3"),
                _manifest_dict(tag="v9.9.9"),
                _manifest_dict(min_python="3"),
                _manifest_dict(published_at="not-a-time"),
                _manifest_dict(published_at="2026-7-21T00:00:00Z"),
            ]
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(release_contract.ReleaseContractError):
                    release_contract.parse_release_manifest(payload)

    def test_duplicate_json_keys_and_unbounded_sequence_are_rejected(self):
        duplicate = json.dumps(_manifest_dict()).replace('"schema": 1', '"schema": 1, "schema": 1')
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "duplicate"):
            release_contract.parse_release_manifest(duplicate)
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "sequence"):
            release_contract.parse_release_manifest(_manifest_dict(release_sequence=2**63))


class ReleaseSignatureTests(unittest.TestCase):
    def setUp(self):
        self.manifest = release_contract.parse_release_manifest(_manifest_dict())
        self.keys = {
            "test-release-key": release_contract.RsaPublicKey(
                key_id="test-release-key", modulus=_TEST_N, exponent=_TEST_E
            )
        }

    def _verify(self, manifest, signature=None, keys=None, **changes):
        options = {
            "expected_repository_id": "deeppatternai/decision-engine",
            "expected_channel": "stable",
        }
        options.update(changes)
        return release_contract.verify_release_signature(
            manifest,
            signature if signature is not None else _sign(manifest),
            self.keys if keys is None else keys,
            **options,
        )

    def test_valid_stdlib_rsa_sha256_signature_is_accepted(self):
        verified = self._verify(self.manifest)
        self.assertEqual(verified.manifest, self.manifest)
        self.assertEqual(verified.key_id, "test-release-key")

    def test_pyca_generated_known_answer_signature_is_accepted(self):
        manifest = release_contract.parse_release_manifest(
            # This fixed third-party signature predates the 3.12 product floor.
            _manifest_dict(key_id="external-vector-key", min_python="3.9")
        )
        signature = release_contract.ReleaseSignature(
            schema=1,
            algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="external-vector-key",
            signature=_EXTERNAL_VECTOR_SIGNATURE,
        )
        key = release_contract.RsaPublicKey(
            key_id="external-vector-key", modulus=_EXTERNAL_VECTOR_N, exponent=65537
        )
        verified = release_contract.verify_release_signature(
            manifest,
            signature,
            {key.key_id: key},
            expected_repository_id="deeppatternai/decision-engine",
            expected_channel="stable",
        )
        self.assertEqual(verified.manifest.commit, "a" * 40)

    def test_tampered_manifest_unknown_key_and_bad_signature_are_rejected(self):
        signature = _sign(self.manifest)
        tampered = release_contract.parse_release_manifest(
            _manifest_dict(version="0.2.4", tag="v0.2.4")
        )
        cases = (
            (tampered, signature, self.keys),
            (self.manifest, release_contract.ReleaseSignature(
                schema=1,
                algorithm=release_contract.RSA_SHA256_ALGORITHM,
                key_id="unknown-key",
                signature=signature.signature,
            ), self.keys),
            (self.manifest, release_contract.ReleaseSignature(
                schema=1,
                algorithm=release_contract.RSA_SHA256_ALGORITHM,
                key_id="test-release-key",
                signature=base64.b64encode(b"wrong").decode("ascii"),
            ), self.keys),
        )
        for manifest, candidate_signature, keys in cases:
            with self.subTest(key=candidate_signature.key_id, version=manifest.version):
                with self.assertRaises(release_contract.ReleaseContractError):
                    self._verify(manifest, candidate_signature, keys)

    def test_signature_json_contract_is_exact_and_key_id_must_match_manifest(self):
        signature = _sign(self.manifest)
        parsed = release_contract.parse_release_signature(json.loads(json.dumps({
            "schema": signature.schema,
            "algorithm": signature.algorithm,
            "key_id": signature.key_id,
            "signature": signature.signature,
        })))
        self.assertEqual(parsed, signature)
        bad = dict(schema=1, algorithm=release_contract.RSA_SHA256_ALGORITHM,
                   key_id="other-key", signature=signature.signature)
        with self.assertRaises(release_contract.ReleaseContractError):
            self._verify(self.manifest, release_contract.parse_release_signature(bad))

    def test_signed_release_is_bound_to_repository_and_expected_channel(self):
        beta = release_contract.parse_release_manifest(_manifest_dict(channel="beta"))
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "channel"):
            self._verify(beta)
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "repository"):
            self._verify(self.manifest, expected_repository_id="other/project")

    def test_noncanonical_base64_and_direct_oversized_signature_fail_closed(self):
        signature = _sign(self.manifest)
        noncanonical = dict(
            schema=1,
            algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="test-release-key",
            signature="AB==",
        )
        with self.assertRaises(release_contract.ReleaseContractError):
            release_contract.parse_release_signature(noncanonical)
        oversized = release_contract.ReleaseSignature(
            schema=1,
            algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="test-release-key",
            signature=signature.signature * 100,
        )
        with self.assertRaises(release_contract.ReleaseContractError):
            self._verify(self.manifest, oversized)

        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        canonical = signature.signature
        index = alphabet.index(canonical[-3])
        alternate = canonical[:-3] + alphabet[index + 1] + canonical[-2:]
        self.assertEqual(base64.b64decode(alternate), base64.b64decode(canonical))
        noncanonical_valid = release_contract.ReleaseSignature(
            schema=1,
            algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="test-release-key",
            signature=alternate,
        )
        with self.assertRaises(release_contract.ReleaseContractError):
            self._verify(self.manifest, noncanonical_valid)

    def test_directly_constructed_invalid_contract_objects_fail_closed(self):
        invalid_manifest = release_contract.ReleaseManifest(
            **{**_manifest_dict(), "schema": 2}
        )
        with self.assertRaises(release_contract.ReleaseContractError):
            self._verify(invalid_manifest, _sign(self.manifest))

        invalid_signature = release_contract.ReleaseSignature(
            schema=1,
            algorithm=release_contract.RSA_SHA256_ALGORITHM,
            key_id="test-release-key",
            signature=None,
        )
        with self.assertRaises(release_contract.ReleaseContractError):
            self._verify(self.manifest, invalid_signature)

    def test_pathological_trusted_public_keys_are_rejected(self):
        signature = _sign(self.manifest)
        cases = (
            release_contract.RsaPublicKey("test-release-key", _TEST_N + 1, _TEST_E),
            release_contract.RsaPublicKey("test-release-key", _TEST_N, _TEST_N + 2),
            release_contract.RsaPublicKey("test-release-key", (1 << 8192) + 1, _TEST_E),
        )
        for key in cases:
            with self.subTest(bits=key.modulus.bit_length(), exponent=key.exponent):
                with self.assertRaises(release_contract.ReleaseContractError):
                    self._verify(self.manifest, signature, {key.key_id: key})

    def test_well_formed_wrong_public_key_reaches_signature_comparison_and_fails(self):
        wrong_key = release_contract.RsaPublicKey(
            "test-release-key", _TEST_N ^ (1 << 128), _TEST_E
        )
        self.assertEqual(wrong_key.modulus.bit_length(), _TEST_N.bit_length())
        self.assertEqual(wrong_key.modulus % 2, 1)
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "verification failed"):
            self._verify(self.manifest, _sign(self.manifest), {wrong_key.key_id: wrong_key})


class ReleaseTransitionTests(unittest.TestCase):
    def setUp(self):
        self.keys = {
            "test-release-key": release_contract.RsaPublicKey(
                key_id="test-release-key", modulus=_TEST_N, exponent=_TEST_E
            )
        }

    def _authorize(
        self,
        manifest,
        *,
        last_sequence=22,
        last_commit="b" * 40,
        last_manifest_sha256="c" * 64,
        running_python=(3, 13, 0),
    ):
        return release_contract.authorize_release(
            manifest,
            _sign(manifest),
            self.keys,
            expected_repository_id="deeppatternai/decision-engine",
            expected_channel="stable",
            last_sequence=last_sequence,
            last_commit=last_commit,
            last_manifest_sha256=last_manifest_sha256,
            running_python=running_python,
        )

    def test_authorization_requires_the_detached_signature(self):
        manifest = release_contract.parse_release_manifest(_manifest_dict())
        with self.assertRaises(release_contract.ReleaseContractError):
            release_contract.authorize_release(
                manifest,
                None,
                self.keys,
                expected_repository_id="deeppatternai/decision-engine",
                expected_channel="stable",
                last_sequence=22,
                last_commit="b" * 40,
                last_manifest_sha256="c" * 64,
                running_python=(3, 13, 0),
            )

    def test_sequence_never_decreases_and_rollback_uses_a_new_sequence(self):
        stale = release_contract.parse_release_manifest(_manifest_dict(release_sequence=22))
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "downgrade"):
            self._authorize(stale, last_sequence=23)

        forward_rollback = release_contract.parse_release_manifest(
            _manifest_dict(release_sequence=24, commit="b" * 40)
        )
        self._authorize(forward_rollback, last_sequence=23, last_commit="a" * 40)

    def test_same_sequence_cannot_change_commit_and_mirrors_must_match(self):
        manifest = release_contract.parse_release_manifest(_manifest_dict())
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "sequence conflict"):
            self._authorize(manifest, last_sequence=23, last_commit="b" * 40)

        with self.assertRaisesRegex(release_contract.ReleaseContractError, "commit"):
            self._authorize(
                manifest, last_sequence=23, last_commit=None, last_manifest_sha256="c" * 64
            )

        with self.assertRaisesRegex(release_contract.ReleaseContractError, "digest is required"):
            self._authorize(
                manifest,
                last_sequence=23,
                last_commit="a" * 40,
                last_manifest_sha256=None,
            )

        same_commit_changed_metadata = release_contract.parse_release_manifest(
            _manifest_dict(version="0.2.4", tag="v0.2.4")
        )
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "manifest"):
            self._authorize(
                same_commit_changed_metadata,
                last_sequence=23,
                last_commit="a" * 40,
                last_manifest_sha256=release_contract.manifest_sha256(manifest),
            )

        self._authorize(
            manifest,
            last_sequence=23,
            last_commit="a" * 40,
            last_manifest_sha256=release_contract.manifest_sha256(manifest),
        )

        other = release_contract.parse_release_manifest(
            _manifest_dict(release_sequence=24, version="0.2.4", tag="v0.2.4", commit="b" * 40)
        )
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "mirror"):
            release_contract.require_matching_release_artifacts(
                manifest, _sign(manifest), other, _sign(other)
            )

    def test_minimum_python_is_checked_before_any_future_reset(self):
        manifest = release_contract.parse_release_manifest(_manifest_dict(min_python="3.12"))
        self.assertFalse(release_contract.python_is_compatible(manifest, (3, 11, 14)))
        self.assertTrue(release_contract.python_is_compatible(manifest, (3, 12, 0)))
        self.assertTrue(release_contract.python_is_compatible(manifest, (3, 13, 1)))
        with self.assertRaisesRegex(release_contract.ReleaseContractError, "Python"):
            self._authorize(manifest, running_python=(3, 11, 14))
        self._authorize(manifest, running_python=(3, 12, 0))


if __name__ == "__main__":
    unittest.main()
