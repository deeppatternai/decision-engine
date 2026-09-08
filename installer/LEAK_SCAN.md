# Leak scan — public projection red-line gate (release design §14)

The public projection may include client-owned windows, display integrations,
and other local UI code. It must **never** contain: real server addresses /
secrets / device tokens / production account metadata / private prompts /
private layout definitions / server orchestration /
market-research·forecast pipelines / proprietary server-side UI or
ad-generation source.

This is enforced in two complementary layers.

## 1. Structural (why the private service boundary is clean by construction)

The public projection separates client-owned UI and transport code from private
service behavior. Three boundary components matter here:

- `install.py` — copies bundle bodies + symlinks skills + writes a local config.
  It embeds no endpoint (owner-provided at install time), no token (filled by
  activation), and no product logic.
- `shim.py` — handles local host, recovery, and display-routing concerns, then
  forwards server-owned requests to the configured `/mcp`. It embeds no private
  endpoint, prompt, layout, voice roster, or server orchestration.
- `config.py` — filesystem + config helpers. No network endpoints baked in.

Because no private prompt / layout / orchestration / pipeline artifact is ever
*present* in these files, there is nothing of that class to leak. The endpoint
and token exist only in the per-device `config.json` the user's machine writes
at install/activation time — outside the shippable source. Public client UI is
not evidence that private service rendering or advertising source was shipped.

## 2. Mechanical (`leak_scan.py`) — automated backstop

`python3 -m installer.leak_scan` walks the shippable surface and flags the
machine-detectable classes:

- **secrets** — OpenAI-style keys including modern `sk-proj-…` / `sk-ant-…`,
  Google `AIza…` keys, AWS access keys, Slack/GitHub tokens, PEM private-key
  headers, long bearer-hex blobs.
- **server IP** — any non-loopback IPv4 **or IPv6** literal. IPv4 excludes
  loopback / `0.0.0.0` / broadcast and octets > 255 (version-string false
  hits); IPv6 candidates are validated with the stdlib `ipaddress` module and
  loopback (`::1`) / unspecified (`::`) are excluded, so clock strings and
  MAC-like hex do not false-trigger. The IETF documentation ranges are the one
  allowed exception — see below.
- **internal identifiers** — the private server module names, the IP core
  package name, the private repo slugs, and the internal design document's filename,
  matched **case- and separator-insensitively**. These would leak knowledge of
  the private topology even without a secret.

#### The one IP exception: RFC documentation ranges

Both IP detectors allow the ranges the IETF reserves *for documentation* and
keeps off the public internet — `192.0.2.0/24`, `198.51.100.0/24` and
`203.0.113.0/24` (RFC 5737 TEST-NET-1/2/3), and `2001:db8::/32` (RFC 3849).
Without the exception the scanner reports the very fixtures its own test suite
uses to prove the IP detectors work, so a whole-tree run can never exit 0 and
the exit code gates nothing.

The exception rests on **a stated assumption, not a law of nature**: *this
project does not route the documentation prefixes.* The RFCs keep them off the
public internet; they do not stop an organisation routing them internally, and
in such a network a TEST-NET literal leaks topology exactly as an RFC 1918
address does. Note the asymmetry deliberately: this scanner keeps `10/8`,
`192.168/16` and `172.16/12` as findings *because* a private address leaks
topology — so "not publicly routable" is not on its own a sufficient reason to
allow an address. The reason is the assumption above. Anyone reusing this
scanner in a network that routes these prefixes must drop the exemption.

Otherwise the exception is **narrow by construction**, and each property below
is pinned by a test in `installer/tests/test_leak_scan.py`:

- an address one octet outside a TEST-NET, or one prefix past the RFC 3849
  `/32`, is ordinary and still flagged;
- the RFC 1918 private ranges are still flagged;
- the IPv4-in-IPv6 forms still expose their payload: `::ffff:<quad>` is caught
  by the IPv4 detector reading the embedded quad, and the 6to4 and NAT64
  transition prefixes are not documentation ranges;
- an address `ipaddress` refuses to parse (a leading-zero octet) fails
  **closed** — it stays suspicious.

  A denylist entry is written once as a slug (`alpha-beta-gamma`) and is matched
  by three arms, in a **union** — a line hits if *any* arm hits:

  1. **Raw substring**, the original test, unchanged. Keeps affixed forms like
     `alpha-beta-gamma2` and `xalpha_beta_gamma` caught.
  2. **Whole-token match on canonicalised text.** Both entry and line are
     canonicalised: case transitions split (`AlphaBetaGamma`), a `.` or `/`
     *between two word characters* becomes a separator (`alpha.beta.gamma`,
     `src/alpha/beta/gamma.py`), each run of `-`, `_` or whitespace collapses to
     one space, and the result is lowercased.
  3. **Separator-free comparison against each alphanumeric token.** Case
     splitting cannot resolve an acronym boundary — it reads `OAuthProxy` as
     `O Auth Proxy` — so the entry with all separators removed
     (`alphabetagamma`) is also compared for equality against each token of the
     line. This is deliberately *token*-scoped: stripping the whole line would
     fuse `audit; mcp` into `auditmcp` and fire on ordinary sentences.

  So one entry covers `Alpha Beta Gamma`, `alpha_beta_gamma`, `Alpha-Beta_Gamma`,
  `AlphaBetaGamma`, `alpha.beta.gamma`, `src/alpha/beta/gamma.py`, `OAuthProxy`
  and `IPv6Gateway`.

  Canonicalisation only ever *adds* a token boundary; it never fuses two things
  that were separate. A `.` followed by whitespace stays sentence punctuation,
  so `…ends the audit. Core team…` does not fuse. Other punctuation is not a
  separator either: `beta; gamma` stays two tokens.

  Because arm 1 is retained verbatim, the matcher is **strictly additive**: it
  cannot lose a detection the older, separator-sensitive scanner made.
  Separator-sensitivity was a real false negative — a title-cased heading of a
  private product name once shipped in a public skill file while the hyphenated
  slug sat on the denylist.

  **Two over-matches are accepted on purpose.** A missed leak is permanent
  disclosure; a false positive costs one human glance. Both are true positives
  often enough to keep:

  - **The spaced form matches in running prose.** Entry `alpha-beta-gamma` fires
    on "the alpha beta gamma pipeline". Do not waive this as a scanner bug: an
    internal name written out in public prose *is* the leak, and it is the same
    string as the heading `# Alpha Beta Gamma`. No matcher catches one and not
    the other.
  - **Case transitions are token boundaries**, so a slug can fire inside an
    unrelated CamelCase word: entry `widget-x` matches `TheWidget x`. The
    all-lowercase `thewidget x` does not.

  **Residual risk — spellings this gate does *not* catch.** Deliberate, recorded
  here so a clean-room operator knows where human review still carries the load:

  - **Zero-width and encoded separators** — `alpha<U+200B>beta`, `alpha%20beta`,
    soft hyphens, homoglyphs. This gate detects *accidental disclosure of our
    own names*; it is not an adversarial-evasion barrier against someone who
    controls the text and wants past their own scanner. Pinned in the tests as
    known misses so the gap stays a decision rather than a surprise.
  - **Separators the canonicaliser does not know** — `alpha..beta` (neither dot
    sits between two word characters), a doubled colon between two words,
    `alpha\beta`, and a name broken by inline markup such as `**Alpha** Beta
    Gamma`. (The doubled-colon case is described rather than shown: a
    hex-lettered pair around `::` parses as an IPv6 address and would trip this
    scanner's own IP detector — the very trap the next section warns about.)
  - **Names split across a line wrap** — matching is line-scoped, and the spaced
    form is the one spelling a prose wrapper can break. Watch for it in
    prose-wrapped `.md` / `.txt`.
  - **Names not on the denylist at all.** The gate is only as complete as the
    injected list.

Nothing is excluded from the scan. `scan_paths()` has no exclusion mechanism at
all — a leak gate whose convenient entry point is the weak one is worse than no
gate. (An earlier version exempted this doc and the test file from the *no-arg*
scan only; a real key pasted into either then passed `leak_scan` while failing
`leak_scan .`. The exemption is gone.) The three files that must name forbidden
tokens on purpose — this doc, the test suite, and `leak_scan.py` — all scan
clean, because the tokens they name are ones the detectors allow (RFC
documentation addresses) or are assembled at run time in the tests.

The price is a rule this file obeys too: **an address the scanner flags may not
be written here as a literal.** Documentation-range addresses may (they are
allowed by definition); everything else is named in prose. Spelling out a
6to4-style prefix in full, for instance, would make that line a finding.

### Scan scope — where it must run

By default `leak_scan` walks **this `installer/` directory** (the shell source).
At bundle-assembly / clean-room time the scan must be re-run over the **whole
assembled bundle**, including the copied `decision-engine/skills/` and
`aqg/skills/` bodies — those come from other repos and are not covered by a
scan of `installer/` alone. Pass the bundle root explicitly:
`python3 -m installer.leak_scan <bundle-root>`.

Because the surface is baseline-clean, the exit code is now *usable*: it is `0`
on a pristine tree, so a non-zero result means the scan found something rather
than tripping over its own fixtures. That makes it a fit CI / pre-push check.

Be precise about what it proves. **Exit 0 means: no known-shape secret, IP
literal, or internal identifier appeared, line by line, in any scanned file
under the given roots** — the config / text / code formats where those live
(the doc/script suffixes plus `.yaml`/`.yml`/`.env`/`.cfg`/`.ini`/`.js`/`.ts`/
`.html` and kin), the well-known extensionless names (`Dockerfile`,
`Makefile`), and any *small* file that sniffs as text. A recognised suffix or
name is scanned at **any size**; the text sniff — the fallback for an
unrecognised type — is bounded, so a file **over 256 KiB with no recognised
suffix or name is left unopened** (an accepted residual: a config/secret file
is not that large, and every common config/code type has a suffix above). The
`.git` and `node_modules` subtrees, and binaries (a NUL byte in the first
4 KiB), are skipped. It is a necessary condition for publishing, not a
sufficient one — the reviewer and the transport-only structure (layer 1) are
the gate; this is the backstop.

Run it over a clean export — `git archive HEAD | tar -x -C "$tmp"` — not a
working tree: the walker descends into whatever is on disk, so an untracked
`.venv/` or scratch directory produces findings that are not in the bundle.

### What it does NOT catch

It does not detect a leaked prompt / layout / orchestration snippet **by
content** — there is no reliable regex for "this paragraph is a system prompt".
Nor does it flag an arbitrary **server hostname / FQDN**: any URL in docs is a
hostname, so a generic hostname denylist would be all false positives — an
endpoint hostname is caught instead by human review + the structural fact that
the shell hardcodes no endpoint (it comes from config at runtime). Those
classes are prevented structurally (layer 1) and confirmed by human review. The
scanner is the backstop for secrets / IP literals / internal identifiers, not a
substitute for the transport-only design or the reviewer.

Three mechanical gaps are known and accepted, all of them **evasions a
deliberate leaker could choose, not shapes an accident takes**:

- **Line-by-line matching.** `scan_text` iterates `splitlines()`, so a value
  split across two lines matches nothing.
- **Alternate IP encodings.** `_IPV4` matches dotted quads only; the integer,
  hex and octal spellings of an address are invisible.
- **The free bits under a documentation prefix.** `2001:db8::/32` leaves 96
  bits unconstrained, so an address can be hand-encoded there and allowed.

None is reachable by copy-pasting a config or a log line, which is the accident
this gate exists to catch. Someone who *wants* to exfiltrate a value out of a
text repository has unbounded options (base64 in a comment, and so on); no
regex scanner closes that, and this one does not pretend to.

## Latest result

```
$ python3 -m installer.leak_scan
leak-scan: clean (1 root(s))

$ git archive HEAD | tar -x -C "$tmp" && (cd "$tmp" && python3 -m installer.leak_scan .)
leak-scan: clean (1 root(s))          # exit 0 — the whole-tree gate

$ python3 -m unittest installer.tests.test_leak_scan
Ran 51 tests
OK
```

Manual review confirms: no real server address, no secret, no token, no private
prompt / layout / orchestration / pipeline, and no proprietary server-side UI
or ad-generation source in the public projection. The only server reference is
the abstract `/mcp` path appended to an owner-provided, config-sourced endpoint.

### A trap this matching creates

Separator-insensitive matching folds `-`, `_`, `.`, `/` and whitespace together, so a
denylist entry that reads as an ordinary English phrase once folded will match ordinary
English prose — *including this scanner's own documentation*. That is not a false positive
to be waived: an internal name written into public prose is exactly the leak this gate
exists to catch. It does mean two things for whoever maintains the denylist:

- Keep the scanner's own prose free of any phrase that folds onto a denylisted identifier.
  A name-shaped entry is fine; a description-shaped one will be noisy.
- Run the scan against the real denylist before a release. A gate that always fails is a
  gate everyone learns to ignore.

## Degrade-branch routing-prose gate (PR5, v5 §15)

Beyond the identifier / IP red line, `leak_scan` also asserts the `/audit`
**degrade-branch** prose (the `Degraded / hub-unreachable` section of
`skills/audit/references/de-lite-routing.md`) stays
**routing-prose-only** — no hub-proprietary orchestration IP. It is a best-effort
tripwire (the DE-side twin of the AQG-side `aqg-multi-review` leak gate), not a
cryptographic moat.

- **Scoped**: it reads ONLY the degrade section (its heading → the next
  same-or-higher heading), so the ordinary `### Adjudication` routing section is
  untouched.
- **Denylist targets hub SPECIFICS** — a voice roster (`gpt-5.x`/`o3`/`deepseek`/
  `qwen`), hub orchestration field names (`reply_prefix`/`adjudication_hint`/…),
  and adjudication-prompt fragments (`convergent vs divergent`) — matched over
  normalized text (folded case + separators). It deliberately does NOT flag the
  generic words "adjudication" / "roster", so the sanctioned negation ("the role
  prompts … live in that skill — never restate them here") scans clean.
- **Category-only + fail-closed**: a finding names the category, never the matched
  value; a degrade section that exists but cannot be read is a finding, not a pass.
