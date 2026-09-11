"""Exercise the Mac wrapper's AQG update contract without touching real hosts."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from installer.tests.test_dp_aqg_managed_layout import shell_function, INSTALLER


class MacAqgUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'home'
        self.root = self.home / '.deeppattern/agent-quality-gates'
        self.package = self.root / 'scripts/aqg_update'
        self.package.mkdir(parents=True)
        (self.root / 'VERSION').write_text('0.14.14\n')
        self.log = self.home / 'calls'
        self.source = INSTALLER.read_text()

    def run_function(self, name, extra='', env=None):
        functions = '\n'.join(shell_function(self.source, n) for n in (
            'ensure_aqg_update_layout', 'update_managed_aqg',
            'sync_aqg_checkout',
            'aqg_backup_residue', 'prepare_aqg_versions',
            'reconcile_codex_hooks_after_aqg_migration',
        ))
        harness = '''set -euo pipefail
clean_exec() { "$@"; }
tty_print() { printf '%s\\n' "$*"; }
fail() { printf '%s\\n' "$*" >&2; exit 2; }
dependency_pending() { printf '%s\\n' "$*" >&2; exit 4; }
verify_aqg_checkout() {
  AQG_LAYOUT=regular
  if [ -L "$AQG_ROOT" ]; then AQG_LAYOUT=managed; fi
  AQG_MANAGED_TARGET="$(cd "$AQG_ROOT" && pwd -P)"
}
comma_list_contains() { return 0; }
AQG_ROOT="$1"
PYTHON_BIN="$2"
PYTHON_DIR="$(dirname "$2")"
DEEPPATTERN_ROOT="$(dirname "$1")"
AQG_REPO=https://github.com/deeppatternai/agent-quality-gates.git
aqg_selected_clients=codex
''' + functions + '\n' + extra + '\n' + name + '\n'
        return subprocess.run(
            ['/bin/bash', '-c', harness, 'test', str(self.root), sys.executable],
            env={**os.environ, 'HOME': str(self.home), **(env or {})},
            capture_output=True, text=True, timeout=20,
        )

    def migration_provider(self, managed=True):
        # A contract double: assert that the wrapper passes the logical root.
        (self.package / 'migrate.py').write_text('''
from pathlib import Path
from types import SimpleNamespace
def ensure_managed_layout(root):
    assert root.name == 'agent-quality-gates'
    assert not root.is_symlink()
    if MANAGED:
        target = root.parent / 'versions' / '0.14.14'
        target.parent.mkdir()
        root.rename(target)
        root.symlink_to(target)
    return SimpleNamespace(managed=MANAGED, reason='fixture migration')
'''.replace('MANAGED', str(managed)))

    def test_install_explicitly_migrates_to_version_directory(self):
        self.migration_provider()
        result = self.run_function('ensure_aqg_update_layout')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.root.is_symlink())
        self.assertEqual(self.root.resolve().name, '0.14.14')

    def test_migration_refusal_cannot_be_reported_as_success(self):
        self.migration_provider(managed=False)
        result = self.run_function('ensure_aqg_update_layout')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('fixture migration', result.stdout + result.stderr)
        self.assertFalse(self.root.is_symlink())

    def update_provider(self, outcome, pending=()):
        (self.package / 'run.py').write_text(f'''
import json
from pathlib import Path
from types import SimpleNamespace
def check(*, root, remote, channel, apply):
    assert root.name == 'agent-quality-gates'
    assert remote == 'https://github.com/deeppatternai/agent-quality-gates.git'
    assert channel == 'stable' and apply is True
    Path({str(self.log)!r}).write_text('signed check')
    return SimpleNamespace(outcome={outcome!r}, detail='fixture', pending={pending!r})
''')

    def test_repeat_install_calls_signed_stable_update(self):
        self.update_provider('applied')
        target = self.root.parent / 'versions/0.14.14'
        target.parent.mkdir()
        self.root.rename(target)
        self.root.symlink_to(target)
        result = self.run_function('sync_aqg_checkout', extra='GIT_BIN=/must-not-fetch-main')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), 'signed check')

    def test_migration_cannot_claim_managed_without_a_link(self):
        (self.package / 'migrate.py').write_text('''
from types import SimpleNamespace
def ensure_managed_layout(root):
    return SimpleNamespace(managed=True, reason='no link created')
''')
        result = self.run_function('ensure_aqg_update_layout')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.root.is_symlink())

    def test_update_import_is_isolated_from_cwd(self):
        self.update_provider('current')
        (self.home / 'json.py').write_text("raise RuntimeError('cwd injection')\n")
        result = self.run_function('update_managed_aqg', extra='cd "$HOME"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_zero_host_apply_still_runs_layout_migration(self):
        self.migration_provider()
        start = self.source.index('tty_print "Planning AQG configuration')
        end = self.source.index('\naqg_sha=', start)
        flow = self.source[start:end]
        extra = '''
aqg_selected_clients=''
run_aqg_clients() { return 0; }
reconcile_codex_hooks_after_aqg_migration() { return 0; }
converge_managed_claude_hooks() { return 0; }
'''
        (self.root / 'scripts/aqg_doctor.py').write_text('')
        result = self.run_function(flow, extra=extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.root.is_symlink())

    def test_pending_or_failed_update_is_not_success(self):
        for outcome in ('pending', 'busy', 'failed', 'no-keyring', 'repair-required'):
            with self.subTest(outcome=outcome):
                self.update_provider(outcome)
                result = self.run_function('update_managed_aqg')
                self.assertIn(result.returncode, (2, 4), result.stdout + result.stderr)
                self.assertIn('status=' + outcome, result.stdout + result.stderr)

    def test_success_with_pending_approvals_is_not_complete(self):
        self.update_provider('applied', pending=('host-approval',))
        result = self.run_function('update_managed_aqg')
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn('host-approval', result.stdout + result.stderr)

    def test_throttled_and_disabled_updates_preserve_existing_install(self):
        for outcome in ('too-soon', 'disabled', 'current'):
            with self.subTest(outcome=outcome):
                self.update_provider(outcome)
                result = self.run_function('update_managed_aqg')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('status=' + outcome, result.stdout)

    def test_codex_hooks_use_logical_entrance(self):
        target = self.root.parent / 'versions/0.14.14'
        target.parent.mkdir()
        self.root.rename(target)
        self.root.symlink_to(target)
        (target / 'scripts/install_aqg_codex_hooks.py').write_text(f'''
import sys
from pathlib import Path
assert sys.argv[sys.argv.index('--aqg-root') + 1] == {str(self.root)!r}
with Path({str(self.log)!r}).open('a') as stream:
    stream.write(sys.argv[1] + '\\n')
''')
        result = self.run_function('reconcile_codex_hooks_after_aqg_migration')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), '--apply\n--verify\n')

    def backup_residue(self):
        backup = self.root.parent / 'versions/aqg-backups'
        backup.mkdir(parents=True)
        (backup / 'saved.json').write_bytes(b'{"original":true}\n')
        return backup

    def test_legacy_backups_are_archived_after_consent(self):
        backup = self.backup_residue()
        canonical = self.root.parent / 'aqg-backups'
        canonical.mkdir()
        (canonical / 'existing').write_text('keep')
        result = self.run_function('prepare_aqg_versions', extra='confirm_dependency_install() { return 0; }')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(backup.exists())
        copies = list(canonical.glob('legacy-versions-*/aqg-backups/saved.json'))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), b'{"original":true}\n')
        self.assertEqual((canonical / 'existing').read_text(), 'keep')

    def test_declining_backup_relocation_preserves_everything(self):
        backup = self.backup_residue()
        result = self.run_function('prepare_aqg_versions', extra='confirm_dependency_install() { return 1; }')
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertTrue((backup / 'saved.json').is_file())
        self.assertFalse((self.root.parent / 'aqg-backups').exists())

    def test_symlink_backup_residue_is_not_moved(self):
        versions = self.root.parent / 'versions'
        versions.mkdir()
        (versions / 'aqg-backups').symlink_to(self.home)
        result = self.run_function('prepare_aqg_versions', extra='confirm_dependency_install() { return 0; }')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue((versions / 'aqg-backups').is_symlink())

    def test_replaced_backup_after_prompt_is_not_moved(self):
        backup = self.backup_residue()
        result = self.run_function('prepare_aqg_versions', extra='''
confirm_dependency_install() {
  mv "$DEEPPATTERN_ROOT/versions/aqg-backups" "$DEEPPATTERN_ROOT/versions/original"
  mkdir "$DEEPPATTERN_ROOT/versions/aqg-backups"
}
''')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(backup.is_dir())
        self.assertTrue((backup.parent / 'original/saved.json').is_file())
        self.assertFalse((self.root.parent / 'aqg-backups').exists())

    def test_no_legacy_backup_needs_no_prompt(self):
        result = self.run_function('prepare_aqg_versions', extra='confirm_dependency_install() { exit 90; }')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root.parent / 'aqg-backups').exists())

    def test_symlink_archive_destination_is_not_used(self):
        backup = self.backup_residue()
        (self.root.parent / 'aqg-backups').symlink_to(self.home)
        result = self.run_function('prepare_aqg_versions', extra='confirm_dependency_install() { return 0; }')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue((backup / 'saved.json').exists())
        self.assertFalse(list(self.home.glob('legacy-versions-*')))

    def test_all_child_calls_pin_backup_directory_outside_versions(self):
        actual = shell_function(self.source, 'clean_exec')
        result = self.run_function(
            'clean_exec "$PYTHON_BIN" -c \'import os; print(os.environ["AQG_BACKUP_DIR"])\'',
            extra=actual, env={'AQG_BACKUP_DIR': str(self.root.parent / 'versions/aqg-backups')},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.root.parent / 'aqg-backups'))


if __name__ == '__main__':
    unittest.main()
