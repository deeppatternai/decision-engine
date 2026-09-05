#!/usr/bin/env python3
"""Single-command version sync for the release process.

`VERSION` is the source of truth. This propagates it to every derived marker so a release bumps
one file (or passes a new version) and nothing drifts — including the README version badge, which
would otherwise be hand-edited and go stale.

    python3 scripts/set_version.py            # sync all markers TO the current VERSION file
    python3 scripts/set_version.py 0.3.0      # set VERSION + all markers to 0.3.0
    python3 scripts/set_version.py --check     # verify everything already agrees (exit 1 on drift)

Markers kept in sync (all `X.Y.Z`):
  VERSION · client/version.py (CLIENT_VERSION) · pyproject.toml (version) ·
  installer/config.example.json (client_version) · README.md + README.zh-CN.md (the `**vX.Y.Z**` badge)

NOT touched: installer/__init__.py SHELL_VERSION — the shell/installer's own version namespace,
independent of the client version the device reports.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _read_version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


# (path, compiled pattern with ONE capturing group around the version, template using {v})
def _edits(v: str):
    return [
        (ROOT / "VERSION", re.compile(r"^\s*\d+\.\d+\.\d+\s*$"), v + "\n", True),
        (ROOT / "client" / "version.py",
         re.compile(r'(?m)^(CLIENT_VERSION\s*=\s*")\d+\.\d+\.\d+(")'), r"\g<1>%s\g<2>" % v, False),
        (ROOT / "pyproject.toml",
         re.compile(r'(?m)^(version\s*=\s*")\d+\.\d+\.\d+(")'), r"\g<1>%s\g<2>" % v, False),
        (ROOT / "installer" / "config.example.json",
         re.compile(r'("client_version"\s*:\s*")\d+\.\d+\.\d+(")'), r"\g<1>%s\g<2>" % v, False),
        (ROOT / "README.md",
         re.compile(r"(\*\*v)\d+\.\d+\.\d+(\*\*)"), r"\g<1>%s\g<2>" % v, False),
        (ROOT / "README.zh-CN.md",
         re.compile(r"(\*\*v)\d+\.\d+\.\d+(\*\*)"), r"\g<1>%s\g<2>" % v, False),
    ]


def apply(v: str, check: bool) -> int:
    drift = []
    for path, pat, repl, whole in _edits(v):
        text = path.read_text(encoding="utf-8")
        new = repl if whole else pat.sub(repl, text)
        if not whole and pat.search(text) is None:
            drift.append("%s: no version marker found (badge/field missing?)" % path.name)
            continue
        if new != text:
            if check:
                drift.append("%s: out of sync (should be %s)" % (path.name, v))
            else:
                path.write_text(new, encoding="utf-8")
                print("updated %s → %s" % (path.relative_to(ROOT), v))
    if check:
        if drift:
            print("version drift:", file=sys.stderr)
            for d in drift:
                print("  " + d, file=sys.stderr)
            return 1
        print("version: all markers agree at %s" % v)
    return 0 if not drift else 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    argv = [a for a in argv if a != "--check"]
    if argv:
        v = argv[0].lstrip("v")
        if not _SEMVER.match(v):
            print("usage: set_version.py [X.Y.Z] [--check]", file=sys.stderr)
            return 2
    else:
        v = _read_version()
    return apply(v, check)


if __name__ == "__main__":
    raise SystemExit(main())
