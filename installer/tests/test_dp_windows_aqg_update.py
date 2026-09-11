"""Offline behavior checks for Python contracts embedded in Windows prototypes."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from installer.tests.test_dp_windows_integrity_hardening import INSTALL, UNINSTALL, embedded_python


def snippet(variable):
    match = re.search(r'\$' + variable + r"\s*=\s*@'\n(.*?)\n'@", INSTALL.read_text(), re.S)
    if match is None:
        raise AssertionError('Missing embedded contract: ' + variable)
    return match.group(1)


class WindowsAqgUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'home'
        self.root = self.home / '.deeppattern/agent-quality-gates'
        self.package = self.root / 'scripts/aqg_update'
        self.package.mkdir(parents=True)

    def run_script(self, variable, *args):
        return subprocess.run([sys.executable, '-I', '-B', '-c', snippet(variable), *map(str, args)],
                              env={**os.environ, 'HOME': str(self.home)},
                              capture_output=True, text=True, timeout=15)

    def test_migration_creates_version_directory(self):
        (self.package / 'migrate.py').write_text('''
from types import SimpleNamespace
def ensure_managed_layout(root):
    assert root.name == 'agent-quality-gates'
    target = root.parent / 'versions/0.14.17'
    target.parent.mkdir()
    root.rename(target)
    root.symlink_to(target)
    return SimpleNamespace(managed=True, reason='migrated')
''')
        result = self.run_script('aqgUpdateScript', self.root, 'migrate', 'official')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.root.resolve().name, '0.14.17')

    def test_migration_refusal_is_not_success(self):
        (self.package / 'migrate.py').write_text('''
from types import SimpleNamespace
def ensure_managed_layout(root):
    return SimpleNamespace(managed=False, reason='locked fixture')
''')
        result = self.run_script('aqgUpdateScript', self.root, 'migrate', 'official')
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn('locked fixture', result.stdout)
        self.assertFalse(self.root.is_symlink())

    def test_signed_update_uses_logical_root_and_reports_pending(self):
        for outcome, pending, code in [('current', (), 0), ('applied', (), 0),
                                        ('applied', ('approval',), 4), ('busy', (), 4),
                                        ('failed', (), 2), ('too-soon', (), 0),
                                        ('disabled', (), 0), ('no-keyring', (), 2)]:
            with self.subTest(outcome=outcome, pending=pending):
                (self.package / 'run.py').write_text(f'''
from types import SimpleNamespace
from pathlib import Path
def check(*, root, remote, channel, apply):
    assert root.name == 'agent-quality-gates'
    assert Path.cwd() == root.parent
    assert remote == 'official' and channel == 'stable' and apply is True
    return SimpleNamespace(outcome={outcome!r}, pending={pending!r}, detail='fixture')
''')
                result = self.run_script('aqgUpdateScript', self.root, 'update', 'official')
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn('status=' + outcome, result.stdout)

    def test_install_and_uninstall_agree_on_official_version_names(self):
        helper = {'__name__': __name__}
        exec(compile(embedded_python(UNINSTALL.read_text())[-1], str(UNINSTALL), 'exec'), helper)
        head = 'a' * 40
        for name, expected in [('0.14.17', True), (head, True),
                               ('0.14.17-' + head[:12], True),
                               ('0.14.17-0123456789abcdef', True),
                               ('0.14.17-' + head[:12] + '-0123456789abcdef', True),
                               ('0.14.16', False), ('0.14.17-unknown', False),
                               ('0.14.17-bbbbbbbbbbbb', False)]:
            with self.subTest(name=name):
                target = self.root.parent / 'versions' / name
                target.mkdir(parents=True)
                (target / 'VERSION').write_text('0.14.17\n')
                result = self.run_script('aqgNameScript', target, head)
                self.assertEqual(result.returncode == 0, expected, result.stderr)
                self.assertEqual(bool(helper['aqg_managed_target_name_matches'](target, head)), expected)

    def test_backup_archive_preserves_content_and_rejects_replacement(self):
        source = self.root.parent / 'versions/aqg-backups'
        source.mkdir(parents=True)
        (source / 'saved').write_bytes(b'original')
        probe = self.run_script('aqgBackupScript', self.root.parent, 'inspect')
        self.assertEqual(probe.returncode, 0, probe.stderr)
        moved = self.run_script('aqgBackupScript', self.root.parent, 'move', probe.stdout.strip())
        self.assertEqual(moved.returncode, 0, moved.stderr)
        archived = list((self.root.parent / 'aqg-backups').glob('legacy-versions-*/aqg-backups/saved'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), b'original')
        self.assertFalse(source.exists())
        source.mkdir()
        probe = self.run_script('aqgBackupScript', self.root.parent, 'inspect')
        source.rename(source.with_name('old'))
        source.mkdir()
        refused = self.run_script('aqgBackupScript', self.root.parent, 'move', probe.stdout.strip())
        self.assertEqual(refused.returncode, 2)
        self.assertTrue(source.exists())

    def test_backup_symlink_source_is_rejected(self):
        source = self.root.parent / 'versions/aqg-backups'
        source.parent.mkdir()
        source.symlink_to(self.home)
        result = self.run_script('aqgBackupScript', self.root.parent, 'inspect')
        self.assertEqual(result.returncode, 2)
        self.assertTrue(source.is_symlink())

    def test_backup_absent_is_noop_and_linked_destination_is_rejected(self):
        probe = self.run_script('aqgBackupScript', self.root.parent, 'inspect')
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout, '')
        source = self.root.parent / 'versions/aqg-backups'
        source.mkdir(parents=True)
        (source / 'saved').write_bytes(b'preserve')
        destination = self.root.parent / 'aqg-backups'
        destination.symlink_to(self.home)
        probe = self.run_script('aqgBackupScript', self.root.parent, 'inspect')
        result = self.run_script('aqgBackupScript', self.root.parent, 'move', probe.stdout.strip())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((source / 'saved').read_bytes(), b'preserve')
        self.assertTrue(destination.is_symlink())

    def test_legacy_junction_defers_without_importing_update_or_migrating(self):
        for mode in ('migrate', 'update'):
            with self.subTest(mode=mode):
                prefix = '''
from pathlib import Path
from types import SimpleNamespace
Path.lstat = lambda self: SimpleNamespace(st_reparse_tag=0xA0000003)
'''
                result = subprocess.run(
                    [sys.executable, '-I', '-B', '-c', prefix + snippet('aqgUpdateScript'),
                     str(self.root), mode, 'official'], capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 5, result.stderr)
                self.assertIn('legacy-junction', result.stdout)
                self.assertTrue(self.root.is_dir())
                self.assertFalse((self.root.parent / 'versions').exists())

    def test_codex_hooks_follow_logical_root_and_reuse_verified_hooks(self):
        (self.root / 'scripts/install_aqg_clients.py').write_text(
            "def installed_supported_clients(): return ['codex']\n")
        log = self.home / 'hook-calls'
        (self.root / 'scripts/install_aqg_codex_hooks.py').write_text(f'''
from pathlib import Path
def main(args):
    assert args[args.index('--aqg-root') + 1] == {str(self.root)!r}
    with Path({str(log)!r}).open('a') as stream: stream.write(args[0] + '\\n')
    return 0
''')
        target = self.root.parent / 'versions/0.14.17'
        target.parent.mkdir()
        self.root.rename(target)
        self.root.symlink_to(target)
        result = self.run_script('aqgHookScript', self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text(), '--verify\n')

    def test_codex_hooks_repair_and_propagate_apply_failure(self):
        (self.root / 'scripts/install_aqg_clients.py').write_text(
            "def installed_supported_clients(): return ['codex']\n")
        for apply_code in (0, 2):
            with self.subTest(apply_code=apply_code):
                log = self.home / ('hook-calls-' + str(apply_code))
                (self.root / 'scripts/install_aqg_codex_hooks.py').write_text(f'''
from pathlib import Path
calls = 0
def main(args):
    global calls
    calls += 1
    assert args[args.index('--aqg-root') + 1] == {str(self.root)!r}
    with Path({str(log)!r}).open('a') as stream: stream.write(args[0] + '\\n')
    if args[0] == '--apply': return {apply_code}
    return 1 if calls == 1 else 0
''')
                result = self.run_script('aqgHookScript', self.root)
                self.assertEqual(result.returncode, apply_code, result.stderr)
                expected = '--verify\n--apply\n' + ('--verify\n' if apply_code == 0 else '')
                self.assertEqual(log.read_text(), expected)

    def test_all_embedded_python_compiles(self):
        for path in (INSTALL, UNINSTALL):
            for index, source in enumerate(embedded_python(path.read_text())):
                with self.subTest(path=path.name, index=index):
                    compile(source, str(path) + ':' + str(index), 'exec')


if __name__ == '__main__':
    unittest.main()
