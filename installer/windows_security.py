"""Compatibility alias for the packaged client Windows-security primitives."""

from __future__ import annotations

import sys

from client import windows_security as _implementation

# Keep legacy ``installer.windows_security`` imports and monkey-patches bound to
# the exact same module object used by the packaged popup client.  A wrapper that
# copied functions would split their module globals and make security tests patch
# a different object from the production implementation.
sys.modules[__name__] = _implementation
