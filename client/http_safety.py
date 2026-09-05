"""Redirect + body-read safety for the client's authenticated hub fetches (stdlib-only leaf).

Home of the TWO guards the whole shell shares across its authenticated hub reads:

* ``NoRedirect`` — the ONE redirect guard. It lives here — not next to any one caller —
  because five unrelated paths need it (the popup's ``/db/render`` board fetch, the
  shim's ``/v1/ge/artifact`` fetch and its ``/mcp`` JSON-RPC transport, the installer's
  ``/v1/devices/activate`` POST, and the ``audit`` CLI's ``request_json``) and a copy
  per caller is a copy that drifts.
* ``read_within_budget`` — the ONE bounded-read helper. It caps BOTH the bytes read into
  memory (OOM guard) AND the wall-clock time the read may take (slowloris guard), because
  ``urllib``'s ``timeout`` is per socket operation, not a transfer deadline: a hostile hub
  that sends ~1 byte just before each socket-timeout window keeps a plain ``read`` alive
  for a very long time while staying under any byte cap. The three authenticated reads
  share this one implementation for the same reason they share ``NoRedirect`` — a copy per
  caller drifts.

This module uses only the standard library, so any layer may import it without dragging
popup/GUI/config weight along.
"""

from __future__ import annotations

import http.client
import math
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Callable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect on an authenticated hub call.

    urllib's default opener follows 3xx, and its ``HTTPRedirectHandler`` rebuilds the
    request carrying the ORIGINAL headers (it strips only Content-Length/Content-Type),
    so a hostile or compromised ``Location`` would hand an ``Authorization: Bearer``
    device token to whatever host it names — and then feed that host's bytes back as
    the answer. None of our endpoints redirect, so refusing costs nothing.

    Refusing surfaces as an ``HTTPError``, which each caller maps onto its own
    fail-closed contract: ``BoardFetchError`` (popup ``/db/render``), ``ShellError``
    (shim ``/v1/ge/artifact`` and ``/mcp``, installer activation), ``AuditError``
    (the ``audit`` CLI's ``request_json``).

    The refusal is wired at the ``http_error_30x`` seam rather than only at
    ``redirect_request``, because the stdlib quotes and ``urlparse``s Location BEFORE it
    consults ``redirect_request``: a malformed one ("http://[") raises ValueError from
    inside that parse, which carries no ``fp`` for the caller to close — measured, three
    live HTTPResponse objects stayed reachable (closed=False) off the resulting error's
    ``__context__``. Refusing first means EVERY 3xx leaves as an HTTPError carrying
    ``fp``, so each caller's ``finally: exc.close()`` actually has something to close.
    (It also gives 308 the same treatment on Pythons whose HTTPRedirectHandler predates
    ``http_error_308``, since the opener dispatches on the attribute name.)

    On the activation POST the reasoning is different but lands in the same place: the
    secret rides in the BODY, and CPython drops the body on a followed 301/302/303
    (rewriting it to a GET) and won't follow 307/308 on a POST at all — so a followed
    redirect could never complete an activation anyway, and refusing one only turns a
    confusing failure into a clear one.
    """

    def _refuse(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(
            req.full_url, code, "unexpected redirect from an authenticated fetch", headers, fp)

    # Every code the stdlib would otherwise follow. Refusing here pre-empts the Location
    # parse (see the class docstring); a 3xx the stdlib does NOT treat as a redirect (a
    # 304, a Location-less 302) still lands on HTTPDefaultErrorHandler and reaches the
    # caller as an HTTPError all the same.
    http_error_301 = http_error_302 = http_error_303 = _refuse
    http_error_307 = http_error_308 = _refuse

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        # Unreachable through the handlers above; kept so the refusal holds for any caller
        # that drives this documented seam directly rather than through an opener.
        raise urllib.error.HTTPError(
            req.full_url, code, "unexpected redirect from an authenticated fetch", headers, fp)


# One chunk per underlying recv. Big enough that a well-behaved body of the sizes these
# fetches carry (a few MB) drains in a handful of reads; small enough that the accumulation
# overshoots the cap by at most this much before the ``> max_bytes`` check catches it.
_READ_CHUNK_BYTES = 64 * 1024
_Timer = threading.Timer


def _response_socket(response):
    """Return urllib's live body socket, or ``None`` when it cannot be interrupted."""
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None or not callable(getattr(sock, "shutdown", None)):
        return None
    return sock


def read_within_budget(
    response,
    *,
    max_bytes: int,
    budget_s: float,
    error: Callable[[str], BaseException],
    allow_chunked: bool = False,
) -> bytes:
    """Read a hub response body under a byte cap, a wall-clock deadline, and a completeness check.

    The ONE bounded-read the three authenticated hub reads share (shim ``/mcp`` +
    ``/v1/ge/artifact``, popup ``/db/render``). It closes a gap ``urllib``'s ``timeout``
    leaves open: that timeout is per socket operation, not a transfer deadline, so a hub
    that dribbles ~1 byte just before each socket-timeout window holds a plain
    ``response.read()`` for a very long time while never tripping the timeout and never
    passing a byte cap. This helper bounds the TOTAL wall-clock of the body read.

    Three invariants, all enforced here so no caller can get one without the others:

    * **Byte cap, detect-not-truncate.** Reads at most ``max_bytes + 1`` bytes and raises
      when the body is larger than ``max_bytes`` — never returns a silently truncated
      prefix (a hostile oversize body must FAIL, not parse clean). ``max_bytes`` is
      required, not optional: a deadline alone does not bound memory, since a fast flood
      can still OOM the process inside the deadline window.
    * **Transfer deadline.** The read may run at most ``budget_s`` seconds of wall clock
      (measured from entry, ``time.monotonic``). It reads via ``response.read1`` and drains
      the body in chunks, re-checking the deadline between chunks. For this to actually
      bound a dribble, each ``read1`` must return after a *single* ``recv`` — which is true
      for identity / ``Content-Length`` bodies but NOT for ``Transfer-Encoding: chunked``,
      whose framing (the chunk-size line, the inter-chunk CRLF, trailers) is assembled by a
      ``read1`` that loops over many ``recv`` calls with no deadline check between them.
      Chunked responses are therefore REFUSED by default. A caller behind an edge proxy may
      opt in with ``allow_chunked=True``; that path arms a watchdog which shuts down urllib's
      body socket at the deadline to interrupt a framing read blocked inside ``read1``. This
      depends on the platform/TLS socket honoring cross-thread shutdown. If the socket cannot
      be identified, the opt-in fails closed. Content-Length and connection-close bodies retain the
      pre-existing between-read checks. The HEADER phase (inside ``opener.open``, before this
      helper) is NOT bounded here.
    * **Completeness.** A ``Content-Length`` body that ends before its declared length
      (``response.length`` still positive at EOF) is a truncated read — it FAILS rather
      than returning the short prefix, preserving the ``IncompleteRead`` detection the
      former unbounded ``resp.read()`` gave ``forward()``. (A ``Connection: close`` body,
      where ``length`` is ``None`` and EOF is the legitimate terminator, is returned as-is.)

    On any breach it raises ``error(reason)`` — the caller supplies its own fail-closed type
    (``ShellError`` / ``BoardFetchError``) via ``error``. ``reason`` is helper-authored text
    only; NO response byte (nor any hub-chosen length/header value) is interpolated into it,
    so a breach report cannot smuggle server-chosen bytes into model context.

    Read-level failures normally propagate to the caller's existing handler arms. On the
    opt-in chunked path, parser/socket failures are instead normalized to fixed helper text;
    a watchdog-caused failure uses the budget reason and any other read failure uses the
    invalid-framing reason. This prevents response bytes from surviving in an error chain.
    Returns the body bytes (``<= max_bytes``); the caller decodes.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise error("requires a non-negative integer byte cap")
    if (
        isinstance(budget_s, bool)
        or not isinstance(budget_s, (int, float))
        or not math.isfinite(budget_s)
        or budget_s <= 0
    ):
        raise error("requires a positive finite transfer budget")
    deadline = time.monotonic() + budget_s
    chunked = bool(getattr(response, "chunked", False))
    watchdog = None
    watchdog_lock = None
    watchdog_finished = None
    watchdog_expired = None
    if chunked:
        if not allow_chunked:
            raise error("refuses chunked transfer-encoding on an authenticated read")
        body_socket = _response_socket(response)
        if body_socket is None:
            raise error("cannot enforce a transfer deadline for chunked response")
        watchdog_lock = threading.Lock()
        watchdog_finished = threading.Event()
        watchdog_expired = threading.Event()

        def abort_at_deadline():
            with watchdog_lock:
                if watchdog_finished.is_set():
                    return
                watchdog_expired.set()
                try:
                    body_socket.shutdown(socket.SHUT_RDWR)
                except (OSError, ValueError, TypeError, AttributeError):
                    # Do not close from this thread: on POSIX a concurrent close may leave recv
                    # blocked while freeing the fd number for reuse. An already-closed socket
                    # will make the reader return. If shutdown fails on a still-live dribbling
                    # socket there is no remaining hard bound, but that is safer than risking
                    # cross-connection corruption through descriptor reuse.
                    pass

        watchdog = _Timer(
            max(0.0, deadline - time.monotonic()), abort_at_deadline)
        watchdog.daemon = True
        watchdog.start()

    def finish_watchdog():
        if watchdog is None:
            return False
        with watchdog_lock:
            expired = watchdog_expired.is_set()
            watchdog_finished.set()
        watchdog.cancel()
        # cancel() cannot stop a callback which has already started. Joining prevents a late
        # shutdown from racing response teardown or a subsequently opened authenticated socket.
        watchdog.join()
        return expired

    limit = max_bytes + 1        # one past the cap so an over-limit body is DETECTED, not truncated
    buf = bytearray()            # one growable buffer — no list-of-chunks + join copy at the end
    try:
        while len(buf) < limit:
            if (
                watchdog_expired is not None and watchdog_expired.is_set()
            ) or time.monotonic() >= deadline:
                # No response bytes in the message — a slow-stream report must not echo the dribble.
                raise error("transfer exceeded its %gs budget" % budget_s)
            read_failure = None
            try:
                chunk = response.read1(min(_READ_CHUNK_BYTES, limit - len(buf)))
            except (OSError, http.client.HTTPException, ValueError, AttributeError):
                if chunked:
                    if watchdog_expired.is_set():
                        read_failure = "transfer exceeded its %gs budget" % budget_s
                    else:
                        read_failure = "chunked response ended with invalid framing"
                else:
                    raise
            if read_failure is not None:
                # Raise after the except suite so parser/socket exceptions carrying remote
                # bytes are not retained in the agent-facing exception's __context__.
                raise error(read_failure)
            if chunked and time.monotonic() >= deadline:
                raise error("transfer exceeded its %gs budget" % budget_s)
            if not chunk:
                # EOF. If the hub declared a Content-Length it has not satisfied, the body was
                # truncated mid-stream — fail closed (the count is hub-supplied, so keep it out
                # of the message). length is None for a Connection: close body: EOF is valid there.
                remaining = getattr(response, "length", None)
                if isinstance(remaining, int) and remaining > 0:
                    raise error("response body ended before its declared length")
                break
            buf.extend(chunk)
        if finish_watchdog():
            raise error("transfer exceeded its %gs budget" % budget_s)
        if len(buf) > max_bytes:
            raise error("body exceeds %d bytes" % max_bytes)
        return bytes(buf)
    finally:
        finish_watchdog()
