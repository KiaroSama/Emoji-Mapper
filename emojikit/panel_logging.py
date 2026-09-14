"""Bounded UI telemetry with a closed schema: no tokens, labels or media IDs."""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque

EVENTS = frozenset({"ready", "hold", "unhold", "undo", "redo", "reset", "selection",
                    "reorder", "save_requested", "save_succeeded", "save_failed",
                    "animation", "zoom", "backdrop", "error"})
NUMBERS = frozenset({"count", "revision", "status", "duration_ms", "line", "column", "zoom"})
SOURCES = frozenset({"panel-grid.js", "panel-actions.js", "panel-holding.js", "window", "promise"})


class ClientEventLog:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._arrivals: deque[float] = deque(maxlen=10)
        self._lock = threading.Lock()

    def record(self, payload: dict) -> int:
        events = payload.get("events")
        if set(payload) != {"events"} or not isinstance(events, list) or not 1 <= len(events) <= 32:
            raise ValueError("expected 1 to 32 UI events")
        # Validate the entire batch before emitting anything. Unknown strings never
        # reach a log, even if a catalog label or exception embeds a credential.
        for event in events:
            if not isinstance(event, dict) or set(event) - (NUMBERS | {"event", "source", "name"}):
                raise ValueError("unknown UI event fields")
            if not isinstance(event.get("event"), str) or event["event"] not in EVENTS:
                raise ValueError("unknown UI event")
            for key in NUMBERS & event.keys():
                val = event[key]
                if type(val) not in (int, float) or not math.isfinite(val) or not 0 <= val <= 1e9:
                    raise ValueError("invalid UI event number")
            if "source" in event and (not isinstance(event["source"], str) or event["source"] not in SOURCES):
                raise ValueError("invalid UI source")
            if "name" in event and (not isinstance(event["name"], str)
                                    or not re.fullmatch(r"[A-Za-z]{1,30}Error|Error", event["name"])):
                raise ValueError("invalid UI error name")
        with self._lock:
            now = time.monotonic()
            if len(self._arrivals) == 10 and now - self._arrivals[0] < 1:
                return 429
            self._arrivals.append(now)
        for event in events:
            fields = " ".join(f"{k}={event[k]}" for k in sorted(event))
            level = logging.WARNING if event["event"] in {"error", "save_failed"} else logging.INFO
            self.logger.log(level, "UI %s", fields)
        return 204
