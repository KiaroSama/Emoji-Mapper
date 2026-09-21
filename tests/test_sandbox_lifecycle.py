"""Construction, source immutability and lifetime are one clone contract."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import threading
import re
import socket
import time
from urllib import request, error
from pathlib import Path
from unittest import mock

from emojikit import sandbox_clone as sc
from emojikit.catalog import Catalog
from tests.test_panel_sandbox import SandboxFixture, panel_sandbox

RUNS_ON_NATIVE_WINDOWS = True


def database_state(db):
    with contextlib.closing(sqlite3.connect(db)) as con:
        return con.execute('PRAGMA journal_mode').fetchone()[0], list(con.iterdump())


class CloneBoundaries(SandboxFixture):
    def test_cloning_does_not_migrate_or_change_source_journal_mode(self):
        self.fill(plan=True)
        db = self.source / 'catalog.db'
        with contextlib.closing(sqlite3.connect(db)) as con:
            con.execute('PRAGMA journal_mode=DELETE')
            con.execute('DROP TABLE meta')
            con.commit()
        before = database_state(db)
        original = db.read_bytes()
        plan = (self.source / 'pack_plan.json').read_bytes()
        sc.clone_catalog(self.source, self.dest())
        self.assertEqual(database_state(db), before)
        self.assertEqual(db.read_bytes(), original)
        self.assertEqual((self.source / 'pack_plan.json').read_bytes(), plan)
        # A clone may upgrade its OWN schema on first panel startup.
        with Catalog(self.dest() / 'catalog.db') as copied:
            self.assertEqual(len(copied.all_items()), 1)
        self.assertEqual(database_state(db), before)

    def test_a_refused_clone_also_leaves_the_source_database_unchanged(self):
        self.fill()
        db = self.source / 'catalog.db'
        with contextlib.closing(sqlite3.connect(db)) as con:
            con.execute('PRAGMA journal_mode=DELETE')
        (self.source / 'media' / '0.png').unlink()
        before = db.read_bytes()
        with self.assertRaises(sc.CloneRefused):
            sc.clone_catalog(self.source, self.dest())
        self.assertEqual(db.read_bytes(), before)
        self.assertFalse(self.dest().exists())

    def test_project_relative_path_is_not_shadowed_by_a_source_relative_file(self):
        self.fill()
        canonical = self.tmp / 'media'
        canonical.mkdir()
        (canonical / '0.png').write_bytes(b'PROJECT-original')
        (self.source / 'media' / '0.png').write_bytes(b'SOURCE-decoy')
        with contextlib.closing(sqlite3.connect(self.source / 'catalog.db')) as con:
            con.execute("UPDATE items SET file_path='media/0.png'")
            con.commit()
        sc.clone_catalog(self.source, self.dest(), project_root=self.tmp)
        with contextlib.closing(sqlite3.connect(self.dest() / 'catalog.db')) as con:
            path = Path(con.execute('SELECT file_path FROM items').fetchone()[0])
        self.assertEqual(path.read_bytes(), b'PROJECT-original')

    def test_same_length_copy_corruption_is_refused_and_removed(self):
        self.fill()
        copy = sc.shutil.copyfile

        def corrupt(source, dest, *args, **kwargs):
            result = copy(source, dest, *args, **kwargs)
            if Path(dest).parent.name == 'media':
                path = Path(dest)
                path.write_bytes(b'X' * path.stat().st_size)
            return result

        original = (self.source / 'media' / '0.png').read_bytes()
        with mock.patch.object(sc.shutil, 'copyfile', corrupt), self.assertRaises(sc.CloneRefused):
            sc.clone_catalog(self.source, self.dest())
        self.assertFalse(self.dest().exists())
        self.assertEqual((self.source / 'media' / '0.png').read_bytes(), original)

    def test_changed_source_during_copy_is_refused_even_when_length_is_unchanged(self):
        self.fill()
        copy = sc.shutil.copyfile

        def mutate(source, dest, *args, **kwargs):
            result = copy(source, dest, *args, **kwargs)
            if Path(dest).parent.name == 'media':
                path = Path(source)
                path.write_bytes(b'Z' * path.stat().st_size)
            return result

        with mock.patch.object(sc.shutil, 'copyfile', mutate), self.assertRaises(sc.CloneRefused):
            sc.clone_catalog(self.source, self.dest())
        self.assertFalse(self.dest().exists())

    def test_portable_names_do_not_alias_distinct_catalog_keys(self):
        self.fill(keys=('s:a_b', 's_a:b'))
        sc.clone_catalog(self.source, self.dest())
        with contextlib.closing(sqlite3.connect(self.dest() / 'catalog.db')) as con:
            paths = [Path(r[0]) for r in con.execute('SELECT file_path FROM items')]
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual({p.read_bytes() for p in paths}, {b'\x89PNG-0', b'\x89PNG-1'})

    def test_sweep_cannot_claim_a_published_marker_before_clone_returns(self):
        self.fill()
        marker = sc.write_marker
        observations = []

        def marked(directory):
            result = marker(directory)
            observations.append(sc.sweep_stale(self.tmp))
            self.assertTrue((directory / 'catalog.db').is_file())
            return result

        with mock.patch.object(sc, 'write_marker', marked):
            self.assertEqual(sc.clone_catalog(self.source, self.dest()), 1)
        self.assertEqual(observations, [0])

    def test_wrapper_keeps_ownership_from_marker_through_complete_cleanup(self):
        self.fill()
        marker = sc.write_marker
        observations = []
        clone_paths = []

        def marked(directory):
            result = marker(directory)
            clone_paths.append(directory)
            observations.append(sc.sweep_stale(self.tmp))
            return result

        def serve(argv, *, reuse_existing):
            directory = Path(argv[argv.index('--data-dir') + 1])
            self.assertFalse(reuse_existing)
            with Catalog(directory / 'catalog.db') as cat:
                self.assertEqual(len(cat.all_items()), 1)
            observations.append(sc.sweep_stale(self.tmp))
            return 0

        with mock.patch.object(sc, 'write_marker', marked), \
                mock.patch.object(panel_sandbox.tempfile, 'gettempdir', return_value=str(self.tmp)), \
                mock.patch.object(panel_sandbox.panel, 'main', serve), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(panel_sandbox.main(['--source', str(self.source), '--port', '8799']), 0)
        self.assertEqual(observations, [0, 0])
        self.assertFalse(clone_paths[0].exists())
        self.assertTrue(sc.lifetime_lock_path(clone_paths[0]).exists())

    def test_old_marker_protocol_is_left_alone_not_mistaken_for_abandonment(self):
        self.fill()
        sc.clone_catalog(self.source, self.dest())
        marker = self.dest() / sc.MARKER_NAME
        doc = json.loads(marker.read_text())
        doc['version'] = 1
        marker.write_text(json.dumps(doc))
        self.assertEqual(sc.sweep_stale(self.tmp), 0)
        self.assertTrue((self.dest() / 'catalog.db').exists())

    def test_marker_is_rechecked_after_acquiring_the_sweep_lease(self):
        self.fill()
        sc.clone_catalog(self.source, self.dest())
        real = sc.exclusive_lock

        @contextlib.contextmanager
        def change_after_probe(path):
            with real(path):
                (self.dest() / sc.MARKER_NAME).unlink()
                yield

        with mock.patch.object(sc, 'exclusive_lock', change_after_probe):
            self.assertEqual(sc.sweep_stale(self.tmp), 0)
        self.assertTrue((self.dest() / 'catalog.db').exists())

    def test_process_death_releases_construction_lease_for_safe_reclamation(self):
        self.fill()
        code = ('import sys, tests; from pathlib import Path; '
                'from emojikit.sandbox_clone import sandbox_session; '
                '\nwith sandbox_session(Path(sys.argv[1]), Path(sys.argv[2])) as count:'
                '\n print("READY", flush=True)'
                '\n input()')
        child = subprocess.Popen([sys.executable, '-c', code, str(self.source), str(self.dest())],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8')
        ready, lines = threading.Event(), []

        def read():
            lines.append(child.stdout.readline())
            ready.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(10), 'child did not acquire its clone')
            self.assertEqual(lines, ['READY\n'])
            before = hashlib.sha256((self.dest() / 'catalog.db').read_bytes()).hexdigest()
            self.assertEqual(sc.sweep_stale(self.tmp), 0)
            self.assertEqual(hashlib.sha256((self.dest() / 'catalog.db').read_bytes()).hexdigest(), before)
        finally:
            child.kill()
            child.communicate(timeout=10)
            reader.join(5)
        self.assertEqual(sc.sweep_stale(self.tmp), 1)
        self.assertFalse((self.dest() / 'catalog.db').exists())
        self.assertTrue(sc.lifetime_lock_path(self.dest()).exists())


    def test_real_wrapper_http_save_changes_only_the_owned_clone(self):
        self.fill(keys=('s:' + 'a' * 32, 's:' + 'b' * 32))
        source_before = database_state(self.source / 'catalog.db')
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        code = ('import sys, tests, tempfile; from scripts import panel_sandbox; '
                'tempfile.gettempdir=lambda:sys.argv[1]; '
                'raise SystemExit(panel_sandbox.main(sys.argv[2:]))')
        child = subprocess.Popen([sys.executable, '-c', code, str(self.tmp),
                                  '--source', str(self.source), '--port', str(port),
                                  '--bot-username', 'FixtureBot'], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8')
        clone = None
        try:
            deadline = time.monotonic() + 15
            html = None
            while time.monotonic() < deadline and child.poll() is None:
                try:
                    with request.urlopen(f'http://127.0.0.1:{port}/', timeout=0.5) as response:
                        html = response.read().decode('utf-8')
                        break
                except (OSError, error.URLError):
                    time.sleep(0.05)
            self.assertIsNotNone(html, 'actual sandbox panel did not serve its clone')
            token = re.search(r'const TOKEN = "([^"]+)"', html)[1]
            cards = json.loads(re.search(r'<script id="items-data" type="application/json">(.*?)</script>',
                                         html, re.S)[1])
            keys = [card['key'] for card in cards if not card.get('isLogo')]
            body = {'known': keys, 'excluded': [keys[0]], 'packs': []}
            req = request.Request(f'http://127.0.0.1:{port}/api/save', data=json.dumps(body).encode(),
                                  headers={'Content-Type': 'application/json', 'X-Panel-Token': token})
            with request.urlopen(req, timeout=5) as response:
                self.assertTrue(json.loads(response.read())['ok'])
            candidates = [p for p in self.tmp.glob(sc.TMP_PREFIX + '*')
                          if sc.marker_is_self_describing(p)]
            self.assertEqual(len(candidates), 1)
            clone = candidates[0]
            with contextlib.closing(sqlite3.connect(clone / 'catalog.db')) as con:
                self.assertEqual(con.execute('SELECT content_key FROM items WHERE included=0').fetchall(),
                                 [(keys[0],)])
            self.assertEqual(database_state(self.source / 'catalog.db'), source_before)
            self.assertEqual(sc.sweep_stale(self.tmp), 0)
        finally:
            child.kill()
            _, stderr = child.communicate(timeout=10)
        self.assertIsNotNone(clone, stderr)
        self.assertEqual(sc.sweep_stale(self.tmp), 1)
        self.assertFalse(clone.exists())
        self.assertEqual(database_state(self.source / 'catalog.db'), source_before)

    def test_panel_exception_cleans_owned_data_and_restores_environment(self):
        import os
        self.fill()
        before = dict(os.environ)
        with mock.patch.object(panel_sandbox.tempfile, 'gettempdir', return_value=str(self.tmp)), \
                mock.patch.object(panel_sandbox.panel, 'main', side_effect=RuntimeError('panel startup failed')), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'startup failed'):
            panel_sandbox.main(['--source', str(self.source), '--port', '8799'])
        self.assertEqual(os.environ, before)
        self.assertFalse(list(self.tmp.glob(sc.TMP_PREFIX + '*')))
        self.assertTrue((self.source / 'catalog.db').is_file())
