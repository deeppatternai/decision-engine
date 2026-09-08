"""Exercise the public installer's checkout guard without running installation."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
AQG_REPO = "https://github.com/deeppatternai/agent-quality-gates.git"
SHA = "0123456789abcdef" * 2 + "01234567"
REQUIRED_FILES = (
    "AI_SETUP.md",
    "scripts/install_aqg_clients.py",
    "scripts/aqg_doctor.py",
)


def _bash_path(path, bash, environment):
    # Do not resolve: the guard must see the symlink, including parent aliases.
    path = Path(path).absolute()
    if os.name == "nt":
        # MSYS maps the Windows temp directory to /tmp. Use its mapping for
        # both native absolute link targets and the root passed to the guard.
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-c", 'cygpath -u "$1"', "test", str(path)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=15,
        ).stdout.strip()
    return str(path)


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()


def _checkout(root):
    root.mkdir(parents=True)
    _git(root, "init", "--quiet")
    _git(root, "remote", "add", "origin", AQG_REPO)
    for name in REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")
    return root


def _managed_checkout(parent):
    staging = _checkout(parent / "staging")
    commit = _git(staging, "rev-parse", "HEAD")
    target = parent / "versions" / commit
    target.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(target)
    return target, commit


def _link(root, target):
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            pytest.skip(f"directory symlinks require Windows privileges: {exc}")
        raise
    return root


@pytest.fixture(params=["de-aqg-install", "dp-install.sh"])
def verify_checkout(request):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash executable not found on PATH")
    source = (ROOT / request.param).read_text(encoding="utf-8")
    # Load the real functions only: no network, activation, or host writes.
    functions = []
    for name in ("fail", "clean_exec", "verify_aqg_checkout"):
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", source, re.M | re.S)
        assert match is not None, f"missing installer function: {name}"
        functions.append(match.group())
    if request.param == "dp-install.sh":
        functions.append(re.search(r"^sync_aqg_checkout\(\) \{\n.*?^\}$", source, re.M | re.S).group())
    script = "\n".join(
        [
            "set -euo pipefail",
            "EXIT_USAGE=2",
            "PROGRAM_NAME=fixture-installer",
            "AQG_REF=fixture-no-network",
            "tty_print() { printf '%s\\n' \"$*\"; }",
            'AQG_ROOT="$1"',
            'AQG_REPO="$2"',
            'GIT_BIN="$(command -v git)"',
            *functions,
            "verify_aqg_checkout",
        ]
    )
    environment = os.environ.copy()
    environment.pop("BASH_ENV", None)
    environment.pop("ENV", None)

    def verify(root, *, synchronize=False):
        invocation = script
        if synchronize and request.param == "dp-install.sh":
            # Refuse mutating Git operations at the dependency boundary: no
            # test may fetch the public repository if the reuse path regresses.
            invocation = invocation.rsplit("verify_aqg_checkout", 1)[0] + '''
real_git="$GIT_BIN"
git_guard() {
  case " $* " in *" fetch "*|*" checkout "*|*" pull "*) exit 79;; esac
  "$real_git" "$@"
}
export real_git
export -f git_guard
clean_exec() { "$@"; }
GIT_BIN=git_guard
sync_aqg_checkout
'''
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-s", "--",
             _bash_path(root, bash, environment), AQG_REPO],
            input=invocation,
            capture_output=True,
            text=True,
            env=environment,
            timeout=15,
        )

    return verify


def test_repeat_install_does_not_checkout_inside_a_managed_version(verify_checkout, tmp_path):
    target, _commit = _managed_checkout(tmp_path)
    root = _link(tmp_path / "agent-quality-gates", target)
    before = _git(root, "rev-parse", "HEAD")
    result = verify_checkout(root, synchronize=True)
    assert result.returncode == 0, result.stderr
    assert _git(root, "rev-parse", "HEAD") == before
    assert _git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("relative", [True, False])
@pytest.mark.parametrize("parent_alias", [True, False])
def test_managed_link_is_accepted(verify_checkout, tmp_path, relative, parent_alias):
    parent = tmp_path / "home with spaces" / ".deeppattern"
    target, commit = _managed_checkout(parent)
    root = _link(
        parent / "agent-quality-gates",
        Path("versions") / commit if relative else target,
    )
    if parent_alias:
        alias = _link(tmp_path / "home alias", parent.parent)
        root = alias / ".deeppattern" / root.name
    before = root.readlink()

    result = verify_checkout(root)

    assert result.returncode == 0, result.stderr
    assert root.is_symlink()
    assert root.readlink() == before


def test_regular_checkout_can_be_reused_after_migration(verify_checkout, tmp_path):
    root = _checkout(tmp_path / ".deeppattern" / "agent-quality-gates")
    commit = _git(root, "rev-parse", "HEAD")
    initial = verify_checkout(root)
    assert initial.returncode == 0, initial.stderr

    target = root.parent / "versions" / commit
    target.parent.mkdir()
    root.rename(target)
    _link(root, Path("versions") / commit)
    for _ in range(2):
        result = verify_checkout(root)
        assert result.returncode == 0, result.stderr
        assert _git(root, "rev-parse", "HEAD") == commit
        assert _git(root, "status", "--porcelain") == ""


def test_managed_git_worktree_is_accepted(verify_checkout, tmp_path):
    source = _checkout(tmp_path / "source")
    commit = _git(source, "rev-parse", "HEAD")
    parent = tmp_path / ".deeppattern"
    target = parent / "versions" / commit
    target.parent.mkdir(parents=True)
    _git(source, "worktree", "add", "--detach", str(target), commit)
    root = _link(parent / "agent-quality-gates", target)
    assert (root / ".git").is_file()

    result = verify_checkout(root)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("target_name", [
    "sibling", f"versions-other/{SHA}", "versions", "versions/latest",
    f"versions/{'a' * 39}", f"versions/{'a' * 41}", f"versions/{'g' * 40}",
    f"versions/{SHA}/nested", f"versions/group/{SHA}", "../outside",
])
def test_unmanaged_links_are_rejected(verify_checkout, tmp_path, target_name):
    parent = tmp_path / ".deeppattern"
    target = _checkout(parent / target_name)
    root = _link(parent / "agent-quality-gates", target)
    before = root.readlink()

    result = verify_checkout(root)

    assert result.returncode == 2, result.stderr
    assert "managed versions" in result.stderr
    assert root.is_symlink()
    assert root.readlink() == before
    assert (target / "AI_SETUP.md").read_text(encoding="utf-8") == "fixture\n"


@pytest.mark.parametrize("linked_component", ["versions", f"versions/{SHA}"])
def test_managed_path_cannot_escape_via_another_link(
    verify_checkout, tmp_path, linked_component,
):
    parent = tmp_path / ".deeppattern"
    outside = _checkout(tmp_path / "outside" / SHA)
    _link(
        parent / linked_component,
        outside.parent if linked_component == "versions" else outside,
    )
    root = _link(parent / "agent-quality-gates", Path("versions") / SHA)

    result = verify_checkout(root)

    assert result.returncode == 2, result.stderr
    assert "managed versions" in result.stderr


@pytest.mark.parametrize("kind", ["missing", "file", "dangling", "link-to-file"])
def test_non_directory_roots_are_rejected(verify_checkout, tmp_path, kind):
    root = tmp_path / "agent-quality-gates"
    if kind == "file":
        root.write_text("preserve\n", encoding="utf-8")
    elif kind in {"dangling", "link-to-file"}:
        target = tmp_path / "versions" / SHA
        if kind == "link-to-file":
            target.parent.mkdir()
            target.write_text("preserve\n", encoding="utf-8")
        _link(root, target)

    result = verify_checkout(root)

    assert result.returncode == 2, result.stderr
    assert "preserve it and stop" in result.stderr


@pytest.mark.parametrize("remote", [
    AQG_REPO,
    "git@github.com:deeppatternai/agent-quality-gates.git",
    "ssh://git@github.com/deeppatternai/agent-quality-gates.git",
    "https://example.test/foreign.git",
])
def test_managed_link_still_checks_origin(verify_checkout, tmp_path, remote):
    target, _commit = _managed_checkout(tmp_path)
    _git(target, "remote", "set-url", "origin", remote)
    root = _link(tmp_path / "agent-quality-gates", target)

    result = verify_checkout(root)

    if remote.startswith("https://example.test/"):
        assert result.returncode == 2, result.stderr
        assert "unexpected Git origin" in result.stderr
    else:
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing", [".git", *REQUIRED_FILES])
def test_managed_link_still_requires_checkout_files(verify_checkout, tmp_path, missing):
    target, _commit = _managed_checkout(tmp_path)
    path = target / missing
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_file():
                item.chmod(0o600)
        shutil.rmtree(path)
    else:
        path.unlink()
    root = _link(tmp_path / "agent-quality-gates", target)

    result = verify_checkout(root)

    assert result.returncode == 2, result.stderr
    expected = "not a Git checkout" if missing == ".git" else "missing"
    assert expected in result.stderr


def test_managed_link_rejects_directory_name_that_does_not_match_head(
    verify_checkout, tmp_path,
):
    target = _checkout(tmp_path / "versions" / SHA)
    actual = _git(target, "rev-parse", "HEAD")
    assert actual != SHA
    root = _link(tmp_path / "agent-quality-gates", target)

    result = verify_checkout(root)

    assert result.returncode == 2, result.stderr
    assert "does not match its version directory" in result.stderr
