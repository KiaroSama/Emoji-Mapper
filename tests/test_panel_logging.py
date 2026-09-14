"""The UI log accepts bounded diagnostic fields, never arbitrary content."""
from __future__ import annotations

import io
import logging
import unittest

from emojikit.panel_logging import ClientEventLog


class UILogContract(unittest.TestCase):
    def test_logs_actions_and_errors_but_rejects_private_or_unbounded_payloads(self):
        stream = io.StringIO()
        logger = logging.Logger("ui-test", level=logging.INFO)
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        self.addCleanup(handler.close)
        sink = ClientEventLog(logger)
        self.assertEqual(sink.record({"events": [{"event": "hold", "count": 4},
                                                {"event": "error", "name": "TypeError",
                                                 "source": "panel-grid.js", "line": 12}]}), 204)
        self.assertIn("event=hold", stream.getvalue())
        self.assertIn("name=TypeError", stream.getvalue())
        good = stream.getvalue()
        for event in ({"event": "error", "message": "private-test-value"},
                      {"event": []}, {"event": "error", "source": []},
                      {"event": "hold", "count": True},
                      {"event": "hold", "count": float("nan")}):
            with self.subTest(event=repr(event)), self.assertRaises(ValueError):
                sink.record({"events": [event]})
        self.assertEqual(stream.getvalue(), good)
        with self.assertRaises(ValueError):
            sink.record({"events": [{"event": "hold"}] * 33})


if __name__ == "__main__":
    unittest.main()
