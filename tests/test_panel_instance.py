"""Reopening a panel preserves its server and distinguishes unrelated listeners."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from emojikit import panel, panel_instance
from emojikit.catalog import Catalog


class ReopenPanel(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.db = self.data / "catalog.db"
        with Catalog(self.db):
            pass

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "test listener survived cleanup")

        self.addCleanup(stop)
        return server.server_port

    def launch(self, port, extra=()):
        argv = ["panel", "--data-dir", str(self.data), "--port", str(port),
                "--bot-username", "FixtureBot", *extra]
        with mock.patch("sys.argv", argv), mock.patch.object(panel, "setup_logging"):
            return panel.main()

    def test_reopening_opens_browser_without_reloading_catalog_or_bot(self):
        identity = panel_instance.session_identity(self.db, False, [])
        handler = panel.make_handler([], {}, self.db, "test-token", session_info=identity)
        port = self.serve(handler)
        before = self.db.read_bytes()
        with mock.patch.object(panel_instance.webbrowser, "open", return_value=True) as opened, \
                mock.patch.object(panel, "Catalog") as catalog, \
                mock.patch.object(panel, "_detect_bot_username") as bot:
            self.assertEqual(self.launch(port), 0)
        opened.assert_called_once_with(f"http://127.0.0.1:{port}/")
        catalog.assert_not_called()
        bot.assert_not_called()
        self.assertEqual(self.db.read_bytes(), before)

    def test_another_collection_or_view_is_not_silently_adopted(self):
        identity = panel_instance.session_identity(self.db, False, [])
        port = self.serve(panel.make_handler([], {}, self.db, "test-token", session_info=identity))
        for args in (("--all",), ("--with-pack", "1"),
                     ("--data-dir", str(self.data / "other"))):
            with self.subTest(args=args):
                if args[0] == "--data-dir":
                    with Catalog(self.data / "other" / "catalog.db"):
                        pass
                with mock.patch.object(panel_instance.webbrowser, "open") as opened:
                    self.assertEqual(self.launch(port, args), 2)
                    opened.assert_not_called()

    def test_legacy_panel_is_reopened_but_foreign_or_malformed_services_are_not(self):
        replies = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                status, body = replies.get(self.path, (404, b""))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        port = self.serve(Handler)
        legacy_page = ('<title>Emoji Mapper — Curate</title><script id="items-data">[]</script>'
                       '<script>const TOKEN = "fixture";</script>'
                       '<script src="/static/panel-grid.js"></script>').encode("utf-8")
        cases = [
            ({"/api/ping": (200, b'{"ok":true}'), "/": (200, legacy_page)}, 0),
            ({"/api/ping": (200, b'{"ok":true}'), "/": (200, b"Other app")}, 2),
            ({"/api/session": (200, b"invalid JSON")}, 2),
            ({"/api/session": (200, json.dumps({"application": "other"}).encode("utf-8"))}, 2),
            ({"/api/session": (200, json.dumps({"application": panel_instance.APP,
                                              "legacy": True}).encode("utf-8"))}, 2),
        ]
        for response_map, expected in cases:
            with self.subTest(expected=expected, paths=list(response_map)):
                replies.clear()
                replies.update(response_map)
                with mock.patch.object(panel_instance.webbrowser, "open", return_value=True) as opened, \
                        mock.patch.object(panel, "_port_holder", return_value=None):
                    self.assertEqual(self.launch(port), expected)
                    self.assertEqual(opened.call_count, int(expected == 0))


if __name__ == "__main__":
    unittest.main()
