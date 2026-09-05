"""Unit tests for the shared bounded-read helper (client.http_safety.read_within_budget).

This is the ONE implementation the three authenticated hub reads share (shim ``/mcp`` +
``/v1/ge/artifact``, popup ``/db/render``); the end-to-end socket behavior is pinned in
test_shim (HubReadDeadlineTests) and the board-fetch tests. Here the logic is exercised
in isolation against a fake response and a controllable clock, so the byte-cap and
deadline branches are deterministic and fast — no real socket, no real sleep.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from client import http_safety
from client.http_safety import read_within_budget


class _FakeResponse:
    """A minimal stand-in for an ``HTTPResponse``: ``read1(n)`` hands back up to ``n`` bytes
    from a fixed buffer and returns ``b""`` at EOF — the exact contract the helper relies on.
    ``chunked`` mirrors ``HTTPResponse.chunked``; ``length`` mirrors the remaining declared
    bytes (a positive value left at EOF = a truncated Content-Length body; ``None`` = a
    Connection: close body where EOF is legitimate)."""

    def __init__(self, data: bytes, *, chunked=False, length=None):
        self._data = data
        self.chunked = chunked
        self.length = length
        self.calls = []

    def read1(self, n=-1):
        self.calls.append(n)
        if n is None or n < 0:
            chunk, self._data = self._data, b""
        else:
            chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class _InfiniteResponse:
    """Never reaches EOF: every ``read1`` yields one byte, forever — a slowloris in the small."""

    def read1(self, n=-1):
        return b"x"


class _RaisingResponse:
    def __init__(self, exc):
        self._exc = exc

    def read1(self, n=-1):
        raise self._exc


class _FakeSocket:
    def __init__(self):
        self.shutdown_calls = []
        self.close_calls = 0

    def shutdown(self, how):
        self.shutdown_calls.append(how)

    def close(self):
        self.close_calls += 1


class _DelayedTimer:
    """Runs its callback from join(), after the reader has declared completion."""

    def __init__(self, _delay, callback):
        self.callback = callback
        self.cancelled = False
        self.joined = False
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def join(self):
        self.joined = True
        self.callback()


class _ImmediateTimer(_DelayedTimer):
    def start(self):
        self.callback()

    def join(self):
        self.joined = True


def _err(reason):
    return RuntimeError("BOUNDARY: " + reason)


class ReadWithinBudgetByteCapTests(unittest.TestCase):
    def test_returns_the_whole_body_when_under_cap_and_within_budget(self):
        resp = _FakeResponse(b"hello world")
        self.assertEqual(
            read_within_budget(resp, max_bytes=1024, budget_s=30, error=_err), b"hello world")

    def test_body_exactly_at_the_cap_is_returned(self):
        # The ceiling is exclusive: a body of exactly max_bytes is legal.
        resp = _FakeResponse(b"abcd")
        self.assertEqual(
            read_within_budget(resp, max_bytes=4, budget_s=30, error=_err), b"abcd")

    def test_one_byte_over_the_cap_is_detected_not_truncated(self):
        # The discriminating case: read(cap) would return a valid-looking prefix and silently
        # drop the tail. Reading cap+1 sees the extra byte and raises.
        resp = _FakeResponse(b"abcde")
        with self.assertRaises(RuntimeError) as caught:
            read_within_budget(resp, max_bytes=4, budget_s=30, error=_err)
        self.assertIn("body exceeds 4 bytes", str(caught.exception))

    def test_over_cap_read_is_bounded_to_cap_plus_one(self):
        # A hostile 1 MB body must not be pulled into memory to be rejected — the read stops
        # the instant it holds cap+1 bytes.
        resp = _FakeResponse(b"x" * (1024 * 1024))
        with self.assertRaises(RuntimeError):
            read_within_budget(resp, max_bytes=16, budget_s=30, error=_err)
        # It requested at most cap+1 bytes across all reads and stopped — never the whole body.
        self.assertLessEqual(sum(resp.calls), 17)

    def test_the_reason_never_carries_response_bytes(self):
        # The body is the attacker's; an over-cap report must not echo it into model context.
        resp = _FakeResponse(b"SENSITIVE-SERVER-BYTES" * 10)
        with self.assertRaises(RuntimeError) as caught:
            read_within_budget(resp, max_bytes=8, budget_s=30, error=_err)
        self.assertNotIn("SENSITIVE", str(caught.exception))


class ReadWithinBudgetDeadlineTests(unittest.TestCase):
    def test_a_never_ending_dribble_is_stopped_by_the_deadline(self):
        # The whole point: bytes keep flowing (never EOF, always under the cap), yet the read
        # returns — bounded by wall clock, not by the body. A fake clock makes it deterministic:
        # entry stamps deadline=0+1; the first loop check sees 0.5 (<1, proceed), the second
        # sees 2.0 (>=1, raise) — so exactly one byte is read before the deadline fires.
        clock = mock.Mock(side_effect=[0.0, 0.5, 2.0])
        with mock.patch.object(http_safety.time, "monotonic", clock):
            with self.assertRaises(RuntimeError) as caught:
                read_within_budget(_InfiniteResponse(), max_bytes=10**9, budget_s=1, error=_err)
        self.assertIn("budget", str(caught.exception))

    def test_the_budget_appears_in_the_reason_without_response_bytes(self):
        clock = mock.Mock(side_effect=[100.0, 130.5])   # entry, then already past the 30s budget
        with mock.patch.object(http_safety.time, "monotonic", clock):
            with self.assertRaises(RuntimeError) as caught:
                read_within_budget(_InfiniteResponse(), max_bytes=10**9, budget_s=30, error=_err)
        # The reason is fixed helper text — no response byte is ever interpolated. Pinning the
        # whole string proves the dribble (b"x" from _InfiniteResponse) cannot leak into it.
        self.assertEqual(str(caught.exception), "BOUNDARY: transfer exceeded its 30s budget")

    def test_non_positive_or_non_finite_budget_is_refused_before_read(self):
        for budget in (0, -1, float("inf"), float("-inf"), float("nan"), True):
            with self.subTest(budget=budget):
                resp = _FakeResponse(b"anything")
                with self.assertRaises(RuntimeError) as caught:
                    read_within_budget(
                        resp, max_bytes=1024, budget_s=budget, error=_err)
                self.assertIn("positive finite", str(caught.exception))
                self.assertEqual(resp.calls, [])

    def test_negative_or_non_integer_byte_cap_is_refused_before_read(self):
        for cap in (-1, 1.5, True):
            with self.subTest(cap=cap):
                resp = _FakeResponse(b"anything")
                with self.assertRaises(RuntimeError) as caught:
                    read_within_budget(
                        resp, max_bytes=cap, budget_s=30, error=_err)
                self.assertIn("non-negative integer", str(caught.exception))
                self.assertEqual(resp.calls, [])


class ReadWithinBudgetChunkedTests(unittest.TestCase):
    def test_a_chunked_response_is_refused_before_any_read(self):
        # read1() on a chunked body assembles its framing with a multi-recv loop the deadline
        # cannot interrupt, so a chunked response is refused outright rather than read under a
        # bound that does not actually hold. The refusal happens before the first read1().
        resp = _FakeResponse(b'anything', chunked=True)
        with self.assertRaises(RuntimeError) as caught:
            read_within_budget(resp, max_bytes=1024, budget_s=30, error=_err)
        self.assertIn("chunked", str(caught.exception))
        self.assertEqual(resp.calls, [], "the body was read despite the chunked refusal")

    def test_chunked_opt_in_fails_closed_without_an_interruptible_socket(self):
        resp = _FakeResponse(b"anything", chunked=True)
        with self.assertRaises(RuntimeError) as caught:
            read_within_budget(
                resp, max_bytes=1024, budget_s=30, error=_err, allow_chunked=True)
        self.assertIn("cannot enforce", str(caught.exception))
        self.assertEqual(resp.calls, [], "an uninterruptible chunked body was read")

    def test_finished_flag_prevents_a_late_watchdog_callback_from_shutdown(self):
        sock = _FakeSocket()
        resp = _FakeResponse(b"complete", chunked=True)
        resp.fp = SimpleNamespace(raw=SimpleNamespace(_sock=sock))
        with mock.patch.object(http_safety, "_Timer", _DelayedTimer):
            body = read_within_budget(
                resp, max_bytes=1024, budget_s=30, error=_err, allow_chunked=True)
        self.assertEqual(body, b"complete")
        self.assertEqual(sock.shutdown_calls, [])
        self.assertEqual(sock.close_calls, 0)

    def test_watchdog_shutdown_fires_before_a_chunked_read(self):
        sock = _FakeSocket()
        resp = _FakeResponse(b"never read", chunked=True)
        resp.fp = SimpleNamespace(raw=SimpleNamespace(_sock=sock))
        with mock.patch.object(http_safety, "_Timer", _ImmediateTimer):
            with self.assertRaises(RuntimeError) as caught:
                read_within_budget(
                    resp, max_bytes=1024, budget_s=30, error=_err, allow_chunked=True)
        self.assertIn("budget", str(caught.exception))
        self.assertEqual(len(sock.shutdown_calls), 1)
        self.assertEqual(resp.calls, [])


class ReadWithinBudgetCompletenessTests(unittest.TestCase):
    def test_a_truncated_content_length_body_fails_closed(self):
        # The server declared more than it sent (length still positive at EOF). A short body
        # that happens to be valid JSON must NOT be accepted as complete — this preserves the
        # IncompleteRead detection the former unbounded resp.read() gave forward().
        resp = _FakeResponse(b'{"ok":true}', length=4989)   # 4989 declared bytes never arrived
        with self.assertRaises(RuntimeError) as caught:
            read_within_budget(resp, max_bytes=1 << 20, budget_s=30, error=_err)
        self.assertIn("declared length", str(caught.exception))

    def test_a_connection_close_body_returns_normally_at_eof(self):
        # length is None for a Connection: close body — EOF is the legitimate terminator, so
        # the completeness check must not fire a false positive.
        resp = _FakeResponse(b'{"ok":true}', length=None)
        self.assertEqual(
            read_within_budget(resp, max_bytes=1 << 20, budget_s=30, error=_err), b'{"ok":true}')

    def test_a_fully_received_content_length_body_returns(self):
        # length == 0 at EOF: the declared body was fully received.
        resp = _FakeResponse(b'{"ok":true}', length=0)
        self.assertEqual(
            read_within_budget(resp, max_bytes=1 << 20, budget_s=30, error=_err), b'{"ok":true}')


class ReadWithinBudgetReadErrorTests(unittest.TestCase):
    def test_a_read_level_error_propagates_uncaught(self):
        # The helper converts ONLY cap/deadline breaches to the caller's type; a socket timeout
        # or IncompleteRead must reach the caller's OWN existing handler arms unchanged.
        for exc in (TimeoutError("socket timed out"), OSError("connection reset")):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(type(exc)):
                    read_within_budget(_RaisingResponse(exc), max_bytes=1024, budget_s=30, error=_err)


if __name__ == "__main__":
    unittest.main()
