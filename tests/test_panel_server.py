"""The panel as a process: which host may reach it, and who owns the port.

These start real subprocesses and bind real sockets, so they are the slowest
tests in the panel set and are kept apart from the in-process ones.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib import error, request

from tests._panel_fixtures import ROOT, _make_png

sys.path.insert(0, str(ROOT))

from emojikit import panel as p
from emojikit.catalog import Catalog


class LoopbackCheck(unittest.TestCase):
    def test_accepts_loopback_forms(self):
        for h in ("127.0.0.1:8765", "localhost:8765", "127.0.0.1",
                  "http://127.0.0.1:8765", "[::1]:8765"):
            self.assertTrue(p._is_loopback(h), h)

    def test_rejects_foreign_hosts(self):
        for h in ("evil.com", "evil.com:8765", "http://evil.com",
                  "127.0.0.1.evil.com", ""):
            self.assertFalse(p._is_loopback(h), h)


class TheBusyPortMessageNamesTheProcess(unittest.TestCase):
    """"Press Ctrl+C in the window running it" is useless with no window.

    Every stray panel so far was started detached, so the advice pointed at a
    window that does not exist. The pid does.
    """

    SAMPLE = chr(10).join([
        "Active Connections",
        "",
        "  Proto  Local Address      Foreign Address    State       PID",
        "  TCP    127.0.0.1:9450     0.0.0.0:0          LISTENING   4321",
        "  TCP    127.0.0.1:9451     0.0.0.0:0          LISTENING   9999",
        "  TCP    10.0.0.5:59450     1.2.3.4:443        ESTABLISHED 1111",
    ])

    def _holder(self, port, stdout=None, boom=None):
        def run(*a, **kw):
            if boom:
                raise boom
            text = self.SAMPLE if stdout is None else stdout
            return type("R", (), {"stdout": text})()

        with mock.patch.object(p.subprocess, "run", run):
            return p._port_holder(port)

    def test_it_finds_the_listener(self):
        self.assertEqual(self._holder(9450), 4321)
        self.assertEqual(self._holder(9451), 9999)

    def test_an_established_connection_is_not_a_listener(self):
        self.assertIsNone(self._holder(443))

    def test_a_port_nobody_listens_on_is_none(self):
        self.assertIsNone(self._holder(1234))

    def test_it_never_raises(self):
        """It runs only to improve an error message."""
        self.assertIsNone(self._holder(9450, boom=OSError("no netstat")))
        self.assertIsNone(self._holder(9450, stdout="garbage" + chr(10)))


class TheSandboxCannotTakeTheRealPanelsPort(unittest.TestCase):
    """One number, one place.

    The sandbox exists to stay off the port a real panel serves on. Both sides
    used to spell the number out, so moving the panel would have silently
    pointed the sandbox at it -- and a synthetic drag against the REAL catalog
    is how three hours of manual ordering were destroyed once.
    """

    def _sandbox(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import panel_sandbox
        return panel_sandbox

    def test_the_sandbox_derives_its_port_from_the_panel(self):
        ps = self._sandbox()
        self.assertEqual(ps.PANEL_PORT, p.DEFAULT_PORT,
                         "the sandbox must read the panel's port, not repeat it")
        self.assertNotEqual(ps.DEFAULT_PORT, p.DEFAULT_PORT)

    def test_it_refuses_to_be_told_to_use_it(self):
        ps = self._sandbox()
        with self.assertRaises(SystemExit) as caught:
            ps.main(["--port", str(p.DEFAULT_PORT)])
        self.assertIn(str(p.DEFAULT_PORT), str(caught.exception))


class OnlyOnePanelPerPort(unittest.TestCase):
    """A second launch reuses the first panel without binding over it.

    socketserver sets SO_REUSEADDR by default and on Windows that does not mean
    what it means on Linux: the second bind SUCCEEDS. Two panels then run, both
    logging "Panel at ...", the browser reaches whichever socket the OS picks,
    and the older process keeps serving its own start-up snapshot -- which is
    why refreshing appeared to do nothing and only closing the launcher, which
    kills every instance, made a change appear.
    """

    TIMEOUT = 15

    def test_the_second_instance_reuses_the_live_panel(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        img = data / "media" / "static" / "a.png"
        _make_png(img)
        with Catalog(data / "catalog.db") as cat:
            cat.add(content_key="s:" + "a" * 30, fmt="static", file_path=img,
                    emojis=["😀"], keywords=["one"])

        with socket.socket() as probe:          # a free port, chosen by the OS
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        argv = [sys.executable, "-m", "emojikit.panel", "--data-dir", str(data),
                "--port", str(port), "--no-open", "--bot-username", "FixtureBot"]
        first = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, encoding="utf-8", cwd=ROOT)

        def stop_first():
            # kill() alone leaves a zombie and an open pipe -- the guarded
            # runner reports both, and a leaked process holding the port would
            # make the NEXT run of this test fail for the wrong reason.
            if first.poll() is None:
                first.kill()
            try:
                first.communicate(timeout=self.TIMEOUT)
            except subprocess.TimeoutExpired:
                first.kill()
                first.communicate(timeout=self.TIMEOUT)

        self.addCleanup(stop_first)

        # Bounded polling on a real readiness signal, never a blind sleep.
        deadline = time.monotonic() + self.TIMEOUT
        ready = False
        while time.monotonic() < deadline:
            if first.poll() is not None:
                out = first.communicate(timeout=self.TIMEOUT)[0] or ""
                self.fail(f"the first panel exited early: {out[-400:]}")
            try:
                with request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
                    ready = r.status == 200
                    break
            except (error.URLError, OSError):
                continue
        self.assertTrue(ready, "the first panel never became reachable")

        second = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                timeout=self.TIMEOUT, cwd=ROOT)
        self.assertEqual(second.returncode, 0, (second.stdout + second.stderr)[-900:])
        self.assertIn("already running", second.stdout + second.stderr)
        self.assertIsNone(first.poll(), "reopening must preserve the first process")
        with request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
