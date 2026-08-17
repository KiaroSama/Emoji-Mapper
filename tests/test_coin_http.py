"""coins/_http: one pooled client, and the status codes that are ANSWERS.

Every coin fetcher goes through this module now. What is under test is the part
of that merge that could quietly change behaviour: which status codes mean "stop
and report", which mean "retry", and how long the run is allowed to wait before
either.

The fetchers' own subjects live elsewhere: the logo cache in
`test_coin_logo_cache`, argument parsing in `test_coin_cli_args`.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from coins import _http  # noqa: E402
from coins import fetch_cmc as coins_cmc  # noqa: E402
from coins import fetch_paprika as coins_fp  # noqa: E402
from tests._cli_fixtures import _load_standalone  # noqa: E402


class StubResponse:
    def __init__(self, status=200, payload=None, body=b"", headers=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {}
        self._payload = payload

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return self._payload


class StubSession:
    """Records every GET and answers from a queue of StubResponses."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        r = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(r, Exception):
            raise r
        return r


class SharedHttpClient(unittest.TestCase):
    """Four hand-rolled urllib wrappers became one Session; the semantics stayed.

    Each fetcher used to open a fresh TCP+TLS connection per call (urllib pools
    nothing) and carried its own retry ladder. The dangerous half of the merge is
    the status branches: 402 means "CoinPaprika's free quota is spent, resume
    later" and 400/404 mean "CMC has no such symbol". Both must stay
    distinguishable from a transient failure, and neither may be retried -- a
    retry there burns the very rate limit that produced it.
    """

    @classmethod
    def setUpClass(cls):
        # Standalone so the reload-based tests cannot mutate a module object
        # another test module already imported.
        cls.logos = _load_standalone(ROOT / "coins" / "fetch_logos.py",
                                     "http_fetch_logos")

    def setUp(self):
        self.slept: list[float] = []
        self.patches = [
            mock.patch.object(_http.time, "sleep", self.slept.append),
            mock.patch.object(_http, "SESSION", None),
        ]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in reversed(self.patches):
            pt.stop()

    def _session(self, *responses) -> StubSession:
        sess = StubSession(*responses)
        _http.SESSION = sess
        return sess

    def test_no_coin_script_hand_rolls_urlopen_again(self):
        """The regression that matters: a per-call connection to one image host.

        fetch_logos hits the same CDN thousands of times per run, so a fresh
        handshake each time is the whole cost. Anything reintroducing urlopen
        here has silently opted out of the pool.
        """
        offenders = sorted(p.name for p in (ROOT / "coins").glob("*.py")
                           if re.search(r"urlopen\s*\(", p.read_text(encoding="utf-8")))
        self.assertEqual(offenders, [])

    def test_402_is_a_quota_answer_and_is_never_retried(self):
        sess = self._session(StubResponse(402))
        self.assertIs(coins_fp.http_json("https://paprika.test/search?q=x"),
                      coins_fp.QUOTA_EXHAUSTED)
        self.assertEqual(len(sess.calls), 1, "a spent quota must not be retried")
        self.assertEqual(self.slept, [])

    def test_400_and_404_are_no_match_and_are_never_retried(self):
        for code in (400, 404):
            with self.subTest(code=code):
                sess = self._session(StubResponse(code))
                self.assertIsNone(
                    coins_cmc.cmc_json("https://cmc.test/map?symbol=zzz", {}))
                self.assertEqual(len(sess.calls), 1)

    def test_a_transient_failure_is_retried_and_then_reported_as_None(self):
        """None, not QUOTA_EXHAUSTED: a 500 is not a spent quota."""
        sess = self._session(StubResponse(500))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(coins_fp.http_json("https://paprika.test/x"))
        self.assertEqual(len(sess.calls), 4)     # the caller's retries=4
        self.assertEqual(len(self.slept), 3,     # no pointless wait after the last
                         "the run slept once more than it had attempts left")

    def test_a_success_is_decoded_and_costs_one_call(self):
        self._session(StubResponse(200, payload={"currencies": [{"id": "btc"}]}))
        self.assertEqual(coins_fp.http_json("https://paprika.test/x"),
                         {"currencies": [{"id": "btc"}]})

    def test_retry_after_beats_the_backoff_ladder(self):
        """The server knows how long it wants us gone better than a fixed ramp."""
        sess = self._session(StubResponse(429, headers={"Retry-After": "7"}),
                             StubResponse(200, payload=[]))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_http.get("https://gecko.test/p1", retries=3).json(), [])
        self.assertEqual(self.slept, [7.0])
        self.assertEqual(len(sess.calls), 2)

    def test_an_absurd_retry_after_is_capped(self):
        """"Come back in an hour" must not turn one 429 into a hung run."""
        self._session(StubResponse(429, headers={"Retry-After": "3600"}),
                      StubResponse(200, payload=[]))
        with contextlib.redirect_stdout(io.StringIO()):
            _http.get("https://gecko.test/p1", retries=3)
        self.assertEqual(self.slept, [_http.RETRY_AFTER_MAX])

    def test_a_transport_exception_is_retried_like_a_bad_status(self):
        sess = self._session(requests.ConnectionError("connection reset"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(_http.get("https://gecko.test/p1", retries=2))
        self.assertEqual(len(sess.calls), 2)

    def test_fetch_logos_still_raises_after_its_retries(self):
        """main() breaks out of paging on RuntimeError; None would page on."""
        self._session(StubResponse(503))
        with contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(RuntimeError):
            self.logos._get("https://gecko.test/markets", retries=2)


class PagingDelay(unittest.TestCase):
    """12 s x 40 pages was ~8 minutes of pure sleep, on top of a real backoff."""

    def test_the_default_is_short(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_http.PAGE_DELAY_ENV, None)
            self.assertLessEqual(_http.page_delay(), 3.0)

    def test_the_env_var_overrides_it(self):
        with mock.patch.dict(os.environ, {_http.PAGE_DELAY_ENV: "15"}):
            self.assertEqual(_http.page_delay(), 15.0)

    def test_a_typo_falls_back_instead_of_crashing_at_import(self):
        with mock.patch.dict(os.environ, {_http.PAGE_DELAY_ENV: "12 seconds"}):
            self.assertEqual(_http.page_delay(), _http.PAGE_DELAY_DEFAULT)


if __name__ == "__main__":
    unittest.main()
