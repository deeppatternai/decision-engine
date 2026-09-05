"""Test package. Importing it sandboxes the client's state paths.

The repo-root ``conftest.py`` does this (and adds a guard) for pytest runs, but a
``python3 -m unittest installer.tests.test_x`` run — the entry point several modules in this
package document in their own docstrings — never loads a conftest. Measured: such a run still
appended to the LIVE ``~/.deeppattern/decision-engine/.runtime/logs/stopper.log``, and the
bootstrap suite still rewrites the live ``config.json`` in place. Importing this package is the
one hook both runners share, so the redirect is repeated here.

The pytest-only part that is NOT repeated is the guard (a test resolving to live state fails
loudly); ``conftest.py`` carries it and explains the whole failure mode.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

# macOS's temp base is under /var, a symlink to /private/var, and the client refuses any managed
# root whose path contains a link — see conftest.py.
_REAL_TMP = os.path.realpath(tempfile.gettempdir())
os.environ["TMPDIR"] = _REAL_TMP + os.sep
tempfile.tempdir = _REAL_TMP

# Skip when a sandbox is already in force (pytest: conftest.py ran first) so the two never disagree.
if "de-test-state-" not in os.environ.get("DE_CONFIG_PATH", ""):
    _sandbox = tempfile.mkdtemp(prefix="de-test-state-")
    atexit.register(shutil.rmtree, _sandbox, True)
    os.environ["DE_CONFIG_PATH"] = os.path.join(_sandbox, "config.json")
