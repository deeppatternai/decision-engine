"""Test package. Importing it sandboxes the client's state paths.

Same reason as ``installer/tests/__init__.py``: the repo-root ``conftest.py`` covers pytest, but a
``python3 -m unittest client.popup.tests.test_x`` run — documented as the entry point in
``test_backend_probe_stdin.py`` among others — never loads a conftest, and every runtime path in
the client derives from ``client.runner.config_path()``. Deliberately standalone rather than
importing the installer's copy: the client ships without the installer package.
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
