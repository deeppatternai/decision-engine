"""Owned WorkBuddy AI prompt-hook configuration lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from installer.client_hosts.hosts import command_prompt_hook


MANAGED_ID = "decision-engine-workbuddy-ai-audit-routing-v1"
_SPEC = command_prompt_hook.CommandPromptHookSpec(
    managed_id=MANAGED_ID,
    script_suffix="/installer/workbuddy_audit_prompt_hook.py",
    host_label="WorkBuddy AI",
    error_prefix="workbuddy_ai",
)


def is_owned_hook_group(group: object) -> bool:
    return command_prompt_hook.is_owned_hook_group(_SPEC, group)


def merge_prompt_hook(
    data: dict[str, Any], *, de_root: Path, python_executable: str
) -> bool:
    return command_prompt_hook.merge_prompt_hook(
        _SPEC,
        data,
        de_root=de_root,
        python_executable=python_executable,
    )


def remove_prompt_hook(data: dict[str, Any]) -> bool:
    return command_prompt_hook.remove_prompt_hook(_SPEC, data)


def install_hook(
    settings_path: Path,
    *,
    de_root: Path,
    python_executable: str,
    dry_run: bool = False,
) -> dict[str, object]:
    return command_prompt_hook.install_hook(
        _SPEC,
        settings_path,
        de_root=de_root,
        python_executable=python_executable,
        dry_run=dry_run,
    )


def remove_owned_hook(
    settings_path: Path, *, dry_run: bool = False
) -> dict[str, object]:
    return command_prompt_hook.remove_owned_hook(
        _SPEC, settings_path, dry_run=dry_run
    )
