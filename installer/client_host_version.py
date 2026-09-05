"""Host-neutral version-floor evidence and evaluation.

Collectors are host adapters.  This module only accepts already-normalized,
non-sensitive evidence and makes the fail-closed minimum-version decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple


_NUMERIC_VERSION = re.compile(
    r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})"
    r"(?:\.(?:0|[1-9][0-9]{0,5}))?\Z"
)


@dataclass(frozen=True, order=True)
class NumericVersion:
    """A strict two- or three-component numeric product version."""

    major: int
    minor: int
    patch: int = 0

    @classmethod
    def parse(cls, value: str) -> "NumericVersion":
        if not isinstance(value, str) or not _NUMERIC_VERSION.fullmatch(value):
            raise ValueError("version must be a bounded numeric product version")
        parts = tuple(int(part) for part in value.split("."))
        if len(parts) == 2:
            parts += (0,)
        return cls(*parts)

    def __str__(self) -> str:
        return "%d.%d.%d" % (self.major, self.minor, self.patch)


@dataclass(frozen=True)
class HostVersionEvidence:
    """Normalized evidence with no paths, commands, or package payloads."""

    selected: Optional[NumericVersion]
    installed: Tuple[NumericVersion, ...]
    installations: int
    scope_complete: bool


@dataclass(frozen=True)
class VersionFloorAssessment:
    state: str  # "floor_met" | "unsupported" | "unknown"


def assess_version_floor(
    evidence: HostVersionEvidence,
    minimum: NumericVersion,
) -> VersionFloorAssessment:
    """Evaluate versions within the collector's explicitly declared scope.

    A floor can be declared met only when that scoped inventory is complete, a
    selected host maps back to it, and every scoped version is at or above the
    candidate floor.  This does not claim an exhaustive machine inventory.
    """

    installed = tuple(evidence.installed)
    if (
        evidence.installations < len(set(installed))
        or (
            evidence.selected is not None
            and evidence.selected not in installed
        )
    ):
        return VersionFloorAssessment("unknown")
    if (
        evidence.selected is not None
        and evidence.selected < minimum
    ) or any(
        version < minimum for version in installed
    ):
        return VersionFloorAssessment("unsupported")
    if (
        not evidence.scope_complete
        or evidence.selected is None
        or not installed
    ):
        return VersionFloorAssessment("unknown")
    return VersionFloorAssessment("floor_met")
