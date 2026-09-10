"""Tests for the red-line leak scanner + the proof that the shell is clean."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from installer import leak_scan


def _v4(*octets: str) -> str:
    """Assemble a dotted quad at run time.

    An address the scanner *flags* must never appear as a literal in this file:
    the shell-surface scan reads this very source, so a literal would make the
    scanner report its own fixture. The RFC documentation addresses are the
    exception — the scanner is required to allow them, so they are written out.
    """
    return ".".join(octets)


def _v6(*groups: str) -> str:
    """Assemble an IPv6 address at run time (an empty group yields ``::``)."""
    return ":".join(groups)


# Reserved for documentation and forbidden on the public internet:
# RFC 5737 TEST-NET-1/2/3 and the RFC 3849 IPv6 prefix. Safe as literals.
_DOC_V4 = "203.0.113.7"
_DOC_V4_ALL = ("192.0.2.1", "198.51.100.42", "203.0.113.7")
_DOC_V6 = "2001:db8::1"

# Arbitrary globally-routable addresses that must stay findings.
_ROUTABLE_V4 = _v4("88", "77", "66", "55")
_ROUTABLE_V6 = _v6("2a0f", "dead", "beef", "", "1")


class ScanTextTestCase(unittest.TestCase):
    def test_clean_text_has_no_findings(self):
        self.assertEqual(leak_scan.scan_text("just some harmless docs\n"), [])

    def test_detects_openai_key(self):
        findings = leak_scan.scan_text("token = 'sk-" + "A" * 40 + "'\n")
        self.assertTrue(any(f.kind.startswith("secret:openai-key") for f in findings))

    def test_detects_pem_private_key(self):
        # Construct the header from fragments so this test file itself carries no
        # full key-header literal (which the secret-scan hook would flag).
        header = "-----BEGIN " + "RSA PRIVATE " + "KEY-----"
        findings = leak_scan.scan_text(header + "\n")
        self.assertTrue(any(f.kind == "secret:pem-private-key" for f in findings))

    def test_allows_valid_rsa_public_modulus_only_in_release_trust_store(self):
        modulus = "ab" * 256
        trust = json.dumps({
            "schema": 1,
            "keys": [{
                "algorithm": "rsa-pkcs1v15-sha256",
                "exponent": 65537,
                "key_id": "public-test",
                "modulus_hex": modulus,
                "revoked": False,
            }],
        })
        self.assertEqual(
            leak_scan.scan_text(
                trust,
                path=str(Path(leak_scan.__file__).resolve().parent / "release-trust.json"),
            ),
            [],
        )
        findings = leak_scan.scan_text(
            'token = "%s"\n' % modulus,
            path="installer/config.json",
        )
        self.assertTrue(any(f.kind == "secret:bearer-hex-token" for f in findings))

    def test_malformed_trust_store_does_not_bypass_hex_secret_detection(self):
        modulus = "cd" * 256
        malformed = json.dumps({
            "schema": 1,
            "keys": [{
                "algorithm": "rsa-pkcs1v15-sha256",
                "exponent": 3,
                "modulus_hex": modulus,
            }],
        })
        findings = leak_scan.scan_text(
            malformed,
            path=str(Path(leak_scan.__file__).resolve().parent / "release-trust.json"),
        )
        self.assertTrue(any(f.kind == "secret:bearer-hex-token" for f in findings))

    def test_valid_public_key_shape_is_not_exempt_outside_real_trust_path(self):
        modulus = "ef" * 256
        trust = json.dumps({
            "schema": 1,
            "keys": [{
                "algorithm": "rsa-pkcs1v15-sha256",
                "exponent": 65537,
                "key_id": "public-test",
                "modulus_hex": modulus,
                "revoked": False,
            }],
        })
        findings = leak_scan.scan_text(trust, path="scratch/release-trust.json")
        self.assertTrue(any(f.kind == "secret:bearer-hex-token" for f in findings))

    def test_detects_suspicious_ipv4(self):
        findings = leak_scan.scan_text("endpoint = %s\n" % _ROUTABLE_V4)
        self.assertTrue(any(f.kind == "ipv4:%s" % _ROUTABLE_V4 for f in findings))

    def test_detects_suspicious_ipv6(self):
        findings = leak_scan.scan_text("endpoint = %s\n" % _ROUTABLE_V6)
        self.assertTrue(any(f.kind.startswith("ipv6:") for f in findings))

    def test_allows_rfc5737_documentation_ipv4(self):
        # TEST-NET-1/2/3 exist so docs and tests can name an address. RFC 5737
        # forbids them on the public internet -> never a real endpoint.
        for addr in _DOC_V4_ALL:
            with self.subTest(addr=addr):
                self.assertEqual(leak_scan.scan_text("endpoint = %s\n" % addr), [])

    def test_allows_rfc3849_documentation_ipv6(self):
        self.assertEqual(leak_scan.scan_text("endpoint = %s\n" % _DOC_V6), [])

    def test_documentation_allowlist_does_not_cover_neighbouring_addresses(self):
        # One octet outside TEST-NET-3, and the prefix just past the RFC 3849
        # /32, are ordinary addresses and must still be flagged.
        near_v4 = _v4("203", "0", "114", "7")
        near_v6 = _v6("2001", "db9", "", "1")
        self.assertTrue(leak_scan.scan_text("endpoint = %s\n" % near_v4))
        self.assertTrue(leak_scan.scan_text("endpoint = %s\n" % near_v6))

    def test_still_flags_private_and_internal_ranges(self):
        # The allowlist covers documentation ranges ONLY. A private-range
        # literal still leaks internal topology and must remain a finding.
        for addr in (_v4("10", "0", "0", "7"), _v4("192", "168", "1", "1"), _v4("172", "16", "0", "1")):
            with self.subTest(addr=addr):
                self.assertTrue(leak_scan.scan_text("host = %s\n" % addr))

    def test_ipv4_mapped_ipv6_still_exposes_the_embedded_address(self):
        # `::ffff:<quad>` is not matched by the IPv6 candidate regex (which has
        # no `.`), so it looks like a bypass -- but the IPv4 detector reads the
        # embedded quad independently and the address is still a finding.
        findings = leak_scan.scan_text("endpoint = ::ffff:%s\n" % _ROUTABLE_V4)
        self.assertTrue(any(f.kind == "ipv4:%s" % _ROUTABLE_V4 for f in findings))

    def test_transition_prefixes_that_embed_ipv4_are_flagged(self):
        # The 6to4 and NAT64 transition prefixes carry a real IPv4 address in
        # their bits. Neither is a documentation range; both must stay findings.
        # (Their prefixes are assembled, not written out: they parse as valid
        # IPv6, so a literal here would be a finding in this very file.)
        for addr in (_v6("2002", "1234", "5678", "", "1"), _v6("64", "ff9b", "", "5850", "4237")):
            with self.subTest(addr=addr):
                findings = leak_scan.scan_text("endpoint = %s\n" % addr)
                self.assertTrue(any(f.kind.startswith("ipv6:") for f in findings))

    def test_known_gap_documentation_prefix_low_bits_are_not_inspected(self):
        # Accepted limitation, pinned so it stays known: 2001:db8::/32 leaves 96
        # free bits, into which a *deliberate* exfiltrator could hand-encode an
        # address. Out of this gate's threat model (accidental leak / careless
        # copy-paste); pinned here so the accepted boundary remains explicit.
        self.assertEqual(leak_scan.scan_text("e = %s\n" % _v6("2001", "db8", "", "128d", "ddef")), [])

    def test_unparseable_dotted_quad_stays_suspicious(self):
        # Leading-zero octets are rejected by ``ipaddress`` (3.9.5+). The
        # allowlist check must fail closed: still a finding, never a crash.
        weird = _v4("01", "2", "3", "4")
        self.assertTrue(leak_scan.scan_text("endpoint = %s\n" % weird))

    def test_secret_in_documentation_ip_line_is_still_flagged(self):
        # Allowing the address must not allow the line it sits on.
        findings = leak_scan.scan_text("host=%s key=sk-%s\n" % (_DOC_V4, "C" * 40))
        self.assertTrue(any(f.kind.startswith("secret:openai-key") for f in findings))
        self.assertFalse(any(f.kind.startswith("ipv4:") for f in findings))

    def test_allows_ipv6_loopback(self):
        self.assertEqual(leak_scan.scan_text("bind ::1\n"), [])

    def test_ipv6_ignores_clock_and_mac_like_tokens(self):
        # Not valid IPv6 -> not flagged (times, MAC-ish 6-group hex).
        self.assertEqual(leak_scan.scan_text("at 12:34:56 mac de:ad:be:ef:00:11\n"), [])

    def test_detects_modern_and_anthropic_keys(self):
        self.assertTrue(any(f.kind.startswith("secret:openai-key")
                            for f in leak_scan.scan_text("k=sk-proj-" + "a" * 30 + "\n")))
        self.assertTrue(any(f.kind.startswith("secret:openai-key")
                            for f in leak_scan.scan_text("k=sk-ant-" + "b" * 30 + "\n")))

    def test_case_insensitive_internal_identifier(self):
        # Denylist is injected (empty by default in the shipped shell); the
        # match is case-insensitive.
        findings = leak_scan.scan_text("import Secret_Pkg.thing\n", internal_identifiers=["secret_pkg"])
        self.assertTrue(any("internal-identifier:secret_pkg" in f.kind for f in findings))

    def test_default_denylist_is_empty_in_shipped_shell(self):
        # The public shell must not hardcode private names; absent env injection
        # the internal-identifier check is inert.
        self.assertEqual(leak_scan.load_internal_identifiers(), ())


# All identifiers below are synthetic placeholders. A real internal name must
# never be written into a committed file — that would itself be the leak this
# scanner exists to prevent.
_SLUG = "crimson-widget-works"


class SeparatorInsensitiveIdentifierTestCase(unittest.TestCase):
    """One hyphenated denylist entry must catch every separator spelling.

    The historical bug: matching was a plain ``ident.lower() in line.lower()``
    substring test, so the entry ``crimson-widget-works`` silently missed the
    Markdown heading ``# Crimson Widget Works`` — the exact shape in which a
    product name reaches a public doc.
    """

    def _kinds(self, line, idents=(_SLUG,)):
        return [f.kind for f in leak_scan.scan_text(line, internal_identifiers=list(idents))]

    def test_hyphen_entry_matches_spaced_heading(self):
        # The real-world false negative: a title-cased, space-separated heading.
        self.assertIn("internal-identifier:%s" % _SLUG, self._kinds("# Crimson Widget Works\n"))

    def test_hyphen_entry_matches_underscore_text(self):
        self.assertIn("internal-identifier:%s" % _SLUG, self._kinds("import crimson_widget_works\n"))

    def test_hyphen_entry_matches_mixed_separators_and_case(self):
        self.assertIn("internal-identifier:%s" % _SLUG, self._kinds("see Crimson-Widget_Works today\n"))

    def test_hyphen_entry_matches_repeated_separator_runs(self):
        self.assertIn("internal-identifier:%s" % _SLUG, self._kinds("crimson --  widget __ works\n"))

    def test_camelcase_run_together_is_flagged(self):
        # A class name or a jammed heading writes the slug with no separator at
        # all. Splitting on case transitions recovers the token sequence.
        self.assertIn("internal-identifier:%s" % _SLUG, self._kinds("class CrimsonWidgetWorks:\n"))
        self.assertIn("internal-identifier:%s" % _SLUG, self._kinds("var crimsonWidgetWorks = 1\n"))

    def test_dotted_module_path_is_flagged(self):
        self.assertIn("internal-identifier:%s" % _SLUG, self._kinds("import crimson.widget.works\n"))

    def test_slashed_path_is_flagged(self):
        self.assertIn("internal-identifier:%s" % _SLUG, self._kinds("src/crimson/widget/works.py\n"))

    def test_camelcase_denylist_entry_matches_hyphenated_text(self):
        # Canonicalisation is symmetric: an operator who writes the entry in a
        # different spelling than the leak still gets a hit. Without this, a
        # CamelCase entry would silently detect nothing at all.
        self.assertIn("internal-identifier:CrimsonWidgetWorks",
                      self._kinds("see crimson-widget-works\n", idents=("CrimsonWidgetWorks",)))

    def test_acronym_prefix_boundary_is_split(self):
        # Pins the SECOND alternative of _CAMEL_BOUNDARY, `(?<=[A-Z])(?=[A-Z][a-z])`.
        # The fixture carries a suffix on purpose: bare `IPCore` would be
        # rescued by the stripped-token arm, masking the loss of this rule.
        # `IPCoreEngine` is one token, so only the split can catch `ip-core`.
        self.assertIn("internal-identifier:ip-core",
                      self._kinds("class IPCoreEngine:\n", idents=("ip-core",)))

    def test_ambiguous_acronym_run_is_caught_by_the_stripped_token_arm(self):
        # Case-splitting reads `OAuthProxy` as `O Auth Proxy` and `IPv6Gateway`
        # as `I Pv6 Gateway` — an acronym boundary it cannot resolve. The
        # separator-free token comparison catches them anyway.
        self.assertIn("internal-identifier:oauth-proxy",
                      self._kinds("class OAuthProxy:\n", idents=("oauth-proxy",)))
        self.assertIn("internal-identifier:ipv6-gateway",
                      self._kinds("class IPv6Gateway:\n", idents=("ipv6-gateway",)))
        self.assertIn("internal-identifier:xml-http-request",
                      self._kinds("XMLHTTPRequest\n", idents=("xml-http-request",)))

    def test_stripped_token_arm_is_token_scoped_not_line_scoped(self):
        # The stripped comparison runs against each alphanumeric RUN, never the
        # whole line. Stripping the whole line would fuse 'audit; mcp' into
        # 'auditmcp' and fire on this sentence — where the ';' means the
        # canonical arm correctly declines to join the two words.
        self.assertEqual(self._kinds("we audit; mcp servers here\n", idents=("audit-mcp",)), [])
        # ...while the run-together token itself is still caught.
        self.assertIn("internal-identifier:audit-mcp",
                      self._kinds("import auditmcp\n", idents=("audit-mcp",)))

    def test_spaced_prose_form_is_an_intended_hit(self):
        # Pins the accepted consequence, so nobody "fixes" it later: a slug
        # written out as ordinary spaced words IS a leak, and no matcher can
        # tell '# Crimson Widget Works' from 'the crimson widget works team'.
        self.assertIn("internal-identifier:%s" % _SLUG,
                      self._kinds("the crimson widget works team ships today\n"))

    def test_camelcase_name_with_a_suffix_is_flagged(self):
        # The stripped-token arm alone would miss this (the token is
        # 'crimsonwidgetworksclient'); case-splitting is what catches it.
        self.assertIn("internal-identifier:%s" % _SLUG,
                      self._kinds("class CrimsonWidgetWorksClient:\n"))

    def test_no_false_positive_on_ordinary_prose(self):
        # The words appear, but never as one adjacent run: punctuation and
        # intervening words are NOT separators, so the slug does not match.
        self.assertEqual(self._kinds("The crimson widget; works fine.\n"), [])
        self.assertEqual(self._kinds("crimson paint on a widget that works\n"), [])

    def test_sentence_period_does_not_fuse_across_the_boundary(self):
        # A '.' is a separator only BETWEEN word characters. Followed by
        # whitespace it is sentence punctuation: fusing there would make every
        # two-word slug fire on the seam between two sentences.
        self.assertEqual(self._kinds("that ends the widget. Works fine.\n",
                                     idents=("widget-works",)), [])

    def test_normalisation_respects_word_boundaries(self):
        # 'widget x' occurs inside 'thewidget x' only as a mid-word fragment.
        # Collapsing separators must not manufacture a match a raw substring
        # test would never have made. (Note 'TheWidget x' DOES match, by
        # design: the case transition is a real token boundary.)
        self.assertEqual(self._kinds("thewidget x is fine\n", idents=("widget-x",)), [])

    def test_boundary_check_is_not_ascii_word_char_naive(self):
        # A CJK neighbour is a boundary, not a word character: `\b` would treat
        # 中 as a word char and miss this. The heading form still leaks.
        self.assertIn("internal-identifier:%s" % _SLUG, self._kinds("中Crimson Widget Works中\n"))

    def test_separator_only_entry_has_no_canonical_arm(self):
        # A degenerate entry canonicalises to "" — an unguarded empty pattern is
        # a zero-width assertion firing wherever two non-alphanumerics abut. The
        # fixture must END in punctuation, or it passes even without the guard.
        self.assertEqual(self._kinds("plain prose.\n", idents=("-",)), [])
        # The raw arm is untouched and still matches a literal hyphen. The guard
        # neutralises the canonical arm only — it does not tame a bad entry.
        self.assertIn("internal-identifier:-", self._kinds("- a markdown bullet\n", idents=("-",)))

    def test_denylist_entry_with_regex_metacharacters_is_literal(self):
        # The entry is interpolated into a regex; re.escape must neutralise it.
        # The `.` must survive canonicalisation to test escaping at all, so it
        # sits next to `+` (not a word char) where _PATH_SEPARATOR won't eat it.
        # Unescaped, `.` is "any char" and `+` is a quantifier: both assertions
        # below fail without re.escape.
        self.assertIn("internal-identifier:a.+c", self._kinds("a.+c\n", idents=("a.+c",)))
        self.assertEqual(self._kinds("abbc\n", idents=("a.+c",)), [])

    def test_suffixed_identifier_is_still_flagged(self):
        # Regression guard: the pre-existing raw-substring detection must
        # survive. Word boundaries alone would drop the trailing-digit form.
        self.assertIn("internal-identifier:crimson-widget-works",
                      self._kinds("crimson-widget-works2\n"))
        self.assertIn("internal-identifier:crimson_widget_works",
                      self._kinds("xcrimson_widget_works\n", idents=("crimson_widget_works",)))

    def test_zero_width_separators_are_an_accepted_residual_miss(self):
        # NOT a bug to fix here: this gate catches ACCIDENTAL disclosure of our
        # own names, not adversarial evasion of our own scanner. Pinned so the
        # gap is a recorded decision rather than an unnoticed hole.
        # Written as escapes on purpose: a literal U+200B in source is invisible
        # to a reader and to grep.
        self.assertEqual(self._kinds("crimson\u200bwidget\u200bworks\n"), [])
        self.assertEqual(self._kinds("crimson%20widget%20works\n"), [])

    def test_line_wrapped_name_is_an_accepted_residual_miss(self):
        # scan_text is line-scoped; a prose wrap between two words of the slug
        # splits it. This accepted boundary is pinned by the assertion below.
        self.assertEqual(self._kinds("the crimson widget\nworks pipeline\n"), [])

    def test_env_injected_denylist(self):
        import os
        saved = os.environ.get("DE_LEAK_INTERNAL_IDENTIFIERS")
        os.environ["DE_LEAK_INTERNAL_IDENTIFIERS"] = "secretname, othername"
        try:
            self.assertEqual(leak_scan.load_internal_identifiers(), ("secretname", "othername"))
        finally:
            if saved is None:
                os.environ.pop("DE_LEAK_INTERNAL_IDENTIFIERS", None)
            else:
                os.environ["DE_LEAK_INTERNAL_IDENTIFIERS"] = saved

    def test_allows_loopback_ip(self):
        self.assertEqual(leak_scan.scan_text("bind 127.0.0.1\n"), [])

    def test_ignores_version_like_dotted_numbers(self):
        # 999.999.999.999 is not a real IP (octet > 255) -> not flagged.
        self.assertEqual(leak_scan.scan_text("version 999.999.999.999\n"), [])

    def test_detects_internal_identifier(self):
        # Build the module name from fragments to avoid the literal in this file;
        # denylist injected explicitly (shipped default is empty).
        ident = "audit" + "_hub"
        findings = leak_scan.scan_text("from server.%s import store\n" % ident,
                                       internal_identifiers=[ident])
        self.assertTrue(any("internal-identifier:" in f.kind for f in findings))


class ShellSurfaceCleanTestCase(unittest.TestCase):
    def test_shippable_shell_surface_is_clean(self):
        # `scan_paths` has no exclusion mechanism, so this covers the scanner,
        # co-located sources, and this very file. If it passes, the release
        # gate's exit code carries no baseline noise and a non-zero result means
        # a finding.
        root = Path(leak_scan.__file__).resolve().parent
        findings = leak_scan.scan_paths([root])
        self.assertEqual(
            findings,
            [],
            msg="section-14 red-line leak in shell surface:\n"
            + "\n".join("%s:%d %s %s" % (f.path, f.line, f.kind, f.excerpt) for f in findings),
        )


class WidenedFileCoverageTestCase(unittest.TestCase):
    """`_iter_files` must open the config / code formats where secrets and server
    IPs actually live — not just the original ``.py/.json/.md/.sh/.toml/.txt``
    set. A secret in a ``.yaml`` or ``.env`` file used to pass the release gate
    silently, because those files were never opened. These pin the widened
    coverage AND the two guards that keep it from scanning binaries or a
    vendored ``node_modules`` tree.

    Each secret is assembled from fragments (``"sk-" + "x" * 40``) so this test
    file itself carries no full-key literal and still scans clean under the
    no-exclusion ``ShellSurfaceCleanTestCase`` above. The fillers are non-hex
    (`x`/`X`) so a fixture matches exactly one detector, not also bearer-hex.
    """

    def _scan_tmp(self, files):
        """Write ``{relpath: text|bytes}`` into a temp dir; return its findings."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel, body in files.items():
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(body, bytes):
                    target.write_bytes(body)
                else:
                    target.write_text(body, encoding="utf-8")
            return leak_scan.scan_paths([root])

    def test_secret_in_yaml_is_flagged(self):
        # The headline gap: a config format the old suffix set never opened.
        key = "sk-" + "x" * 40
        findings = self._scan_tmp({"config.yaml": "api_key: %s\n" % key})
        self.assertTrue(any(f.kind.startswith("secret:openai-key") for f in findings))

    def test_secret_in_dotenv_is_flagged(self):
        # `.env` is the canonical secrets file AND a pathlib trap: it must be
        # matched by NAME, because Path('.env').suffix == '' defeats a suffix
        # check. `.env.local` and friends are covered by the same name rule.
        akey = "AKIA" + "X" * 16
        findings = self._scan_tmp({".env": "AWS_ACCESS_KEY_ID=%s\n" % akey})
        self.assertTrue(any(f.kind.startswith("secret:aws-access-key") for f in findings))

    def test_extensionless_wellknown_name_is_scanned(self):
        # Dockerfile / Makefile carry no suffix pathlib can key on; matched by
        # name so an assembled bundle's build recipe is not a blind spot.
        key = "sk-" + "x" * 40
        findings = self._scan_tmp({"Dockerfile": "ENV TOKEN=%s\n" % key})
        self.assertTrue(any(f.kind.startswith("secret:openai-key") for f in findings))

    def test_binary_file_is_not_scanned(self):
        # The small-text heuristic must reject binaries. A leading NUL is valid
        # UTF-8 (U+0000), so `read_text` would NOT raise — only the NUL sniff
        # stops this. Pins that the sniff, not the size cap, is what guards the
        # committed 222 KiB (< 256 KiB cap) arm64 stopper binary.
        blob = b"\x00token=sk-" + b"x" * 40  # a secret-shaped run behind a NUL
        findings = self._scan_tmp({"payload.bin": blob})
        self.assertEqual(findings, [])

    def test_vendored_and_vcs_dirs_are_pruned(self):
        # Widening to `.js` must not turn a working-tree run into a scan of every
        # vendored file (a minified blob's hex run would false-positive and
        # train operators to ignore the gate). `.git` and `node_modules` are
        # pruned; a real `.js` at the root is still opened.
        line = "const k = '%s'\n" % ("sk-" + "x" * 40)
        findings = self._scan_tmp({
            "node_modules/pkg/index.js": line,
            ".git/config": "token = %s\n" % ("sk-" + "x" * 40),
            "app.js": line,
        })
        flagged = [f for f in findings if f.kind.startswith("secret:openai-key")]
        self.assertEqual(len(flagged), 1)
        self.assertTrue(flagged[0].path.endswith("app.js"))

    def test_dotenv_family_variant_is_flagged(self):
        # The name rule covers the whole `.env` family, not just the bare file:
        # `.env.local`, `.env.production`, ... (Path(".env.local").suffix is
        # `.local`, so this too can only be caught by name).
        akey = "AKIA" + "X" * 16
        findings = self._scan_tmp({".env.local": "AWS_SECRET_ACCESS_KEY=%s\n" % akey})
        self.assertTrue(any(f.kind.startswith("secret:aws-access-key") for f in findings))

    def test_extensionless_small_text_is_scanned(self):
        # The third arm's POSITIVE path: an extensionless, non-well-known file
        # (a stray `credentials` / `netrc`-style secret) is opened because it is
        # small and sniffs as text — the backstop for types not on any list.
        key = "sk-" + "x" * 40
        findings = self._scan_tmp({"credentials": "password = %s\n" % key})
        self.assertTrue(any(f.kind.startswith("secret:openai-key") for f in findings))

    def test_oversized_unknown_type_is_an_accepted_residual_miss(self):
        # Pinned, not a bug to fix here: the text-sniff arm is size-bounded, so
        # a >256 KiB file with NO recognised suffix/name is left unopened even
        # if it is plain text with a secret. A recognised suffix/name has
        # no cap (see the .yaml/.env tests above), so this is the narrow corner.
        key = "sk-" + "x" * 40
        big = "harmless padding line\n" * 20000  # ~440 KB, over the 256 KiB cap
        findings = self._scan_tmp({"dump.unknownext": big + "leak=%s\n" % key})
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
