"""Red-line leak scanner for the Decision Engine public shell (release design §14).

Machine-checkable half of the "prove the shell is clean" gate. It walks the
shippable shell surface and flags anything that looks like:

- a secret / credential (API-key formats, PEM private keys, long bearer hex),
- a non-loopback IPv4 literal (a possible server IP),
- an *internal identifier* that must never appear in the public shell — the
  private server module names, the IP core package, the private repo slugs, or
  the internal design document's filename. Matched case- AND separator-insensitively,
  so one hyphenated denylist entry also catches the spaced and underscored
  spellings of the same name.

Addresses in the IETF documentation ranges (``203.0.113.7`` and friends, see
``_DOC_NETWORKS``) are allowed anywhere. That keeps the scan free of baseline
noise — otherwise this scanner flags the very fixtures its test suite uses to
prove the detectors work, and the exit code gates nothing. The exemption rests
on a stated assumption, not a law of nature: **this project's infrastructure
does not route the documentation prefixes.** RFC 5737 / RFC 3849 keep them off
the public internet, but an organisation *can* route them internally, and there
such a literal would leak topology exactly as an RFC 1918 address does. Anyone
reusing this scanner in a network that routes them must drop the exemption.
Every other address — private ranges included — is still a finding.

It does NOT try to detect leaked prompts / layout definitions / orchestration by
content — those are prevented structurally (this shell is transport-only and
embeds no such artifacts) and confirmed by human review. This scanner is the
automated backstop for the mechanically-detectable classes.

Usage:
    python3 -m installer.leak_scan [PATH ...]     # default: the installer/ dir
Exit code 0 = clean, 1 = findings (also printed).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple

# Internal-identifier denylist — the private module / package / repo names that
# must not appear in the public shell. Crucially the PUBLIC shell must not
# *hardcode these names either* (that would itself leak the private topology).
# So the shipped default is EMPTY; the private release process injects the real
# names at clean-room scan time via:
#   - env DE_LEAK_INTERNAL_IDENTIFIERS="name1,name2,..."
#   - or a gitignored file named in env DE_LEAK_DENYLIST_FILE (one per line)
# Neither the names nor that file are ever lifted into the public repo.
_DEFAULT_INTERNAL_IDENTIFIERS: Sequence[str] = ()
INTERNAL_IDENTIFIER_KIND_PREFIX = "internal-identifier:"


def load_internal_identifiers() -> tuple:
    ids: List[str] = list(_DEFAULT_INTERNAL_IDENTIFIERS)
    env = os.getenv("DE_LEAK_INTERNAL_IDENTIFIERS", "")
    ids.extend(x.strip() for x in env.split(",") if x.strip())
    path = os.getenv("DE_LEAK_DENYLIST_FILE")
    if path and os.path.exists(path):
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    ids.append(line)
        except OSError:
            pass
    return tuple(ids)


INTERNAL_IDENTIFIERS = load_internal_identifiers()

# --- separator-insensitive identifier matching --------------------------------
#
# A denylist entry is a slug (``alpha-beta-gamma``), but the same name reaches a
# public file in whatever spelling the author reached for: a heading writes it
# ``Alpha Beta Gamma``, a module writes it ``alpha_beta_gamma`` or
# ``alpha.beta.gamma``, a class writes it ``AlphaBetaGamma``, a path writes it
# ``alpha/beta/gamma``. A plain substring test is separator-SENSITIVE and misses
# all but the exact spelling — a false negative in a security gate.
#
# So both sides are canonicalised down to the same token sequence. Each rule
# below only ever ADDS a boundary; none fuses two tokens that were separate, so
# canonicalisation cannot invent an adjacency the text did not have.

# 1. camelCase / PascalCase run the words together with no separator at all.
#    Split at each case transition, before lowercasing. The second alternative
#    handles an acronym followed by a word: ``HTTPSProxy`` -> ``HTTPS Proxy``.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# 2. A ``.`` or ``/`` BETWEEN two word characters is a module / path separator:
#    ``alpha.beta.gamma``, ``alpha/beta/gamma``. One followed by whitespace is
#    sentence punctuation and must NOT fuse "…ends the audit. Core team…".
_PATH_SEPARATOR = re.compile(r"(?<=\w)[./](?=\w)")

# 3. Runs of the ordinary separators collapse to one space.
_SEPARATOR_RUN = re.compile(r"[-_\s]+")

# Other punctuation is deliberately NOT a separator: "beta; gamma" stays two
# things. See LEAK_SCAN.md "Residual risk" for the spellings left uncovered.

# 4. Case-splitting cannot resolve an acronym boundary — it reads ``OAuthProxy``
#    as ``O Auth Proxy`` and ``IPv6Gateway`` as ``I Pv6 Gateway``, neither of
#    which matches the slug. So also compare the entry, stripped of separators
#    entirely, against each alphanumeric RUN of the line, likewise stripped.
#    Doing this per-token rather than over the whole line is what keeps it safe:
#    the line "we audit mcp servers" has no token equal to ``auditmcp``, so a
#    whole-line strip (which would fuse it) is exactly what we avoid.
_ALNUM_RUN = re.compile(r"[0-9a-z]+")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def _canonicalise(text: str) -> str:
    text = _CAMEL_BOUNDARY.sub(" ", text)
    text = _PATH_SEPARATOR.sub(" ", text)
    return _SEPARATOR_RUN.sub(" ", text.lower())


def _strip_separators(canonical: str) -> str:
    """``alpha beta gamma`` -> ``alphabetagamma`` (compared token-wise only)."""
    return _NON_ALNUM.sub("", canonical)


@lru_cache(maxsize=None)
def _identifier_pattern(canonical: str) -> "re.Pattern":
    """Match ``canonical`` as a whole token in canonicalised text.

    The boundary is "no adjacent ASCII alphanumeric" rather than ``\\b``, for
    two reasons: canonicalised text is already lowercased ASCII-folded on the
    separator axis, and ``\\b`` treats a CJK neighbour as a word character —
    which would miss ``中Alpha Beta Gamma中`` in a Chinese doc.

    Boundaries matter because collapsing separators can otherwise manufacture a
    match a raw substring test would never make: the entry ``widget-x``
    canonicalises to ``widget x``, which occurs inside ``thewidget x``.
    """
    return re.compile(r"(?<![0-9a-z])%s(?![0-9a-z])" % re.escape(canonical))


class _Entry(NamedTuple):
    """A denylist entry, pre-bound to the three forms it is matched by."""

    ident: str  # as written on the denylist, used in the finding label
    raw_lower: str  # the verbatim old predicate
    pattern: Optional["re.Pattern"]  # whole-token match on canonicalised text
    stripped: str  # separator-free form, compared against single tokens


def _compile_identifiers(idents: Sequence[str]) -> List[_Entry]:
    compiled: List[_Entry] = []
    for ident in idents:
        canonical = _canonicalise(ident).strip()
        # A separator-only entry canonicalises to "" — an empty pattern is a
        # zero-width assertion that fires wherever two non-alphanumerics abut.
        # Drop the canonical arm for it; the raw arm still applies unchanged.
        pattern = _identifier_pattern(canonical) if canonical else None
        compiled.append(_Entry(ident, ident.lower(), pattern, _strip_separators(canonical)))
    return compiled


def _identifier_hit(entry: _Entry, line_lower: str, line_canonical: str, line_tokens: frozenset) -> bool:
    """True if the entry occurs in the line, in any separator spelling.

    Three arms, deliberately a union, cheapest first. The raw substring test is
    kept so this can only ever ADD detections, never remove one: it still
    catches the affixed forms (``alpha-beta-gamma2``, ``xalpha_beta_gamma``)
    that the word-bounded canonical arm rejects on its own.
    """
    if entry.raw_lower in line_lower:
        return True
    if entry.pattern is not None and entry.pattern.search(line_canonical):
        return True
    # No guard needed for an empty ``stripped``: ``_ALNUM_RUN`` is ``+``, so it
    # never yields an empty token and ``"" in line_tokens`` is always False.
    return entry.stripped in line_tokens

# Secret / credential shapes. The ``sk-`` class allows hyphens so it also
# catches modern project (``sk-proj-…``) and Anthropic (``sk-ant-…``) keys.
SECRET_PATTERNS = (
    ("openai-key", re.compile(r"sk-[A-Za-z0-9-]{20,}")),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack-token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("pem-private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer-hex-token", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
)
_RSA_PUBLIC_MODULUS = re.compile(r"[0-9a-fA-F]{512,}")

# IPv4 literals, excluding loopback / any / documentation-safe hosts.
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_ALLOWED_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}

# IPv6 candidates: any hex:hex[:...] run with >=2 colons; validated with the
# stdlib ``ipaddress`` module so we only flag *real* addresses (times, MACs,
# and short "a:b" pairs fail validation and are dropped). §14 red-line #1 is
# "server IP" — an IPv6 endpoint must not slip past the IPv4-only check.
_IPV6_CANDIDATE = re.compile(r"[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,}")

# Ranges the IETF reserves *for documentation* and keeps off the public
# internet (RFC 5737 §1, RFC 3849 §4). Allowed here because THIS project does
# not route them (see the module docstring) and because flagging them makes the
# scanner report its own doc and test fixtures, leaving the exit code unusable
# as a clean-room gate. Two consequences to keep in mind rather than paper over:
# a maintainer who routes one of these prefixes internally loses the guarantee,
# and the 96 free bits under 2001:db8::/32 are a channel a *deliberate*
# exfiltrator could hand-encode an address into. Neither is in this gate's
# threat model (accidental leak / careless copy-paste); both are in the doc.
#
# Only ever add a range an RFC keeps out of the wild. Private / carrier /
# benchmark ranges stay findings: a private address still leaks topology.
_DOC_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),     # RFC 5737 TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),  # RFC 5737 TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),   # RFC 5737 TEST-NET-3
    ipaddress.ip_network("2001:db8::/32"),    # RFC 3849 documentation prefix
)


class Finding(NamedTuple):
    path: str
    line: int
    kind: str
    excerpt: str


def _is_documentation_address(literal: str) -> bool:
    """True iff ``literal`` sits in an RFC documentation range.

    Fail-closed: anything ``ipaddress`` refuses to parse — a dotted quad with a
    leading-zero octet, rejected since 3.9.5 — is *not* proven safe, so it stays
    suspicious. (Writing such a quad here as an example would itself be a
    finding; the test suite carries it, assembled at run time.)
    """
    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        return False
    return any(addr in net for net in _DOC_NETWORKS if net.version == addr.version)


def _ip_is_suspicious(ip: str) -> bool:
    if ip in _ALLOWED_IPS:
        return False
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if any(o > 255 for o in octets):
        return False  # not a real IP (e.g. a version string)
    return not _is_documentation_address(ip)


def _suspicious_ipv6(line: str) -> List[str]:
    """Return real, non-loopback, non-documentation IPv6 addresses in ``line``."""
    hits: List[str] = []
    for cand in _IPV6_CANDIDATE.findall(line):
        try:
            addr = ipaddress.IPv6Address(cand)
        except ipaddress.AddressValueError:
            continue
        if addr.is_loopback or addr.is_unspecified:
            continue
        if _is_documentation_address(str(addr)):
            continue
        hits.append(str(addr))
    return hits


def _trusted_public_moduli(text: str, path: str) -> frozenset:
    """Return RSA public moduli from the release trust store.

    A modulus is intentionally public and must ship with the updater. The exemption is
    deliberately narrow: only a valid ``release-trust.json`` object, only the supported
    RSA signature algorithm, and only a full-length even-sized hexadecimal modulus.
    The same hex run anywhere else remains a leak finding.
    """
    expected = Path(__file__).resolve().parent / "release-trust.json"
    try:
        candidate = Path(path).resolve()
    except (OSError, TypeError):
        return frozenset()
    if candidate != expected:
        return frozenset()
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return frozenset()
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "keys"}
        or payload.get("schema") != 1
    ):
        return frozenset()
    entries = payload.get("keys")
    if not isinstance(entries, list):
        return frozenset()
    allowed = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        modulus = entry.get("modulus_hex")
        if (
            set(entry) == {"algorithm", "exponent", "key_id", "modulus_hex", "revoked"}
            and entry.get("algorithm") == "rsa-pkcs1v15-sha256"
            and entry.get("exponent") == 65537
            and isinstance(entry.get("key_id"), str)
            and bool(entry["key_id"].strip())
            and isinstance(entry.get("revoked"), bool)
            and isinstance(modulus, str)
            and len(modulus) % 2 == 0
            and _RSA_PUBLIC_MODULUS.fullmatch(modulus)
        ):
            allowed.add(modulus)
    return frozenset(allowed)


def scan_text(
    text: str,
    *,
    path: str = "<text>",
    internal_identifiers: Optional[Sequence[str]] = None,
) -> List[Finding]:
    idents = INTERNAL_IDENTIFIERS if internal_identifiers is None else internal_identifiers
    compiled = _compile_identifiers(idents)
    public_moduli = _trusted_public_moduli(text, path)
    findings: List[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line_lower = line.lower()
        line_canonical = _canonicalise(line)
        line_tokens = frozenset(_ALNUM_RUN.findall(line_lower)) if compiled else frozenset()
        for entry in compiled:
            # Case- AND separator-insensitive: an internal name in any casing or
            # any separator spelling is still a leak.
            if _identifier_hit(entry, line_lower, line_canonical, line_tokens):
                findings.append(
                    Finding(
                        path,
                        lineno,
                        INTERNAL_IDENTIFIER_KIND_PREFIX + entry.ident,
                        line.strip(),
                    )
                )
        for kind, pattern in SECRET_PATTERNS:
            matches = list(pattern.finditer(line))
            if matches and not (
                kind == "bearer-hex-token"
                and all(match.group(0) in public_moduli for match in matches)
            ):
                findings.append(Finding(path, lineno, "secret:%s" % kind, line.strip()))
        for match in _IPV4.findall(line):
            if _ip_is_suspicious(match):
                findings.append(Finding(path, lineno, "ipv4:%s" % match, line.strip()))
        for match in _suspicious_ipv6(line):
            findings.append(Finding(path, lineno, "ipv6:%s" % match, line.strip()))
    return findings


# --- which files the walk opens ----------------------------------------------
#
# Secrets and server IPs live in config and code far beyond the original
# doc/script set, so a suffix allowlist of `.py/.json/.md/.sh/.toml/.txt` is a
# silent false negative: a key in a `.yaml`, `.env` or `.js` file inside an
# assembled bundle was never opened. A file is opened if ANY of three arms
# matches — a config/code suffix, a well-known name, or a small file that
# sniffs as text — so on any given directory the set of file *types* opened is
# a strict superset of the old set. The one deliberate narrowing is by
# LOCATION, not type: the `.git` and `node_modules` subtrees are pruned (see
# `_SKIP_DIRS`), so a file of a scanned type sitting inside one of those is no
# longer opened.
_SCAN_SUFFIXES = frozenset({
    # original doc / script set
    ".py", ".json", ".md", ".sh", ".toml", ".txt",
    # config / data
    ".yaml", ".yml", ".cfg", ".ini", ".conf", ".properties", ".xml", ".env",
    # web / scripting code
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css",
})

# Files that carry config/secrets but no suffix pathlib can key on: build
# recipes, and the `.env` family. (`.env` above only matches `foo.env`: the
# bare `.env` dotfile has Path(".env").suffix == "", so it is matched by name.)
_SCAN_FILENAMES = frozenset({"dockerfile", "makefile"})

# A file with no recognised suffix or name is still scanned when it is small and
# sniffs as text — the backstop for an extensionless secret/config file, so the
# gate is not hostage to an ever-growing suffix list. Bounded twice: a size cap
# (a config/secret file is never this large) and a NUL-byte sniff (a binary).
# BOTH are load-bearing — the committed 222 KiB arm64 stopper binary is UNDER
# the cap, so only the NUL sniff rejects it.
_TEXT_SNIFF_MAX_BYTES = 262_144  # 256 KiB

# Directories never descended into: version-control internals and vendored
# dependency trees. `_iter_files` drops these from the walk itself (os.walk's
# `dirnames`), so their subtrees are never traversed OR opened. The scan target
# — the shippable shell / assembled bundle — contains neither; and once
# `.js`/`.ts` are in scope a `node_modules` would otherwise flood the scan with
# vendored files (a minified blob's long hex run false-positives as a bearer
# token and trains operators to ignore the gate).
_SKIP_DIRS = frozenset({".git", "node_modules"})


def _looks_like_small_text(path: Path) -> bool:
    """True for a small file whose head carries no NUL byte (a binary marker).

    Only the head is read: a NUL in the first 4 KiB is the standard "this is
    binary" signal. Final UTF-8 validation is left to ``scan_paths``' existing
    ``read_text`` guard, which already drops undecodable bytes.

    Two deliberate bounds, BOTH accepted residuals recorded in LEAK_SCAN.md: an
    unknown-suffix file larger than the cap, or one that raises ``OSError``, is
    not opened. A *recognised* suffix / name skips this arm entirely and is
    scanned at any size, so the cap only bounds the open-ended text-sniff
    fallback — not the config/code formats a leak actually tends to land in.
    """
    try:
        if path.stat().st_size > _TEXT_SNIFF_MAX_BYTES:
            return False
        with path.open("rb") as fh:
            return b"\x00" not in fh.read(4096)
    except OSError:
        return False


def _should_scan(path: Path) -> bool:
    name = path.name.lower()
    if name in _SCAN_FILENAMES or name == ".env" or name.startswith(".env."):
        return True
    if path.suffix.lower() in _SCAN_SUFFIXES:
        return True
    return _looks_like_small_text(path)


def _iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for base in paths:
        if base.is_file():
            yield base
        elif base.is_dir():
            matched: List[Path] = []
            for dirpath, dirnames, filenames in os.walk(base):
                # Prune the walk in place: dropping a name from ``dirnames``
                # stops os.walk (topdown) from ever descending into it, so a
                # large ``node_modules`` costs no traversal, not just no reads.
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for name in filenames:
                    child = Path(dirpath) / name
                    if child.is_file() and _should_scan(child):
                        matched.append(child)
            # Sorted for a deterministic finding order, matching the old walk.
            yield from sorted(matched)


def scan_paths(
    paths: Iterable[Path],
    *,
    internal_identifiers: Optional[Sequence[str]] = None,
) -> List[Finding]:
    """Scan every eligible file under ``paths``.

    There is deliberately no exclusion mechanism: a leak gate with a bypass is a
    leak gate whose default entry point is the weak one. Files that must name a
    forbidden token (this scanner's doc and test suite) name one the detectors
    allow — an RFC documentation address — or assemble it at run time.
    """
    findings: List[Finding] = []
    for f in _iter_files([Path(p) for p in paths]):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text(text, path=str(f), internal_identifiers=internal_identifiers))
    return findings


# ---- /audit degrade-branch routing-prose-only gate (PR5, v5 §15) -------------
# Assert the /audit canonical degrade-routing reference stays ROUTING-PROSE-ONLY:
# no hub-proprietary orchestration IP (voice roster / hub orchestration fields /
# adjudication-convergence prompt fragments). Best-effort tripwire mirroring the
# AQG-side leak_gate (skills/aqg-multi-review). The markers target hub SPECIFICS,
# not the generic words "adjudication"/"roster", so the sanctioned negation
# ("the role prompts ... live in that skill — never restate them here") scans clean.

# The heading needs a real boundary after `hub-unreachable` (space / paren / EOL),
# so a different heading like `hub-unreachable-notes` is NOT selected (audit f-codex5).
_DEGRADE_HEADER_RE = re.compile(
    r"(?i)^(#{2,4})[ \t]*degraded[ \t]*/[ \t]*hub-unreachable(?=[ \t()]|$)")
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")

# Markers matched over NORMALIZED text. Multi-token markers tolerate an optional
# separator (` ?`) so glued forms ("replyprefix", official "DeepSeek") are caught
# too (audit convergent). Roster includes every hub voice.
_HUB_PROSE_MARKERS = (
    ("hub-orchestration-field",
     re.compile(r"\b(?:adjudication ?hint|reply ?prefix|panel ?label|panel ?size|caller ?family)\b")),
    ("hub-voice-label", re.compile(r"\bclean ?(?:claude|context)\b")),
    ("hub-voice-roster", re.compile(r"\b(?:gpt ?5|o ?3|deep ?seek|qwen|gemini|grok)\b")),
    ("hub-adjudication-prompt", re.compile(r"\bconvergent ?vs ?divergent\b")),
)

# The canonical docs whose degrade section this gate protects.
_DEGRADE_DOCS = ("skills/audit/references/de-lite-routing.md",)


def _normalize_prose(text: str) -> str:
    """Fold case + separators + inline markdown/HTML so orthographic and
    formatting variants collapse to one form (audit convergent): strip HTML
    comments/tags, split camelCase, lowercase, then fold separators AND markdown
    emphasis punctuation (`*`` ~ , : |) — so `adjudication **hint**` / `gpt**5**`
    and `DeepSeek` no longer evade."""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)  # split camelCase
    text = text.lower()
    return re.sub(r"[\s\-_./·*`~,:|]+", " ", text)


def _degrade_regions(text: str):
    """Yield (region_text, header_line) for EVERY degrade section — looping all
    matches so a clean decoy cannot hide a later section (audit f-codex3/grok4).
    Fence-aware + empty-ATX-aware line scan for the terminator: a `#`-prefixed line
    INSIDE a ``` code fence does not truncate the region (audit f-claude2/gemini2),
    and a bare `##` heading with no trailing space still terminates it (f-codex4)."""
    lines = text.split("\n")
    n = len(lines)
    regions = []
    i = 0
    while i < n:
        m = _DEGRADE_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        level = len(m.group(1))
        header_line = i + 1
        terminator = re.compile(r"^#{1,%d}(?:[ \t]|$)" % level)
        body = [lines[i]]
        j = i + 1
        in_fence = False
        while j < n:
            if _FENCE_RE.match(lines[j]):
                in_fence = not in_fence
            elif not in_fence and terminator.match(lines[j]):
                break
            body.append(lines[j])
            j += 1
        regions.append(("\n".join(body), header_line))
        i = j
    return regions


def scan_degrade_prose_text(text: str, path: str = "<text>") -> List[Finding]:
    """Scan EVERY degrade section in a doc for hub-IP prose. Category-only Findings
    (the matched value is never echoed). An in-memory helper: no degrade section
    means nothing to scan — the fail-closed 'section absent' check lives in
    ``scan_degrade_prose_repo`` where the docs are KNOWN to be required."""
    findings: List[Finding] = []
    for region, header_line in _degrade_regions(text):
        norm = _normalize_prose(region)
        for category, pattern in _HUB_PROSE_MARKERS:
            if pattern.search(norm):
                findings.append(Finding(
                    path, header_line, "degrade-hub-ip:%s" % category,
                    "routing-prose-only violation (value withheld)"))
    return findings


def scan_degrade_prose_repo(repo_root: Path) -> List[Finding]:
    """Fail-CLOSED gate over the canonical degrade docs: a missing doc, an
    unreadable doc, OR a doc with no degrade section is a finding — never a silent
    pass (audit 4/4 convergent: a renamed heading / deleted file must alert)."""
    findings: List[Finding] = []
    for rel in _DEGRADE_DOCS:
        p = Path(repo_root) / rel
        if not p.is_file():
            findings.append(Finding(str(p), 0, "degrade-doc-missing",
                                    "expected degrade doc absent (fail-closed)"))
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(Finding(str(p), 0, "degrade-scan-error",
                                    "degrade doc unreadable (fail-closed)"))
            continue
        if not _degrade_regions(text):
            findings.append(Finding(str(p), 0, "degrade-section-missing",
                                    "expected degrade section absent (fail-closed)"))
            continue
        findings.extend(scan_degrade_prose_text(text, path=str(p)))
    return findings


def main(argv: List[str] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        roots = [Path(a).expanduser() for a in argv]
    else:
        roots = [Path(__file__).resolve().parent]
    findings = scan_paths(roots)
    # PR5b: also gate the /audit degrade-branch prose. Always run against the
    # CANONICAL repo root (parents[1] of this module) so the fail-closed
    # missing-doc / missing-section checks fire regardless of the argv roots — and
    # so a bare `-m installer.leak_scan` covers it.
    findings = findings + scan_degrade_prose_repo(Path(__file__).resolve().parents[1])
    if not findings:
        print("leak-scan: clean (%d root(s))" % len(roots))
        return 0
    print("leak-scan: %d finding(s)" % len(findings), file=sys.stderr)
    for f in findings:
        print("  %s:%d  %s  %s" % (f.path, f.line, f.kind, f.excerpt), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
