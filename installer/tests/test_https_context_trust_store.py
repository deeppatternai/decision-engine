"""Behavior tests for the shared HTTPS trust-store resolution.

Three modules build the SSL context for authenticated hub fetches with the same
CA-resolution helper: `installer.shim._https_context`, `installer.activate._https_context`,
and `client.runner.https_context`. They must all obey one contract:

  * with NO explicit SSL_CERT_FILE / REQUESTS_CA_BUNDLE / SSL_CERT_DIR, the context MUST NOT
    bypass the platform default trust store. The pre-fix code called
    `ssl.create_default_context(cafile=certifi.where())`, which loaded ONLY certifi's bundle
    and dropped the OS store — on Windows that store holds roots (e.g. the GTS chain behind
    endpoint.deeppattern.ai) absent from a stale/partial certifi, so the MCP launcher failed
    with CERTIFICATE_VERIFY_FAILED. R1 catches that: the context trusts a SUPERSET of the
    platform default CAs.
  * certifi is a fallback, NOT a supplement: on a machine whose platform store is usable, the
    context trusts EXACTLY the platform store and certifi is never consulted (R1b). This is
    asserted three ways with different blind spots (see the R1b tests). certifi is loaded only
    when the platform ships no usable store, and even then via a FRESH certifi-only context, so
    it can never sit on top of a populated OS store (R-rescue).
  * an explicit SSL_CERT_FILE / REQUESTS_CA_BUNDLE / SSL_CERT_DIR stays authoritative and
    exclusive — the OS store is not silently folded in on top of a user-pinned CA (R2, R2-dir).
  * when no usable store exists AND certifi is unavailable, the helper still returns a context
    with verification ON (empty anchors → TLS fails CLOSED, never open) (R-failclosed).
  * verification is never silently disabled: every returned context keeps
    verify_mode == CERT_REQUIRED and check_hostname is True (R3).

Certificate identity is keyed on DER bytes (`get_ca_certs(binary_form=True)`), not on serial
numbers, which are unique only within an issuer and collide across self-signed roots.

Introspectability vs. usability — two different gates, deliberately kept apart. The platform
store is *introspectable in memory* only where it loads eagerly: Windows enumerates it, POSIX
loads default paths lazily so `get_ca_certs()` is empty even with a full /etc/ssl. The DER
comparisons (R1, R1b-result) can only run where introspectable and skip elsewhere. But the
MECHANISM guards (R1b-mechanism-{spy,calls}) do NOT need introspectability — they assert only
that certifi was not consulted — so they run wherever the helper's OWN usability predicate is
True, i.e. on POSIX CI too. An earlier revision gated the mechanism guards behind the
introspectability skip, which silently disabled the entire union/certifi-only regression
contract on every non-Windows runner; that is exactly what these gates now avoid.
"""

from __future__ import annotations

import os
import ssl
import sys
import unittest
from unittest import mock

import certifi

from client import runner
from installer import activate, shim

_CONTEXT_BUILDERS = (
    ("shim._https_context", shim._https_context),
    ("activate._https_context", activate._https_context),
    ("runner.https_context", runner.https_context),
)

# Every CA-override variable the helpers now honor. All three are consumed by the explicit
# exclusive branch, and all three (SSL_CERT_DIR included) are respected by
# ssl.create_default_context(), so a "no override" baseline must clear every one of them.
_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "SSL_CERT_DIR")

# A DefaultVerifyPaths that names no usable file/dir — used to force the POSIX branch to judge
# the platform store unusable without depending on the host's real /etc/ssl.
_UNUSABLE_PATHS = ssl.DefaultVerifyPaths(None, None, "SSL_CERT_FILE", "", "SSL_CERT_DIR", "")


def _ca_der(context: ssl.SSLContext) -> set:
    """CA identity as DER bytes — collision-free, unlike X.509 serial numbers."""
    return set(context.get_ca_certs(binary_form=True))


def _platform_store_usable() -> bool:
    """Mirror the helper's own usability predicate (no env overrides set), so a test can run a
    mechanism assertion exactly when production would take the platform-store branch — on every
    platform, not only where the store is introspectable in memory."""
    if sys.platform == "win32":
        return bool(ssl.create_default_context().get_ca_certs())
    paths = ssl.get_default_verify_paths()
    try:
        return bool(
            (paths.cafile and os.path.getsize(paths.cafile) > 0)
            or (paths.capath and os.listdir(paths.capath))
        )
    except OSError:
        return False


class HttpsContextTrustStoreTests(unittest.TestCase):
    def _clear_ca_env(self):
        for var in _CA_ENV_VARS:
            os.environ.pop(var, None)

    # ---- R1: platform store is not dropped -------------------------------------------------

    def test_no_env_context_keeps_the_platform_trust_store(self):
        # R1. RED before the fix: a certifi-only context drops OS-store roots, so this subset
        # check fails on any platform whose store carries a root certifi lacks. DER comparison,
        # so it can only run where the store is introspectable; skips (not passes) elsewhere.
        with mock.patch.dict(os.environ, {}, clear=False):
            self._clear_ca_env()
            platform_der = _ca_der(ssl.create_default_context())
            if not platform_der:
                self.skipTest("platform default store is not introspectable here (lazy load)")
            for label, build in _CONTEXT_BUILDERS:
                with self.subTest(builder=label):
                    context = build()
                    self.assertIsNotNone(context, f"{label} returned no context")
                    missing = platform_der - _ca_der(context)
                    self.assertFalse(
                        missing,
                        f"{label} dropped {len(missing)} platform trust-store root(s)",
                    )

    # ---- R1b: certifi is a fallback, never a supplement ------------------------------------

    def test_certifi_is_not_consulted_when_platform_store_usable_spy(self):
        # R1b-mechanism-spy. Runs wherever the helper's OWN usability predicate is True (POSIX
        # CI included), NOT only where the store is introspectable — that is the whole point:
        # the union/certifi-only regression contract must have teeth on every runner. Catches a
        # regression that reaches certifi through the `certifi.where` module attribute at call
        # time. Narrow by construction: a `from certifi import where` binding or a hardcoded
        # bundle path would evade it, which is why the call-count guard below backs it up.
        if not _platform_store_usable():
            self.skipTest("no usable platform store here; nothing to prove certifi stays out of")
        for label, build in _CONTEXT_BUILDERS:
            with self.subTest(builder=label), mock.patch.dict(os.environ, {}, clear=False):
                self._clear_ca_env()
                with mock.patch.object(certifi, "where", wraps=certifi.where) as where_spy:
                    build()
                self.assertEqual(
                    0,
                    where_spy.call_count,
                    f"{label} consulted certifi despite a usable platform store — "
                    "certifi must be a fallback, never a supplement",
                )

    def test_certifi_is_not_consulted_when_platform_store_usable_callcount(self):
        # R1b-mechanism-calls. Shape-independent backstop for the spy: count SSLContext
        # .load_verify_locations calls during a bare create_default_context() (the baseline) vs.
        # during build(). On the usable path build() does exactly what create_default_context()
        # does and no more, so the counts must match. ANY extra anchor load — union
        # (load_verify_locations(certifi) on top) or certifi-only (create_default_context(
        # cafile=certifi), a different count) — breaks the equality, however the bundle is
        # named. Works on POSIX (baseline 0) and Windows (baseline = per-cert eager load) alike.
        if not _platform_store_usable():
            self.skipTest("no usable platform store here; call-count baseline is not meaningful")
        for label, build in _CONTEXT_BUILDERS:
            with self.subTest(builder=label), mock.patch.dict(os.environ, {}, clear=False):
                self._clear_ca_env()
                with mock.patch.object(
                    ssl.SSLContext, "load_verify_locations", autospec=True
                ) as m:
                    ssl.create_default_context()
                    baseline = m.call_count
                with mock.patch.object(
                    ssl.SSLContext, "load_verify_locations", autospec=True
                ) as m:
                    build()
                    built = m.call_count
                self.assertEqual(
                    baseline,
                    built,
                    f"{label} made {built} anchor loads vs. baseline {baseline} — certifi (or "
                    "another bundle) folded onto the usable platform store",
                )

    def test_certifi_does_not_widen_a_usable_platform_store_result(self):
        # R1b-result. The RESULT half: the trust set equals the platform store exactly. Only has
        # teeth where certifi ships a root the OS store lacks, and needs an introspectable store,
        # so it is Windows-only in practice — the mechanism guards above carry POSIX.
        with mock.patch.dict(os.environ, {}, clear=False):
            self._clear_ca_env()
            platform_der = _ca_der(ssl.create_default_context())
            if not platform_der:
                self.skipTest("platform default store is not introspectable here (lazy load)")
            for label, build in _CONTEXT_BUILDERS:
                with self.subTest(builder=label):
                    self.assertEqual(
                        platform_der,
                        _ca_der(build()),
                        f"{label} widened the usable platform store (certifi folded on top?)",
                    )

    # ---- R2: an explicit override is authoritative and exclusive ---------------------------

    def test_explicit_ca_file_stays_authoritative_and_exclusive(self):
        # R2. An explicit file override pins exactly that bundle — the OS store is NOT folded in
        # on top of it. certifi's bundle stands in for a user-supplied CA file. Exercised for
        # both file variables and the both-set precedence (SSL_CERT_FILE wins).
        pinned_der = _ca_der(ssl.create_default_context(cafile=certifi.where()))
        cases = (
            ("SSL_CERT_FILE", {"SSL_CERT_FILE": certifi.where()}),
            ("REQUESTS_CA_BUNDLE", {"REQUESTS_CA_BUNDLE": certifi.where()}),
            (
                "both-set (SSL_CERT_FILE wins)",
                {"SSL_CERT_FILE": certifi.where(), "REQUESTS_CA_BUNDLE": os.devnull},
            ),
        )
        for env_label, env in cases:
            for label, build in _CONTEXT_BUILDERS:
                with self.subTest(builder=label, env=env_label), mock.patch.dict(
                    os.environ, {}, clear=False
                ):
                    self._clear_ca_env()
                    os.environ.update(env)
                    context = build()
                    self.assertIsNotNone(context, f"{label} returned no context")
                    self.assertEqual(
                        pinned_der,
                        _ca_der(context),
                        f"{label} did not honor the pinned CA exclusively ({env_label})",
                    )

    def test_ssl_cert_dir_is_an_exclusive_override(self):
        # R2-dir. SSL_CERT_DIR must be treated like SSL_CERT_FILE: an exclusive capath, NOT a
        # value that leaves the platform probe to fold the OS default cafile (or, on a probe
        # miss, certifi) in on top. capath certs load lazily so DER is empty — assert the
        # MECHANISM instead: create_default_context is called once with capath=<dir> and no
        # cafile, certifi is never consulted, and the platform-probe branch is never entered.
        real_cdc = ssl.create_default_context
        for label, build in _CONTEXT_BUILDERS:
            with self.subTest(builder=label), mock.patch.dict(os.environ, {}, clear=False):
                self._clear_ca_env()
                os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())
                with mock.patch.object(
                    ssl, "create_default_context", wraps=real_cdc
                ) as cdc, mock.patch.object(
                    certifi, "where", wraps=certifi.where
                ) as where_spy:
                    context = build()
                self.assertEqual(
                    0, where_spy.call_count, f"{label} consulted certifi under SSL_CERT_DIR"
                )
                self.assertEqual(
                    1,
                    cdc.call_count,
                    f"{label} did not take the exclusive branch for SSL_CERT_DIR "
                    f"(create_default_context called {cdc.call_count}x)",
                )
                _, kwargs = cdc.call_args
                self.assertEqual(
                    os.path.dirname(certifi.where()),
                    kwargs.get("capath"),
                    f"{label} did not pin SSL_CERT_DIR as an exclusive capath",
                )
                self.assertIsNone(
                    kwargs.get("cafile"),
                    f"{label} folded a cafile in alongside the pinned SSL_CERT_DIR",
                )
                self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
                self.assertTrue(context.check_hostname)

    # ---- R-rescue / R-failclosed: the no-usable-store branch -------------------------------

    def _force_unusable_store(self):
        # Drive every helper down the `not platform_store_usable` branch host-independently:
        # pretend we are on POSIX and that OpenSSL's default paths name nothing usable.
        return (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(ssl, "get_default_verify_paths", return_value=_UNUSABLE_PATHS),
        )

    def test_rescue_loads_certifi_only_when_no_usable_store(self):
        # R-rescue. When no platform store is usable, the helper must fall back to a
        # certifi-ONLY context (fresh, so nothing is layered on top of anything) and actually
        # consult certifi. certifi loads eagerly from a cafile, so DER is checkable here.
        certifi_der = _ca_der(ssl.create_default_context(cafile=certifi.where()))
        p_platform, p_paths = self._force_unusable_store()
        for label, build in _CONTEXT_BUILDERS:
            with self.subTest(builder=label), mock.patch.dict(
                os.environ, {}, clear=False
            ), p_platform, p_paths:
                self._clear_ca_env()
                with mock.patch.object(certifi, "where", wraps=certifi.where) as where_spy:
                    context = build()
                self.assertEqual(
                    1, where_spy.call_count, f"{label} did not consult certifi as the rescue"
                )
                self.assertEqual(
                    certifi_der,
                    _ca_der(context),
                    f"{label} rescue did not trust exactly the certifi bundle",
                )
                self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
                self.assertTrue(context.check_hostname)

    def test_fail_closed_when_no_store_and_certifi_unavailable(self):
        # R-failclosed. The strongest safety claim in the helper docstring: with no usable store
        # AND certifi missing/unreadable, the helper returns a context with NO anchors and
        # verification still ON, so TLS fails CLOSED (CERTIFICATE_VERIFY_FAILED) rather than
        # silently trusting everything. Mock create_default_context so the returned fallback
        # context is a real, empty, secure-by-default context regardless of the host's own store.
        def empty(*_a, **_k):
            return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        p_platform, p_paths = self._force_unusable_store()
        for label, build in _CONTEXT_BUILDERS:
            with self.subTest(builder=label), mock.patch.dict(
                os.environ, {}, clear=False
            ), p_platform, p_paths, mock.patch.object(
                ssl, "create_default_context", side_effect=empty
            ), mock.patch.object(
                certifi, "where", side_effect=RuntimeError("certifi unavailable")
            ) as where_spy:
                self._clear_ca_env()
                context = build()
            self.assertIsNotNone(context, f"{label} returned no context on the fail-closed path")
            self.assertGreaterEqual(
                where_spy.call_count, 1, f"{label} did not attempt the certifi rescue"
            )
            self.assertEqual(
                set(),
                _ca_der(context),
                f"{label} trusted anchors on the fail-closed path (should be empty)",
            )
            self.assertEqual(
                ssl.CERT_REQUIRED,
                context.verify_mode,
                f"{label} disabled certificate verification on the fail-closed path",
            )
            self.assertTrue(
                context.check_hostname,
                f"{label} disabled hostname checking on the fail-closed path",
            )

    # ---- R3: verification is never silently disabled --------------------------------------

    def test_verification_is_always_enabled(self):
        # R3. Neither branch may return a context with verification disabled. Has teeth on every
        # platform: a drifted copy returning an unverified context is caught even where R1/R1b
        # skip. Covers the no-override, SSL_CERT_FILE and SSL_CERT_DIR entry paths.
        environments = (
            ("no override", {}),
            ("explicit SSL_CERT_FILE", {"SSL_CERT_FILE": certifi.where()}),
            ("explicit SSL_CERT_DIR", {"SSL_CERT_DIR": os.path.dirname(certifi.where())}),
        )
        for env_label, env in environments:
            for label, build in _CONTEXT_BUILDERS:
                with self.subTest(builder=label, env=env_label), mock.patch.dict(
                    os.environ, {}, clear=False
                ):
                    self._clear_ca_env()
                    os.environ.update(env)
                    context = build()
                    self.assertEqual(
                        ssl.CERT_REQUIRED,
                        context.verify_mode,
                        f"{label} disabled certificate verification ({env_label})",
                    )
                    self.assertTrue(
                        context.check_hostname,
                        f"{label} disabled hostname checking ({env_label})",
                    )


if __name__ == "__main__":
    unittest.main()
