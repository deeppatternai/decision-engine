"""Which agent hosts show evidence that Decision Engine was already onboarded into them.

``mcp_config.detect_clients`` answers the loose question "does this agent look installed on
this machine". That looseness is deliberate for a *write* target list, but it is too weak for
a *diagnostic*: a merely-present host that carries no Decision Engine payload is not a broken
install, so Doctor has always reported such a host as an informational "not wired" note.

This module answers the stricter question — "has Decision Engine already been onboarded into
this host?" — from per-host evidence: an installed DE skill payload and the host's own
directory. A host with that evidence
but WITHOUT an MCP entry is a genuinely broken state: its skills tell the agent to call
``mcp__decision-engine__*`` tools that its own config never registered.

Codex, Claude Code, and Cursor can all drift because their skills live
outside their MCP config. Claude Desktop takes no skills at all, so it has no probes: a missing
entry there is a choice, not a contradiction. The registry is keyed by host, so a new one joins
by adding a row.

Read-only, individually guarded, and never raising: every probe answers "no evidence" rather
than propagating a filesystem or parse failure into the caller's diagnostic. Reports host names
and evidence CATEGORIES only — never paths, commands, tokens, or configuration contents.
Stdlib only.
"""

from __future__ import annotations

from functools import partial
from typing import Callable, Dict, Iterable, Optional, Tuple

from installer import config, mcp_config
from installer.config import ShellError

# Evidence categories. PRESENCE means only "this host exists here"; PAYLOAD means Decision
# Engine content was actually installed into it, which is the stronger claim a diagnostic may
# make ("skills installed") and must not make on presence alone.
PRESENCE_EVIDENCE = "host directory"
SKILL_EVIDENCE = "skill route"
PAYLOAD_EVIDENCE = (SKILL_EVIDENCE,)


def _skill_payload_present(client: str) -> bool:
    """DE skills were actually routed into ``client``'s skills directory.

    Deliberately not ``active_skill_routes``: that answers where skills WOULD go, which is
    intent, not payload — an explicitly configured skills directory counts as in use before
    anything is installed, and Cursor's Doctor-side predicate is gated on its MCP entry already
    existing, which is exactly the entry this probe exists to find missing. So the host's skills
    directory is intersected with the names in the installed payload (the same payload
    ``check_skills`` routes from), and only a name actually present in the host counts.
    """
    spec = mcp_config.CLIENT_SPECS[client]
    if spec.skills_global_path is None:
        return False
    destination = spec.skills_global_path()
    payload = config.de_config_path().parent / "skills"
    if not destination.is_dir() or not payload.is_dir():
        return False
    return any(
        (destination / entry.name).exists()
        for entry in payload.iterdir()
        if entry.is_dir() and entry.name not in spec.excluded_skills
    )


_EVIDENCE_LABELS = {
    "presence": PRESENCE_EVIDENCE,
    "skill": SKILL_EVIDENCE,
}


def _build_onboarding_probes(specs):
    """Build read-only evidence probes entirely from registered host metadata."""

    mcp_config.validate_host_specs(dict(specs))
    probes: Dict[str, Tuple[Tuple[str, Callable[[], bool]], ...]] = {}
    for client, spec in specs.items():
        if not spec.onboarding_evidence:
            continue
        if "mcp-entry" not in spec.doctor_capabilities:
            raise ShellError(
                "onboarding host %r does not register an MCP entry" % client
            )
        entries = []
        for kind in spec.onboarding_evidence:
            label = _EVIDENCE_LABELS.get(kind)
            if label is None:
                raise ShellError(
                    "onboarding host %r declares an unknown evidence category" % client
                )
            if kind == "presence":
                probe = partial(mcp_config.client_present, client)
            else:
                if spec.skills_global_path is None:
                    raise ShellError(
                        "onboarding host %r claims skill evidence without a skills path"
                        % client
                    )
                probe = partial(_skill_payload_present, client)
            if not callable(probe):
                raise ShellError(
                    "onboarding host %r has invalid evidence metadata" % client
                )
            entries.append((label, probe))
        probes[client] = tuple(entries)

    return probes


ONBOARDING_PROBES = _build_onboarding_probes(mcp_config.CLIENT_SPECS)


def _validate_probes(
    probes: Optional[Dict[str, Tuple[Tuple[str, Callable[[], bool]], ...]]] = None,
) -> None:
    """Fail closed when the probe registry drifts from the host registry.

    Two directions, both at import time. Downward: every registered entry must name a real
    host that registers an MCP entry, and declare only categories it can actually produce.
    Upward: every host that DELIVERS SKILLS must be registered, because that is precisely the
    host shape this module exists for — skills in one place, the MCP entry in another, free to
    drift apart silently. A new host (say a Cursor-like IDE) that ships skills and forgets a
    probe would otherwise reintroduce the exact gap Codex shipped with, and nothing would say
    so; here it cannot be imported at all.
    """

    registry = ONBOARDING_PROBES if probes is None else probes
    for client, entries in registry.items():
        spec = mcp_config.CLIENT_SPECS.get(client)
        if spec is None:
            raise ShellError("unknown onboarding host %r" % client)
        if "mcp-entry" not in spec.doctor_capabilities:
            raise ShellError(
                "onboarding host %r does not register an MCP entry" % client
            )
        if not entries or any(not callable(probe) for _label, probe in entries):
            raise ShellError("onboarding host %r has invalid evidence metadata" % client)
        known = {PRESENCE_EVIDENCE, *PAYLOAD_EVIDENCE}
        labels = {label for label, _probe in entries}
        if not labels.issubset(known):
            raise ShellError(
                "onboarding host %r declares an unknown evidence category" % client
            )
        if SKILL_EVIDENCE in labels and spec.skills_global_path is None:
            raise ShellError(
                "onboarding host %r claims skill evidence without a skills path" % client
            )

    for client, spec in mcp_config.CLIENT_SPECS.items():
        if spec.skill_delivery_mode == "none" or "mcp-entry" not in spec.doctor_capabilities:
            continue
        declared = {label for label, _probe in registry.get(client, ())}
        if SKILL_EVIDENCE not in declared:
            raise ShellError(
                "host %r delivers skills but declares no %r evidence — its skills could land "
                "without an MCP entry and no check would say so" % (client, SKILL_EVIDENCE)
            )


_validate_probes()


def onboarding_evidence(client: str) -> Tuple[str, ...]:
    """Evidence categories showing ``client`` was onboarded, in registry order.

    Empty for a host with no probes (its mere presence is not evidence) and for a host whose
    probes all come back negative. One failing probe never hides the others.
    """
    evidence = []
    for label, probe in ONBOARDING_PROBES.get(client, ()):
        try:
            satisfied = probe()
        except Exception:  # aqg: top-level boundary — an unreadable host is simply no evidence
            continue
        if satisfied:
            evidence.append(label)
    return tuple(evidence)


def is_onboarded(client: str) -> bool:
    """Was Decision Engine already onboarded into ``client``?"""
    return bool(onboarding_evidence(client))


def has_payload_evidence(evidence: Iterable[str]) -> bool:
    """Does ``evidence`` show installed Decision Engine content, not just a present host?"""
    return any(label in PAYLOAD_EVIDENCE for label in evidence)


def evidence_phrase(evidence: Iterable[str]) -> str:
    """How a diagnostic may describe ``evidence`` in one clause.

    Only claim installed Decision Engine skills when a payload route was actually found.
    """
    labels = frozenset(evidence)
    if SKILL_EVIDENCE in labels:
        return "skills installed"
    return "in use"


def onboarding_report(
    clients: Optional[Iterable[str]] = None,
) -> Dict[str, Tuple[str, ...]]:
    """Evidence per onboarded host among ``clients`` (default: every probed host).

    Registry order; hosts with no evidence are omitted. One pass over the probes, so a caller
    can classify presence-only against payload evidence without probing the disk twice.
    """
    candidates = ONBOARDING_PROBES if clients is None else set(clients)
    report = {}
    for client in mcp_config.CLIENT_SPECS:
        if client not in candidates:
            continue
        evidence = onboarding_evidence(client)
        if evidence:
            report[client] = evidence
    return report


def onboarded_clients(clients: Optional[Iterable[str]] = None) -> Tuple[str, ...]:
    """Onboarded hosts among ``clients`` (default: every host with probes), registry order.

    Callers pass the hosts they are already reasoning about. Probing every host by default
    reads the live filesystem, so a caller that has narrowed the set (Doctor: the hosts whose
    entry came back ``absent``) should say so rather than re-widening it here.
    """
    return tuple(onboarding_report(clients))
