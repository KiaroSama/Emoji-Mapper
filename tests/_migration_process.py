"""Bounded child operations for native migration/writer exclusion tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

from emojikit import collection_migrate as cm
from emojikit import migration_bundle
from emojikit.catalog import Catalog
from scripts import identity_repair as ir


def _decode(path, fmt):
    raw = Path(path).read_bytes()
    index = raw[-1]
    return "v:" + (f"{index + 16:02x}" * 16), index


def _hold():
    print("READY", flush=True)
    # Parent always closes this pipe, including on assertion failure; the
    # parent's communicate timeout owns and kills an unresponsive child.
    sys.stdin.readline()


def main():
    operation, raw = sys.argv[1:3]
    data = Path(raw)
    if operation == "hold-catalog":
        with Catalog(data / "catalog.db"):
            _hold()
        return 0
    if operation == "hold-publisher":
        import build_collection as publisher
        with mock.patch.object(publisher, "_publish", side_effect=lambda *a, **k: _hold() or 0):
            return publisher.main(["--base", "newfamily", "--title", "Fixture", "--dry-run",
                                   "--no-brand-logo", "--data-dir", str(data)])
    if operation == "hold-archive":
        import pack_archive
        with mock.patch.object(pack_archive, "CATALOG", data / "catalog.db"), \
                mock.patch.object(pack_archive, "_catalog", side_effect=lambda: _hold() or ({}, {})), \
                mock.patch.object(pack_archive, "_sets", return_value=[]), \
                mock.patch.object(pack_archive, "archive_root", return_value=data / "archive"):
            return pack_archive.sync(None)
    if operation == "hold-ingest":
        import add_media
        from PIL import Image
        image = data / "incoming.png"
        Image.new("RGBA", (100, 100), "red").save(image)
        store = add_media.store_media
        with mock.patch.object(add_media, "store_media", side_effect=lambda *a, **k: _hold() or store(*a, **k)):
            return add_media.main([str(image), "--data-dir", str(data)])
    if operation == "hold-panel":
        import threading
        from http.server import ThreadingHTTPServer
        from urllib import request
        import panel

        def opening(*args, **kwargs):
            catalog = Catalog(*args, **kwargs)
            _hold()
            return catalog

        handler = panel.make_handler([], {}, data / "catalog.db", "fixture-token")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            with mock.patch.object(panel, "Catalog", opening):
                req = request.Request(f"http://127.0.0.1:{server.server_port}/api/save",
                    data=b'{"excluded":[],"known":[]}',
                    headers={"Content-Type": "application/json", "X-Panel-Token": "fixture-token"})
                with request.urlopen(req, timeout=15) as response:
                    return 0 if response.status == 200 else 1
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    if operation in ("migrate", "die-after-second-move"):
        original = migration_bundle.move_file
        moves = 0

        def crash(source, dest, digest, **kwargs):
            nonlocal moves
            original(source, dest, digest, **kwargs)
            if source != dest:
                moves += 1
                if moves == 2:
                    os._exit(73)

        cm.identity.fingerprint = _decode
        if operation == "die-after-second-move":
            migration_bundle.move_file = crash
        return ir.main(["migrate-video-keys", "--apply", "--data-dir", str(data)])
    raise ValueError(f"unknown operation: {operation}")


if __name__ == "__main__":
    raise SystemExit(main())
