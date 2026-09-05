"""Retire Decision Engine's legacy block from Codex's global ``AGENTS.md``.

Decision Engine now routes Codex entirely through installed skills.  This module remains as a
one-way migration for installations that received the former managed block: it can remove only
that marker-delimited region, backup-first and with the existing atomic clobber guard.  It never
creates ``AGENTS.md`` and has no API that can add or refresh Decision Engine instructions.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from installer.config import ShellError
from installer import config
from installer.mcp_config import _atomic_write_text, _backup

_BEGIN = "<!-- BEGIN decision-engine routing (managed by installer.codex_routing; do not edit inside) -->"
_END = "<!-- END decision-engine routing -->"

# These four installed skills replace every routing decision formerly carried by the global block:
# visual explanation, interactive boards, defect review, and hypothesis-class review.
REPLACEMENT_SKILLS = (
    "graphic-explanation",
    "discussion-board",
    "audit",
    "audit-brainstorming",
)

# Old documentation also permitted copying the fragment without installer markers.  Never delete
# such user-owned prose automatically; Doctor uses these exact headings only to surface a warning.
_LEGACY_UNMARKED_SIGNATURES = (
    "## Graphic explanation — visually explain the current conversation / a decision / a concept",
    "## Audit Routing & Autonomous Continuation",
)


def agents_md_path() -> Path:
    """Return Codex's global instructions path, overridable for isolated tests."""

    return Path(
        os.getenv("CODEX_AGENTS_MD") or (Path.home() / ".codex" / "AGENTS.md")
    ).expanduser()


def _without_managed_block(original: str) -> str:
    """Remove the one well-formed DE block while preserving outside content."""

    begins, ends = original.count(_BEGIN), original.count(_END)
    if begins == 0 and ends == 0:
        return original
    if begins != 1 or ends != 1:
        raise ShellError(
            "~/.codex/AGENTS.md has a malformed decision-engine block; remove the "
            "duplicate or unmatched marker by hand"
        )
    begin = original.find(_BEGIN)
    end = original.find(_END)
    if end < begin:
        raise ShellError(
            "~/.codex/AGENTS.md has decision-engine markers in the wrong order; "
            "remove that block by hand"
        )
    return original[:begin] + original[end + len(_END) :]


def replacement_skills_ready(body_root: Path) -> bool:
    """Prove that Codex's four replacement routes point into ``body_root``."""

    if not config.codex_skills_in_use():
        return False
    skills_root = config.codex_skills_dir()
    body_root = Path(body_root)
    try:
        for name in REPLACEMENT_SKILLS:
            installed = (skills_root / name).resolve(strict=True)
            expected = (body_root / "skills" / name).resolve(strict=True)
            if installed != expected or not (installed / "SKILL.md").is_file():
                return False
    except (OSError, RuntimeError):
        return False
    return True


def has_unmarked_legacy_copy(text: str) -> bool:
    """Recognize an exact old copy without claiming ownership of it."""

    return all(signature in text for signature in _LEGACY_UNMARKED_SIGNATURES)


def retire_routing(
    *,
    dry_run: bool = False,
    require_skills_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Remove the legacy DE block without creating or otherwise rewriting the file."""

    path = agents_md_path()
    if require_skills_root is not None and not replacement_skills_ready(
        Path(require_skills_root)
    ):
        return {"path": str(path), "action": "deferred (skills unavailable)", "backup": None}
    if path.is_symlink():
        raise ShellError(
            "Codex AGENTS.md is a symbolic link; remove the legacy Decision Engine block "
            "from its managed target by hand"
        )
    if not path.exists():
        return {"path": str(path), "action": "unchanged", "backup": None}
    if not path.is_file():
        raise ShellError("Codex AGENTS.md path is not a regular file: %s" % path)

    mtime_ns = os.stat(path).st_mtime_ns
    original_bytes = path.read_bytes()
    original_digest = hashlib.sha256(original_bytes).hexdigest()
    original = original_bytes.decode("utf-8")
    retired = _without_managed_block(original)
    if retired == original:
        return {"path": str(path), "action": "unchanged", "backup": None}
    if dry_run:
        return {"path": str(path), "action": "removed (dry-run)", "backup": None}

    backup = _backup(path)
    _atomic_write_text(
        path,
        retired,
        expect_mtime_ns=mtime_ns,
        expect_sha256=original_digest,
        expect_exists=True,
        newline="",
    )
    return {"path": str(path), "action": "removed", "backup": str(backup)}


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="de-codex-routing",
        description="Remove Decision Engine's retired global Codex AGENTS.md block.",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument(
        "--require-skills-root",
        type=Path,
        help="defer automatic cleanup until the four replacement skill routes point into this root",
    )
    args = parser.parse_args(argv)
    try:
        result = retire_routing(
            dry_run=args.dry_run,
            require_skills_root=args.require_skills_root,
        )
    except (OSError, UnicodeError, ShellError) as exc:
        print("de-codex-routing: %s" % exc, file=sys.stderr)
        return 1
    note = "" if not result["backup"] else "  (backup: %s)" % result["backup"]
    print("de-codex-routing: %-17s %s%s" % (result["action"], result["path"], note))
    if result["action"] == "removed":
        print("de-codex-routing: retired — restart Codex to drop the old instructions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
