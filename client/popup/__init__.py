"""DB/GE local popup launch mechanism (thin client).

This package is the LAUNCH mechanism for the Decision Engine local popup GUI on a
tester client — the layer that opens a native window and round-trips a result. It
is deliberately decoupled from HOW the content is authored: the popup body displays
the FINISHED artifact the server delivered (see ``launcher.render_artifact_html``),
carried in on ``spec.artifact``. The generation IP that produced it stays on the
server and never reaches this client.

Modules:
  * ``backend``      — probe + one-time auto-install of pywebview into a venv.
  * ``native_shell`` — the child process that owns the native window + js_api result.
  * ``launcher``     — the client-facing entry: ensure backend, open the shell, read
                       the committed result, hand it back to the caller.
"""

__all__ = ["backend", "launcher", "native_shell"]
