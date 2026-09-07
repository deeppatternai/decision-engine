"""Owned command-style UserPromptSubmit hook lifecycle."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from installer.config import ShellError


@dataclass(frozen=True)
class CommandPromptHookSpec:
    managed_id: str
    script_suffix: str
    host_label: str
    error_prefix: str
    timeout: int = 30


def _hook_command(
    spec: CommandPromptHookSpec, *, de_root: Path, python_executable: str
) -> str:
    script = Path(de_root) / spec.script_suffix.removeprefix("/")
    return shlex.join(
        [python_executable, str(script), "--managed-id", spec.managed_id]
    )


def _hook_group(
    spec: CommandPromptHookSpec, *, de_root: Path, python_executable: str
) -> dict[str, object]:
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": _hook_command(
                    spec, de_root=de_root, python_executable=python_executable
                ),
                "timeout": spec.timeout,
            }
        ],
    }


def _single_command_hook(group: object) -> dict[str, object] | None:
    if not isinstance(group, dict) or group.get("matcher") != "":
        return None
    hooks = group.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1:
        return None
    hook = hooks[0]
    return hook if isinstance(hook, dict) else None


def _owned_command(spec: CommandPromptHookSpec, command: object) -> bool:
    if not isinstance(command, str) or spec.managed_id not in command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return (
        len(tokens) == 4
        and bool(tokens[0])
        and tokens[1].replace("\\", "/").endswith(spec.script_suffix)
        and tokens[2:] == ["--managed-id", spec.managed_id]
    )


def is_owned_hook_group(spec: CommandPromptHookSpec, group: object) -> bool:
    hook = _single_command_hook(group)
    return bool(
        hook
        and hook.get("type") == "command"
        and hook.get("timeout") == spec.timeout
        and _owned_command(spec, hook.get("command"))
    )


def _prompt_groups(
    spec: CommandPromptHookSpec, data: dict[str, Any], *, create: bool
) -> list[Any] | None:
    hooks = data.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = {}
        data["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ShellError(f"{spec.error_prefix}_hooks_invalid: 'hooks' must be an object")
    groups = hooks.get("UserPromptSubmit")
    if groups is None:
        if not create:
            return None
        groups = []
        hooks["UserPromptSubmit"] = groups
    if not isinstance(groups, list):
        raise ShellError(
            f"{spec.error_prefix}_hooks_invalid: hooks.UserPromptSubmit must be an array"
        )
    return groups


def merge_prompt_hook(
    spec: CommandPromptHookSpec,
    data: dict[str, Any],
    *,
    de_root: Path,
    python_executable: str,
) -> bool:
    groups = _prompt_groups(spec, data, create=True)
    assert groups is not None
    desired = _hook_group(
        spec, de_root=Path(de_root), python_executable=python_executable
    )
    owned_indexes: list[int] = []
    for index, group in enumerate(groups):
        hook = _single_command_hook(group)
        command = hook.get("command") if hook is not None else None
        if isinstance(command, str) and spec.managed_id in command:
            if not is_owned_hook_group(spec, group):
                raise ShellError(
                    f"same_id_unowned: the {spec.host_label} Decision Engine audit "
                    "hook is not owned by this install; nothing was written"
                )
            owned_indexes.append(index)
    if len(owned_indexes) == 1 and groups[owned_indexes[0]] == desired:
        return False
    insertion = owned_indexes[0] if owned_indexes else len(groups)
    retained = [
        group for index, group in enumerate(groups) if index not in owned_indexes
    ]
    retained.insert(insertion, desired)
    groups[:] = retained
    return True


def remove_prompt_hook(
    spec: CommandPromptHookSpec, data: dict[str, Any]
) -> bool:
    groups = _prompt_groups(spec, data, create=False)
    if groups is None:
        return False
    retained = []
    changed = False
    for group in groups:
        hook = _single_command_hook(group)
        command = hook.get("command") if hook is not None else None
        if isinstance(command, str) and spec.managed_id in command:
            if not is_owned_hook_group(spec, group):
                raise ShellError(
                    f"same_id_unowned: the {spec.host_label} Decision Engine audit "
                    "hook is not owned by this install; nothing was removed"
                )
            changed = True
        else:
            retained.append(group)
    if changed:
        groups[:] = retained
    return changed


def _load_settings(
    spec: CommandPromptHookSpec, path: Path
) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        return {}, b""
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ShellError(f"refusing to parse oversized {spec.host_label} settings")
    try:
        loaded = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShellError(
            f"refusing to write invalid {spec.host_label} settings JSON"
        ) from exc
    if not isinstance(loaded, dict):
        raise ShellError(
            f"refusing to write non-object {spec.host_label} settings JSON"
        )
    return loaded, raw


def _write_settings(path: Path, data: dict[str, Any], original: bytes) -> str | None:
    from installer import mcp_config

    existed = path.is_file()
    mtime_ns = path.stat().st_mtime_ns if existed else None
    digest = hashlib.sha256(original).hexdigest() if existed else None
    backup = mcp_config._backup(path) if existed else None
    mcp_config._atomic_write_text(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        expect_mtime_ns=mtime_ns,
        expect_sha256=digest,
        expect_exists=existed,
    )
    return str(backup) if backup is not None else None


def install_hook(
    spec: CommandPromptHookSpec,
    settings_path: Path,
    *,
    de_root: Path,
    python_executable: str,
    dry_run: bool = False,
) -> dict[str, object]:
    path = Path(settings_path)
    data, original = _load_settings(spec, path)
    changed = merge_prompt_hook(
        spec, data, de_root=de_root, python_executable=python_executable
    )
    if not changed:
        return {"action": "unchanged", "path": str(path), "backup": None}
    if dry_run:
        return {"action": "updated (dry-run)", "path": str(path), "backup": None}
    return {
        "action": "updated",
        "path": str(path),
        "backup": _write_settings(path, data, original),
    }


def remove_owned_hook(
    spec: CommandPromptHookSpec, settings_path: Path, *, dry_run: bool = False
) -> dict[str, object]:
    path = Path(settings_path)
    data, original = _load_settings(spec, path)
    changed = remove_prompt_hook(spec, data)
    if not changed:
        return {"action": "unchanged", "path": str(path), "backup": None}
    if dry_run:
        return {"action": "removed (dry-run)", "path": str(path), "backup": None}
    return {
        "action": "removed",
        "path": str(path),
        "backup": _write_settings(path, data, original),
    }
