"""Execute entrypoint boundaries under hostile inherited Git environments."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if ROOT.name == 'installer':
    ROOT = ROOT.parent


class EntrypointEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git = shutil.which('git')
        self.assertIsNotNone(self.git)
        self.clean = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
        self.clean['LC_ALL'] = 'C'
        for name in ('actual', 'decoy'):
            repo = self.root / name
            subprocess.run([self.git, 'init', '-q', str(repo)], env=self.clean, check=True)
            subprocess.run([self.git, '-C', str(repo), 'remote', 'add', 'origin',
                            'https://example.invalid/' + name], env=self.clean, check=True)
        self.poison = {
            'GIT_DIR': str(self.root / 'decoy/.git'),
            'GIT_WORK_TREE': str(self.root / 'decoy'),
            'GIT_COMMON_DIR': str(self.root / 'decoy/.git'),
            'GIT_OBJECT_DIRECTORY': str(self.root / 'decoy/.git/objects'),
            'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(self.root / 'decoy/.git/objects'),
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'remote.origin.url',
            'GIT_CONFIG_VALUE_0': 'https://example.invalid/poison',
            'GIT_CONFIG_PARAMETERS': "'remote.origin.url=https://example.invalid/poison'",
            'GIT_CONFIG_GLOBAL': str(self.root / 'missing-config'),
            'GIT_CONFIG_SYSTEM': str(self.root / 'missing-config'),
            'GIT_EXEC_PATH': str(self.root / 'decoy'),
            'GIT_SSH_COMMAND': 'must-not-run',
            'GIT_FUTURE_TEST_VARIABLE': 'line one\nGIT_DIR=second line',
            'GIT_TERMINAL_PROMPT': '1',
        }

    def probe(self):
        return '''import json, os, subprocess, sys
print(json.dumps({
    "keys": sorted(k for k in os.environ if k.upper().startswith("GIT_")),
    "origin": subprocess.check_output([sys.argv[1], "-C", sys.argv[2], "remote", "get-url", "origin"], text=True).strip()
}))
'''

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS shell entrypoint')
    def test_mac_install_cleans_all_git_variables_and_preserves_parent(self):
        source = (ROOT / 'dp-install.sh').read_text()
        match = re.search(r'(?ms)^clean_exec\(\) \{\n.*?^\}', source)
        self.assertIsNotNone(match)
        probe = self.root / 'probe.py'
        probe.write_text(self.probe())
        script = 'set -eu\n' + match.group() + '''
clean_exec "$@"
test "$GIT_DIR" = "$EXPECTED_GIT_DIR"
set +e
clean_exec /bin/sh -c 'exit 17'
code=$?
test "$code" = 17 || exit 98
test "$GIT_DIR" = "$EXPECTED_GIT_DIR" || exit 99
clean_exec env GIT_TERMINAL_PROMPT=0 /bin/sh -c 'test "$GIT_TERMINAL_PROMPT" = 0'
'''
        for shell in ('/bin/bash', '/bin/zsh'):
            with self.subTest(shell=shell):
                result = subprocess.run(
                    [shell, '-c', script, 'test', sys.executable, '-I',
                     str(probe), self.git, str(self.root / 'actual')],
                    env={**self.clean, **self.poison,
                         'EXPECTED_GIT_DIR': self.poison['GIT_DIR']},
                    text=True, capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data['keys'], [])
                self.assertEqual(data['origin'], 'https://example.invalid/actual')

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS shell entrypoint')
    def test_mac_uninstall_bootstrap_cleans_helper_and_descendant_git(self):
        source = (ROOT / 'dp-uninstall.sh').read_text()
        bootstrap = source.split("<<'PY'\n", 1)[0]
        harness = bootstrap + "<<'PY'\n" + self.probe() + '\nPY\n'
        for shell in ('/bin/bash', '/bin/zsh'):
            with self.subTest(shell=shell):
                result = subprocess.run(
                    [shell, '-c', harness, 'test', self.git,
                     str(self.root / 'actual')],
                    env={**self.clean, **self.poison,
                         'DE_AQG_PYTHON': sys.executable},
                    text=True, capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data['keys'], [])
                self.assertEqual(data['origin'], 'https://example.invalid/actual')

    def test_powershell_boundaries_execute_against_real_git(self):
        pwsh = os.environ.get('DP_TEST_PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
        if not pwsh:
            self.skipTest('PowerShell runtime unavailable; native behavior not proven')
        (self.root / 'json.py').write_text("raise RuntimeError('cwd module injection')\n")
        result = subprocess.run(
            [pwsh, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(Path(__file__).with_name('check_dp_entrypoint_integrity.ps1')),
             '-Root', str(ROOT), '-PythonPath', sys.executable, '-GitPath', self.git,
             '-FixtureRoot', str(self.root)], env=self.clean,
            text=True, capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS: both parsers', result.stdout)


if __name__ == '__main__':
    unittest.main()
