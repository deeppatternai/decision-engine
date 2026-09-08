"""Execute only the native bootstrapper's AQG guard, without downloading or activation."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from installer.tests.test_de_aqg_install import AQG_REPO, SHA, _checkout, _git, _link

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows bootstrapper")


@pytest.fixture
def verify(tmp_path):
    source = (ROOT / "dp-install.ps1").read_text(encoding="utf-8")
    functions = re.findall(r"^function .*?^\}", source, re.M | re.S)
    start = source.index("if (Test-Path -LiteralPath $AqgRoot)")
    guard = source[start:source.index("$tempRoot =", start)]
    script = tmp_path / "guard.ps1"
    script.write_text("\n".join([
        "$ErrorActionPreference = 'Stop'", "Set-StrictMode -Version Latest",
        "$ProgramName = 'fixture-installer'", "$ExitUsage = 2", "$ExitBlocked = 3",
        "$AqgRoot = $env:FIXTURE_AQG_ROOT", f"$AqgRepository = '{AQG_REPO}'",
        "$GitPath = (Get-Command git.exe).Source", *functions, guard,
    ]), encoding="utf-8-sig")

    def run(root):
        return subprocess.run(
            [shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(script)],
            env={**os.environ, "FIXTURE_AQG_ROOT": str(root)},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    return run


@pytest.mark.parametrize("kind", ["checkout", "migrated", "worktree"])
def test_native_guard_accepts_installed_layouts(verify, tmp_path, kind):
    root = tmp_path / "profile with spaces" / ".deeppattern" / "agent-quality-gates"
    if kind == "checkout":
        _checkout(root)
    else:
        source = _checkout(tmp_path / "source")
        sha = _git(source, "rev-parse", "HEAD")
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
    target = _checkout(root.parent / "versions" / SHA)
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
