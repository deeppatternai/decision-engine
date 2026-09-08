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
