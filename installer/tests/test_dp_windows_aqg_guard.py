"""Execute only the native bootstrapper's AQG guard, without downloading or activation."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from installer.tests.test_de_aqg_install import (
    AQG_REPO,
    SHA,
    _checkout,
    _git,
    _link,
    _managed_checkout,
    _managed_release_checkout,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows bootstrapper")


@pytest.fixture
def verify(tmp_path):
    source = (ROOT / "dp-install.ps1").read_text(encoding="utf-8")
    functions = re.findall(r"^function .*?^\}", source, re.M | re.S)
    script = tmp_path / "guard.ps1"
    script.write_text("\n".join([
        "$ErrorActionPreference = 'Stop'", "Set-StrictMode -Version Latest",
        "$ProgramName = 'fixture-installer'", "$ExitUsage = 2", "$ExitBlocked = 3",
        "$AqgRoot = $env:FIXTURE_AQG_ROOT", f"$AqgRepository = '{AQG_REPO}'",
        "$GitPath = $env:FIXTURE_GIT", "$script:PythonPath = $env:FIXTURE_PYTHON",
        "$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)",
        *functions, "$null = Get-VerifiedAqgLayout",
    ]), encoding="utf-8-sig")

    def run(root):
        return subprocess.run(
            [shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(script)],
            env={
                **os.environ,
                "FIXTURE_AQG_ROOT": str(root),
                "FIXTURE_GIT": shutil.which("git.exe"),
                "FIXTURE_PYTHON": sys.executable,
            },
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    return run


@pytest.mark.parametrize("kind", ["checkout", "migrated", "worktree", "release"])
def test_native_guard_accepts_installed_layouts(verify, tmp_path, kind):
    root = tmp_path / "profile with spaces" / ".deeppattern" / "agent-quality-gates"
    if kind == "checkout":
        _checkout(root)
    elif kind == "release":
        target, _sha = _managed_release_checkout(root.parent)
        _link(root, target)
    else:
        source, sha = _managed_checkout(tmp_path / "managed-source")
        target = root.parent / "versions" / sha
        target.parent.mkdir(parents=True)
        if kind == "migrated":
            source.rename(target)
        else:
            _git(source, "worktree", "add", "--detach", str(target), sha)
        _link(root, target)
    before = _git(root, "rev-parse", "HEAD")
    for _ in range(2):
        result = verify(root)
        assert result.returncode == 0, result.stderr
        assert _git(root, "rev-parse", "HEAD") == before


@pytest.mark.parametrize("kind", ["outside", "nested-link", "dirty", "foreign-origin"])
def test_native_guard_preserves_unowned_or_modified_installs(verify, tmp_path, kind):
    root = tmp_path / ".deeppattern" / "agent-quality-gates"
    target, _commit = _managed_checkout(root.parent)
    if kind == "outside":
        target = _checkout(tmp_path / "outside")
    elif kind == "nested-link":
        outside = tmp_path / "outside"
        target.rename(outside)
        _link(target, outside)
    elif kind == "dirty":
        (target / "personal.txt").write_text("preserve me", encoding="utf-8")
    else:
        _git(target, "remote", "set-url", "origin", "https://example.test/foreign.git")
    _link(root, target)
    before = root.readlink()
    result = verify(root)
    assert result.returncode == 3, result.stderr
    assert root.readlink() == before
    assert (root / "AI_SETUP.md").is_file()


def test_native_guard_rejects_version_directory_that_does_not_match_head(
    verify, tmp_path,
):
    root = tmp_path / ".deeppattern" / "agent-quality-gates"
    target = _checkout(root.parent / "versions" / SHA)
    assert _git(target, "rev-parse", "HEAD") != SHA
    _link(root, target)

    result = verify(root)

    assert result.returncode == 3, result.stderr


def test_native_guard_rejects_release_directory_that_does_not_match_version(
    verify, tmp_path,
):
    root = tmp_path / ".deeppattern" / "agent-quality-gates"
    target, _commit = _managed_release_checkout(root.parent)
    (target / "VERSION").write_text("0.14.13\n", encoding="utf-8")
    _git(target, "add", "VERSION")
    _git(
        target,
        "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.test",
        "-c", "commit.gpgsign=false",
        "commit", "--quiet", "-m", "mismatched release identity",
    )
    _link(root, target)

    result = verify(root)

    assert result.returncode == 3, result.stderr


def test_native_guard_rejects_clean_checkout_missing_requirements(verify, tmp_path):
    root = _checkout(tmp_path / "agent-quality-gates")
    assert verify(root).returncode == 0
    _git(root, "rm", "requirements.txt")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "incomplete layout")
    assert _git(root, "status", "--porcelain") == ""
    result = verify(root)
    assert result.returncode == 3, result.stderr
    assert "incomplete or link-like AQG layout" in result.stderr
