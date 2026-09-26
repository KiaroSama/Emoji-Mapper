"""An atomic writer may only truncate or clean up its own temporary inode."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from emojikit import packstate as ps

RUNS_ON_NATIVE_WINDOWS = True


class AtomicStateFiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'state.json'
        self.path.write_text('{"old":true}', encoding='utf-8')
        self.foreign = self.root / 'state.json.tmp'
        self.foreign.write_bytes(b'pre-existing unrelated bytes')

    def assert_foreign_preserved(self):
        self.assertTrue(self.foreign.exists())
        self.assertEqual(self.foreign.read_bytes(), b'pre-existing unrelated bytes')

    def test_existing_temporary_filename_is_never_claimed_or_removed(self):
        ps.write_json_atomic(self.path, {'new': 'سلام'})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {'new': 'سلام'})
        self.assert_foreign_preserved()
        self.assertEqual(set(self.root.iterdir()), {self.path, self.foreign})

    def test_serialization_failure_retains_old_state_and_removes_only_owned_temp(self):
        with self.assertRaises(TypeError):
            ps.write_json_atomic(self.path, {'invalid': object()})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {'old': True})
        self.assert_foreign_preserved()
        self.assertEqual(set(self.root.iterdir()), {self.path, self.foreign})

    def test_fsync_failure_retains_old_state_and_removes_only_owned_temp(self):
        with mock.patch.object(ps.os, 'fsync', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            ps.write_json_atomic(self.path, {'new': True})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {'old': True})
        self.assert_foreign_preserved()
        self.assertEqual(set(self.root.iterdir()), {self.path, self.foreign})

    def test_replace_failure_retains_old_state_and_removes_only_owned_temp(self):
        with mock.patch.object(ps.os, 'replace', side_effect=OSError('destination busy')), self.assertRaises(OSError):
            ps.write_json_atomic(self.path, {'new': True})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {'old': True})
        self.assert_foreign_preserved()
        self.assertEqual(set(self.root.iterdir()), {self.path, self.foreign})

    @unittest.skipUnless(os.name == 'nt', "Windows' transient replace refusal")
    def test_a_transient_windows_refusal_is_retried_to_completion(self):
        """Without the retry, two writers to one file lost a write 13/40 runs.

        Deterministic here: the destination refuses twice, then accepts.
        """
        original, calls = ps.os.replace, []

        def refuse_twice(source, destination):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(13, 'Access is denied')
            return original(source, destination)

        with mock.patch.object(ps.os, 'replace', refuse_twice):
            ps.write_json_atomic(self.path, {'new': True})
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {'new': True})
        self.assertEqual(set(self.root.iterdir()), {self.path, self.foreign})

    @unittest.skipUnless(os.name == 'nt', "Windows' transient replace refusal")
    def test_a_persistent_refusal_still_fails_and_cleans_up(self):
        """A retry that never gives up would turn a real error into a hang."""
        with mock.patch.object(ps, '_REPLACE_DEADLINE', 0.05), \
                mock.patch.object(ps.os, 'replace', side_effect=PermissionError(13, 'denied')), \
                self.assertRaises(PermissionError):
            ps.write_json_atomic(self.path, {'new': True})
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {'old': True})
        self.assert_foreign_preserved()
        self.assertEqual(set(self.root.iterdir()), {self.path, self.foreign})

    @unittest.skipUnless(os.name == 'posix', 'permission bits are a POSIX contract')
    def test_rewriting_an_existing_file_keeps_its_permissions(self):
        """mkstemp creates its file 0600, and os.replace publishes that inode.

        Without carrying the old mode across, every rewrite silently narrowed an
        existing 0644 state file to owner-only -- a change no caller asked for.
        """
        os.chmod(self.path, 0o644)
        ps.write_json_atomic(self.path, {'new': True})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {'new': True})

    def test_overlapping_writes_use_independent_temps_and_publish_complete_json(self):
        barrier = threading.Barrier(2, timeout=5)
        original = ps.os.replace
        failures, sources = [], []

        def replace(source, destination):
            # Only each writer's FIRST attempt meets the other at the barrier:
            # that is what forces the overlap. A Windows retry of the same temp
            # must not wait for a partner that has already gone through.
            first = str(source) not in sources
            sources.append(str(source))
            if first:
                barrier.wait()
            return original(source, destination)

        def write(value):
            try:
                ps.write_json_atomic(self.path, value)
            except (OSError, TypeError, threading.BrokenBarrierError) as exc:
                failures.append(repr(exc))

        bodies = [{'writer': 1, 'value': 'A' * 10000}, {'writer': 2, 'value': 'B' * 1000}]
        with mock.patch.object(ps.os, 'replace', replace):
            threads = [threading.Thread(target=write, args=(body,)) for body in bodies]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(set(sources)), 2)
        self.assertIn(json.loads(self.path.read_text(encoding="utf-8")), bodies)
        self.assert_foreign_preserved()
        self.assertEqual(set(self.root.iterdir()), {self.path, self.foreign})
