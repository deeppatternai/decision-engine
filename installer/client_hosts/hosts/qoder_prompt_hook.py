"""Owned macOS Qoder prompt-hook configuration lifecycle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from installer.config import ShellError


MANAGED_HOOK_NAME = "decision-engine-audit-routing-v1"
LEGACY_EXPERIMENT_NAME = "decision-engine-audit-routing-experiment"
_HOOK_SCRIPT_SUFFIX = "/installer/qoder_audit_prompt_hook.py"


def _hook_group(*, de_root: Path, python_executable: str) -> dict[str, object]:
    script = Path(de_root) / "installer" / "qoder_audit_prompt_hook.py"
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": python_executable,
                "args": [str(script)],
                "name": MANAGED_HOOK_NAME,
                "statusMessage": "Routing Decision Engine audit request...",
            }
        ],
    }


def _named_hook(group: object) -> dict[str, object] | None:
    if not isinstance(group, dict) or group.get("matcher") != "":
        return None
    hooks = group.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1:
        return None
    hook = hooks[0]
    return hook if isinstance(hook, dict) else None


def _hook_script_is_owned(hook: dict[str, object]) -> bool:
    args = hook.get("args")
    return (
        hook.get("type") == "command"
        and isinstance(hook.get("command"), str)
        and bool(str(hook["command"]).strip())
        and isinstance(args, list)
        and len(args) == 1
        and isinstance(args[0], str)
        and args[0].replace("\\", "/").endswith(_HOOK_SCRIPT_SUFFIX)
    )


def is_owned_hook_group(group: object) -> bool:
    hook = _named_hook(group)
    return bool(
        hook
        and hook.get("name") in {MANAGED_HOOK_NAME, LEGACY_EXPERIMENT_NAME}
        and _hook_script_is_owned(hook)
    )


def _prompt_groups(data: dict[str, Any], *, create: bool) -> list[Any] | None:
    hooks = data.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = {}
        data["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ShellError("qoder_hooks_invalid: 'hooks' must be an object")
    groups = hooks.get("UserPromptSubmit")
    if groups is None:
        if not create:
            return None
        groups = []
        hooks["UserPromptSubmit"] = groups
    if not isinstance(groups, list):
        raise ShellError(
            "qoder_hooks_invalid: hooks.UserPromptSubmit must be an array"
        )
    return groups


def merge_prompt_hook(
    data: dict[str, Any], *, de_root: Path, python_executable: str
) -> bool:
    """Merge the owned prompt hook while preserving unrelated groups."""
    groups = _prompt_groups(data, create=True)
    assert groups is not None
    desired = _hook_group(
        de_root=Path(de_root), python_executable=python_executable
    )
    owned_indexes = []
    for index, group in enumerate(groups):
        hook = _named_hook(group)
        if hook is None:
            continue
        name = hook.get("name")
        if name == MANAGED_HOOK_NAME and not is_owned_hook_group(group):
            raise ShellError(
                "same_name_unowned: the Qoder Decision Engine audit hook is not "
                "owned by this install; nothing was written"
            )
        if is_owned_hook_group(group):
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


def merge_prompt_hook_from_entry(
    data: dict[str, Any], entry: dict[str, Any]
) -> bool:
    args = entry.get("args")
    if (
        not isinstance(entry.get("command"), str)
        or not isinstance(args, list)
        or len(args) < 2
        or not isinstance(args[1], str)
    ):
        raise ShellError("rendered Qoder entry cannot identify its DE root")
    return merge_prompt_hook(
        data,
        de_root=Path(args[1]),
        python_executable=entry["command"],
    )


def remove_prompt_hook(data: dict[str, Any]) -> bool:
    """Remove only a provably DE-owned prompt-hook group."""
    groups = _prompt_groups(data, create=False)
    if groups is None:
        return False
    retained = []
    changed = False
    for group in groups:
        hook = _named_hook(group)
        if (
            hook is not None
            and hook.get("name") == MANAGED_HOOK_NAME
            and not is_owned_hook_group(group)
        ):
            raise ShellError(
                "same_name_unowned: the Qoder Decision Engine audit hook is not "
                "owned by this install; nothing was written"
            )
        if is_owned_hook_group(group):
            changed = True
        else:
            retained.append(group)
    if changed:
        groups[:] = retained
    return changed


def _load_settings(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        return {}, b""
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ShellError("refusing to parse oversized Qoder settings")
    try:
        loaded = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShellError("refusing to write invalid Qoder settings JSON") from exc
    if not isinstance(loaded, dict):
        raise ShellError("refusing to write non-object Qoder settings JSON")
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
    settings_path: Path,
    *,
    de_root: Path,
    python_executable: str,
    dry_run: bool = False,
) -> dict[str, object]:
    path = Path(settings_path)
    data, original = _load_settings(path)
    changed = merge_prompt_hook(
        data, de_root=de_root, python_executable=python_executable
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
    settings_path: Path, *, dry_run: bool = False
) -> dict[str, object]:
    path = Path(settings_path)
    data, original = _load_settings(path)
    changed = remove_prompt_hook(data)
    if not changed:
        return {"action": "unchanged", "path": str(path), "backup": None}
    if dry_run:
        return {"action": "removed (dry-run)", "path": str(path), "backup": None}
    return {
        "action": "removed",
        "path": str(path),
        "backup": _write_settings(path, data, original),
    }
