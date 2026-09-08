"""All DE AQG clone entrances must preserve future signed-update hook bytes."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("entry", ["install.sh", "dp-install.sh", "de-aqg-install", "dp-install.ps1"])
def test_aqg_clone_preserves_bytes_with_global_crlf_conversion(tmp_path, entry):
    if entry == "dp-install.ps1" and os.name != "nt":
        pytest.skip("PowerShell bootstrap clone runs on native Windows")
    origin, dest = tmp_path / "origin", tmp_path / "installed"
    origin.mkdir()
    config = tmp_path / "gitconfig"
    config.write_text("[core]\n\tautocrlf = true\n\teol = crlf\n", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}
    def git(*args):
        subprocess.run(["git", "-C", str(origin), *args], env=env, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.autocrlf", "false")
    (origin / "scripts").mkdir()
    payload = b"#!/usr/bin/env bash\nexit 0\n"
    (origin / "scripts/install.sh").write_bytes(payload)
    (origin / "VERSION").write_bytes(b"0.14.3\n")
    git("add", ".")
    git("commit", "-qm", "fixture")
    source = (REPO / entry).read_text(encoding="utf-8")
    if entry == "dp-install.ps1":
        # This entrance clones DE first, then delegates AQG to install.sh.
        # Execute its actual clone expression with only the process wrapper
        # replaced, so global CRLF defaults exercise the shipped arguments.
        start = source.index("$clone = Invoke-WithCleanEnvironment")
        clone = source[start:source.index("if ($clone -ne 0)", start)]
        code = """
function Invoke-WithCleanEnvironment {
    param($FilePath, $ArgumentList)
    & $FilePath @ArgumentList 2>&1 | Out-Null
    return $LASTEXITCODE
}
$GitPath = (Get-Command git.exe).Source
$DecisionEngineRepository = $env:AQG_REPO
$sourceRoot = $env:AQG_DEST
""" + clone + "\nexit $clone\n"
    elif entry == "install.sh":
        start = source.index("install_aqg_body() {")
        code = source[start:source.index("\n}\n", start) + 3]
        code += "\nrun_aqg_installer() { :; }\ninstall_aqg_body\n"
    else:
        # Execute the shipped clone command; no DE activation or network.
        matches = re.findall(r'if ! (clean_exec env GIT_TERMINAL_PROMPT=0 "\$GIT_BIN" clone\s+[^;]*?"\$AQG_REPO" "\$AQG_ROOT"); then', source, re.S)
        assert matches, entry
        code = 'clean_exec() { "$@"; }\n' + matches[-1] + "\n"
    env.update(AQG_REPO=origin.as_posix(), AQG_DEST=dest.as_posix(),
               AQG_ROOT=dest.as_posix(), AQG_REF="main", GIT_BIN="git")
    command = ([shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", code]
               if entry == "dp-install.ps1" else ["bash", "--noprofile", "--norc", "-c", code])
    result = subprocess.run(command,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (dest / "scripts/install.sh").read_bytes() == payload


@pytest.mark.parametrize("entry", ["install.sh", "dp-install.sh"])
def test_aqg_update_pins_local_line_endings_before_git_operation(tmp_path, entry):
    origin, dest = tmp_path / "origin", tmp_path / "installed"
    origin.mkdir()
    config = tmp_path / "gitconfig"
    config.write_text("[core]\n\tautocrlf = true\n\teol = crlf\n", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}

    def git(root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], env=env, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.name", "Fixture")
    git(origin, "config", "user.email", "fixture@example.invalid")
    git(origin, "config", "commit.gpgsign", "false")
    git(origin, "config", "core.autocrlf", "false")
    (origin / "scripts").mkdir()
    for name in (
        "AI_SETUP.md",
        "scripts/install.sh",
        "scripts/install_aqg_clients.py",
        "scripts/aqg_doctor.py",
    ):
        path = origin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "fixture")
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(dest)], env=env, check=True,
        capture_output=True,
    )
    git(dest, "remote", "set-url", "origin", origin.as_posix())
    subprocess.run(
        ["git", "-C", str(dest), "config", "--unset-all", "core.autocrlf"],
        env=env, check=False, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(dest), "config", "--unset-all", "core.eol"],
        env=env, check=False, capture_output=True,
    )

    source = (REPO / entry).read_text(encoding="utf-8")
    if entry == "install.sh":
        start = source.index("install_aqg_body() {")
        code = source[start:source.index("\n}\n", start) + 3]
        code += "\nrun_aqg_installer() { :; }\ninstall_aqg_body\n"
    else:
        names = ("fail", "clean_exec", "verify_aqg_checkout", "sync_aqg_checkout")
        functions = []
        for name in names:
            match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", source, re.M | re.S)
            assert match is not None, name
            functions.append(match.group())
        code = "\n".join([
            "set -euo pipefail", "EXIT_USAGE=2", "PROGRAM_NAME=fixture-installer",
            "tty_print() { :; }", *functions, "sync_aqg_checkout",
        ])
    env.update(
        AQG_REPO=origin.as_posix(), AQG_DEST=dest.as_posix(),
        AQG_ROOT=dest.as_posix(), AQG_REF="main", GIT_BIN="git",
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", code], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(dest, "config", "--local", "--get", "core.autocrlf") == "false"
    assert git(dest, "config", "--local", "--get", "core.eol") == "lf"
