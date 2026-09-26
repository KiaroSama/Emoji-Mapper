"""The sandbox must isolate the ACCOUNT, not only the catalog.

`scripts/panel_sandbox.py` exists because an agent's synthetic drags rewrote
the owner's real ordering. It cloned the catalog and then handed the child its
own environment untouched, so the cloned-catalog panel still called
`_detect_bot_username()` -> `load_env()` -> `getMe` against live Telegram with
the real token. Cloning the data while keeping the credentials is half a
sandbox.

These assert the scrub directly rather than launching a panel: the value under
test is a dict, and a subprocess would prove the same thing far more slowly.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import panel_sandbox  # noqa: E402 - needs the paths above
from emojikit import sandbox_clone  # noqa: E402
from emojikit.catalog import Catalog  # noqa: E402
from emojikit.packstate import exclusive_lock  # noqa: E402

# Two things here are only real on native Windows, so a green Linux run proves
# neither. `exclusive_lock` uses msvcrt byte-range locking there and `flock`
# elsewhere -- different code, and the lease tests are what exercise it. And a
# colon is a LEGAL filename character on Linux, so the test that a key like
# `s:<hex>` becomes a real file can only fail on the platform where a colon
# opens an alternate data stream. This project is Windows-first.
RUNS_ON_NATIVE_WINDOWS = True


class TheSandboxChildGetsNoCredentials(unittest.TestCase):

    def child(self, **extra):
        with mock.patch.dict(os.environ, extra, clear=False):
            return panel_sandbox.scrubbed_environment()

    def test_dotenv_reading_is_switched_off_for_the_child(self):
        """The flag load_env() already honours for the suite; without it the
        child re-reads the real .env no matter what the parent environment holds."""
        self.assertEqual(self.child()["NUMERA_EMOJI_MAPPER_NO_DOTENV"], "1")

    def test_an_exported_token_does_not_survive_into_the_child(self):
        child = self.child(GENERAL_BOT_TOKEN="111:live", COIN_BOT_TOKEN="222:live")
        self.assertNotIn("GENERAL_BOT_TOKEN", child)
        self.assertNotIn("COIN_BOT_TOKEN", child)

    def test_every_key_the_template_names_is_dropped(self):
        """`.env.example` is the authoritative list, so a credential added to the
        project later is scrubbed without editing this file."""
        template = ROOT / ".env.example"
        if not template.is_file():
            self.skipTest("no .env.example in this checkout")
        named = [line.split("=", 1)[0].strip()
                 for line in template.read_text(encoding="utf-8").splitlines()
                 if "=" in line and not line.lstrip().startswith("#")]
        self.assertTrue(named, ".env.example names no keys; the scrub proves nothing")
        child = self.child(**{key: "live-value" for key in named})
        self.assertEqual([key for key in named if key in child], [])

    def test_a_credential_shape_is_dropped_even_when_the_template_is_silent(self):
        child = self.child(SOME_PRIVATE_TOKEN="x", DEPLOY_SECRET="y", OWNER_ID="7")
        for key in ("SOME_PRIVATE_TOKEN", "DEPLOY_SECRET", "OWNER_ID"):
            self.assertNotIn(key, child)

    def test_the_ordinary_environment_still_reaches_the_child(self):
        """Scrubbing must not break the child: it still has to find Python."""
        child = self.child(EMOJI_SANDBOX_CANARY="kept")
        self.assertEqual(child.get("EMOJI_SANDBOX_CANARY"), "kept")
        self.assertIn("PATH", {key.upper() for key in child})


if __name__ == "__main__":
    unittest.main()


class SandboxFixture(unittest.TestCase):
    """A disposable source catalog. Never `collection/` -- see the module docstring."""

    def setUp(self):
        # RESOLVED once. On a Windows runner `TEMP` is an 8.3 short name
        # (`C:\Users\RUNNER~1\...`), and `Path.resolve()` expands it, so a
        # stored path compared against an unresolved fixture path looks like it
        # escaped the clone when it did not. It passes on a machine whose user
        # name is short enough not to need the alias -- which is why this only
        # showed up in the native-Windows job.
        self.tmp = Path(tempfile.mkdtemp(prefix="sandbox-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = self.tmp / "src"
        (self.source / "media").mkdir(parents=True)
        self.outside = self.tmp / "archive"     # stands in for the owner's F: archive
        self.outside.mkdir()

    def media(self, directory: Path, name: str, body: bytes = b"\x89PNG-fixture") -> Path:
        path = directory / name
        path.write_bytes(body)
        return path

    def fill(self, *, keys=("s:" + "a" * 32,), outside=False, plan=False):
        with Catalog(self.source / "catalog.db") as cat:
            for i, key in enumerate(keys):
                where = self.outside if outside else self.source / "media"
                path = self.media(where, f"{i}.png", b"\x89PNG-" + str(i).encode())
                cat.add(content_key=key, fmt="static", file_path=path)
        if plan:
            (self.source / "pack_plan.json").write_text(
                json.dumps({"version": 1, "targets": [], "known": [], "excluded": [],
                            "moves": [], "held": []}), encoding="utf-8")

    def dest(self) -> Path:
        return self.tmp / f"{sandbox_clone.TMP_PREFIX}dest"


class TheCloneSharesNothingWithTheSource(SandboxFixture):

    def test_no_cloned_file_shares_storage_with_its_source(self):
        """Hard links made the 'safe copy' the SAME BYTES: editing the clone
        edited the original, which is the defect rather than an optimisation."""
        self.fill()
        dest = self.dest()
        sandbox_clone.clone_catalog(self.source, dest)
        source_files = sorted((self.source / "media").iterdir())
        clone_files = sorted((dest / "media").iterdir())
        self.assertTrue(clone_files)
        # strict: a differing count is itself the failure, not something
        # to zip away silently.
        for a, b in zip(source_files, clone_files, strict=True):
            self.assertNotEqual((a.stat().st_dev, a.stat().st_ino),
                                (b.stat().st_dev, b.stat().st_ino))
            self.assertEqual(a.read_bytes(), b.read_bytes(), "content must still match")

    def test_a_media_file_outside_the_source_is_copied_in_not_referenced(self):
        """The old code hit `except ValueError: continue` for these, leaving the
        sandbox serving -- and able to write over -- the owner's real archive."""
        self.fill(outside=True)
        dest = self.dest()
        sandbox_clone.clone_catalog(self.source, dest)
        with sqlite3.connect(dest / "catalog.db") as con:
            paths = [row[0] for row in con.execute("SELECT file_path FROM items")]
        self.assertTrue(paths)
        for raw in paths:
            self.assertTrue(Path(raw).resolve().is_relative_to(dest),
                            f"{raw} still points outside the clone")

    def test_a_project_relative_media_path_is_copied_in(self):
        """The third shape a stored path takes. Rows written relative to the
        project root resolved to nothing under the old `relative_to(source)`
        attempt, so they kept the original reference."""
        rel_dir = ROOT / "tests" / "__sandbox_rel__"
        rel_dir.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, rel_dir, True)
        (rel_dir / "rel.png").write_bytes(b"PNG-relative")
        with Catalog(self.source / "catalog.db") as cat:
            cat.add(content_key="s:" + "r" * 32, fmt="static",
                    file_path=Path("tests/__sandbox_rel__/rel.png"))
        dest = self.dest()
        sandbox_clone.clone_catalog(self.source, dest)
        with sqlite3.connect(dest / "catalog.db") as con:
            (raw,) = con.execute(
                "SELECT file_path FROM items WHERE content_key=?",
                ("s:" + "r" * 32,)).fetchone()
        self.assertTrue(Path(raw).resolve().is_relative_to(dest))
        self.assertEqual(Path(raw).read_bytes(), b"PNG-relative")

    def test_a_key_with_colons_becomes_a_real_file(self):
        """`content_key` is `s:<hex>` and `collision_key` is `s:<hex>:<hex>`. On
        NTFS a colon opens an alternate data stream, so a key used verbatim as a
        filename is not a file -- and this project is Windows-first."""
        keys = ("s:" + "b" * 32, "s:" + "c" * 32 + ":" + "d" * 32)
        self.fill(keys=keys)
        dest = self.dest()
        sandbox_clone.clone_catalog(self.source, dest)
        for path in (dest / "media").iterdir():
            self.assertNotIn(":", path.name)
            self.assertTrue(path.is_file() and path.stat().st_size > 0)
        with sqlite3.connect(dest / "catalog.db") as con:
            for (raw,) in con.execute("SELECT file_path FROM items"):
                self.assertTrue(Path(raw).is_file(), f"{raw} is not a real file")

    def test_rows_still_only_in_the_write_ahead_log_are_cloned(self):
        """`shutil.copy2` copied `catalog.db` and never its `-wal`, so committed
        rows could be missing -- at worst the clone arrived without its tables."""
        self.fill(keys=tuple("s:" + ch * 32 for ch in "efg"))
        con = sqlite3.connect(self.source / "catalog.db")
        try:
            con.execute("PRAGMA journal_mode=WAL")
            # A REAL file: a row naming a missing one is refused by design,
            # which is a different test (see the missing-media case below).
            late = self.media(self.source / "media", "late.png", b"PNG-late-fixture")
            con.execute("INSERT INTO items (content_key, format, file_path, "
                        "created_utc) VALUES ('s:inwal', 'static', ?, 'now')",
                        (str(late),))
            con.commit()          # committed, but left in the WAL: no checkpoint
            expected = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            dest = self.dest()
            sandbox_clone.clone_catalog(self.source, dest)
        finally:
            con.close()
        with sqlite3.connect(dest / "catalog.db") as clone:
            self.assertEqual(clone.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                             expected)

    def test_the_curation_plan_travels_with_the_clone(self):
        self.fill(plan=True)
        dest = self.dest()
        sandbox_clone.clone_catalog(self.source, dest)
        self.assertTrue((dest / "pack_plan.json").is_file())

    def test_a_source_without_a_plan_still_clones(self):
        self.fill()
        dest = self.dest()
        self.assertEqual(sandbox_clone.clone_catalog(self.source, dest), 1)


class TheCloneRefusesRatherThanLie(SandboxFixture):

    def test_an_occupied_destination_is_never_erased(self):
        self.fill()
        dest = self.dest()
        dest.mkdir()
        keep = dest / "someone-elses-work.txt"
        keep.write_text("precious", encoding="utf-8")
        with self.assertRaises(sandbox_clone.CloneRefused):
            sandbox_clone.clone_catalog(self.source, dest)
        self.assertEqual(keep.read_text(encoding="utf-8"), "precious")

    def test_a_destination_inside_the_source_is_refused(self):
        self.fill()
        with self.assertRaises(sandbox_clone.CloneRefused):
            sandbox_clone.clone_catalog(self.source, self.source / "inner")

    def test_a_source_with_no_catalog_is_refused(self):
        with self.assertRaises(sandbox_clone.CloneRefused):
            sandbox_clone.clone_catalog(self.tmp / "empty", self.dest())

    def test_an_interrupted_migration_refuses_and_leaves_nothing_behind(self):
        """`Catalog` acquires `writer()`, which raises on the journal. The clone
        inherits that refusal rather than snapshotting a moving target."""
        from emojikit.maintenance import JOURNAL_NAME
        from emojikit.packstate import LockBusy
        self.fill()
        (self.source / JOURNAL_NAME).write_text("{}", encoding="utf-8")
        dest = self.dest()
        with self.assertRaises(LockBusy):
            sandbox_clone.clone_catalog(self.source, dest)
        self.assertFalse(dest.exists(), "a refused clone must leave no directory")

    def test_a_source_another_writer_holds_is_refused(self):
        """`Catalog` takes `writer()`, which is non-blocking -- so a contended
        source refuses at once instead of hanging a sandbox start-up."""
        from emojikit.packstate import LockBusy
        self.fill()
        dest = self.dest()
        with Catalog(self.source / "catalog.db"):
            import subprocess
            probe = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, r'%s');"
                 "from emojikit.sandbox_clone import clone_catalog;"
                 "clone_catalog(r'%s', r'%s')" % (ROOT, self.source, dest)],
                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(probe.returncode, 0, probe.stdout)
        self.assertIn(LockBusy.__name__, probe.stderr)
        self.assertFalse(dest.exists(), "a refused clone leaves no directory")

    def test_a_missing_media_file_abandons_the_clone_entirely(self):
        """FR-007: a clone finished with one row still pointing at production is
        worse than no clone, because it looks safe."""
        self.fill()
        for path in (self.source / "media").iterdir():
            path.unlink()
        dest = self.dest()
        with self.assertRaises(sandbox_clone.CloneRefused):
            sandbox_clone.clone_catalog(self.source, dest)
        self.assertFalse(dest.exists())
        self.assertTrue((self.source / "catalog.db").is_file(), "source untouched")


class TheWrapperRefusesAnythingOutsideItsAllowlist(unittest.TestCase):
    """The defect this feature was opened for: forwarded options landed AFTER
    the wrapper's own `--data-dir`, so a `--data-dir collection` passed through
    won, and the panel served the owner's live catalog while the wrapper printed
    that the live catalog was not served."""

    def refuse(self, argv):
        # argparse prints usage to stderr on refusal; captured so a passing run
        # stays readable.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                panel_sandbox.build_parser().parse_args(argv)
        self.assertNotEqual(caught.exception.code, 0)

    def test_a_data_directory_override_cannot_be_smuggled_through(self):
        self.refuse(["--data-dir", "collection"])

    def test_an_unknown_option_is_not_forwarded(self):
        self.refuse(["--nonsense"])

    def test_abbreviations_are_refused(self):
        """Unambiguous today, a different option the day another is added."""
        self.refuse(["--sou", "x"])
        self.refuse(["--wi", "3"])

    def test_the_presentation_options_are_accepted_and_reach_the_panel(self):
        args = panel_sandbox.build_parser().parse_args(
            ["--all", "--with-pack", "3", "--with-pack", "4", "--bot-username", "b"])
        argv = panel_sandbox.panel_arguments(args, Path("/clone"))
        self.assertEqual(argv[:2], ["--data-dir", str(Path("/clone"))])
        self.assertIn("--all", argv)
        self.assertEqual(argv.count("--with-pack"), 2)
        self.assertIn("b", argv)
        self.assertEqual(argv.count("--data-dir"), 1, "exactly one, built here")

    def test_the_real_panels_port_is_refused(self):
        with self.assertRaises(SystemExit):
            panel_sandbox.main(["--port", str(panel_sandbox.PANEL_PORT)])

    def test_a_number_that_is_not_a_port_is_refused(self):
        for bad in ("0", "70000"):
            with self.assertRaises(SystemExit):
                panel_sandbox.main(["--port", bad])


class ThePanelCanRunInProcessWithoutAdoptingAListener(unittest.TestCase):

    def setUp(self):
        self.no_catalog = Path(tempfile.mkdtemp(prefix="sandbox-nocat-"))
        self.addCleanup(shutil.rmtree, self.no_catalog, True)

    def test_reuse_disabled_reaches_neither_reopen_call_site(self):
        """Guarding only the first left the defect reachable by the second --
        the post-bind-failure recovery path, which is exactly the one a sandbox
        takes when something else already holds its port."""
        from emojikit import panel
        with mock.patch.object(
                panel, "reopen_existing",
                side_effect=AssertionError("a listener was adopted")) as spy:
            # A data directory with no catalog: main reports that and returns
            # before it would ever bind, which is all this needs. The port is a
            # valid one so the panel's own parser has nothing to complain about.
            code = panel.main(["--data-dir", str(self.no_catalog), "--port", "8798"],
                              reuse_existing=False)
            self.assertNotEqual(code, 0)
            spy.assert_not_called()

    def test_main_still_reads_sys_argv_when_given_none(self):
        import inspect
        from emojikit import panel
        signature = inspect.signature(panel.main)
        self.assertIs(signature.parameters["argv"].default, None)
        self.assertIs(signature.parameters["reuse_existing"].default, True,
                      "ordinary command-line runs must keep reuse")


class TheSweepReclaimsOnlyWhatItCanProveIsAbandoned(SandboxFixture):
    """The old sweep matched the name prefix and rmtree'd every hit, so starting
    a second sandbox deleted the catalog the first one was still serving."""

    def sandbox_dir(self, name, *, marker=True, foreign=False):
        path = self.tmp / f"{sandbox_clone.TMP_PREFIX}{name}"
        path.mkdir()
        (path / "catalog.db").write_text("data", encoding="utf-8")
        if marker:
            sandbox_clone.write_marker(path)
            if foreign:
                doc = json.loads((path / sandbox_clone.MARKER_NAME)
                                 .read_text(encoding="utf-8"))
                doc["directory"] = str(self.tmp / "somewhere-else")
                (path / sandbox_clone.MARKER_NAME).write_text(
                    json.dumps(doc), encoding="utf-8")
        return path

    def test_an_abandoned_sandbox_is_reclaimed(self):
        old = self.sandbox_dir("dead")
        self.assertEqual(sandbox_clone.sweep_stale(self.tmp), 1)
        self.assertFalse((old / "catalog.db").exists())

    def test_a_sandbox_whose_lease_is_held_survives(self):
        live = self.sandbox_dir("live")
        with exclusive_lock(sandbox_clone.lifetime_lock_path(live)):
            self.assertEqual(sandbox_clone.sweep_stale(self.tmp), 0)
        self.assertTrue((live / "catalog.db").exists(),
                        "a served sandbox must not be deleted by the next start")

    def test_an_unmarked_directory_is_left_alone(self):
        plain = self.sandbox_dir("unmarked", marker=False)
        self.assertEqual(sandbox_clone.sweep_stale(self.tmp), 0)
        self.assertTrue((plain / "catalog.db").exists())

    def test_a_marker_naming_another_directory_is_foreign(self):
        moved = self.sandbox_dir("moved", foreign=True)
        self.assertEqual(sandbox_clone.sweep_stale(self.tmp), 0)
        self.assertTrue((moved / "catalog.db").exists())

    def test_a_malformed_marker_is_not_proof_of_abandonment(self):
        broken = self.sandbox_dir("broken")
        (broken / sandbox_clone.MARKER_NAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(sandbox_clone.sweep_stale(self.tmp), 0)
        self.assertTrue((broken / "catalog.db").exists())

    def test_a_reclaimed_directory_keeps_its_lock_file(self):
        """Unlinking a locked inode on POSIX lets two processes each hold a lock
        at one path and each believe it is alone -- the hole `exclusive_lock`'s
        own docstring records. A tombstone costs nothing."""
        # The LIFETIME lock, which is the one the sweep takes and therefore the
        # one whose inode must not be unlinked. The catalog's writer lock is a
        # different file and not this test's subject.
        old = self.sandbox_dir("dead")
        lock = sandbox_clone.lifetime_lock_path(old)
        with exclusive_lock(lock):
            pass                       # create the lock file, then release it
        self.assertTrue(lock.exists())
        self.assertEqual(sandbox_clone.sweep_stale(self.tmp), 1)
        self.assertFalse((old / "catalog.db").exists())
        self.assertTrue(lock.exists(), "the lock tombstone must survive")

    def test_a_symlink_is_never_followed(self):
        target = self.tmp / "real-data"
        target.mkdir()
        (target / "precious.txt").write_text("keep", encoding="utf-8")
        link = self.tmp / f"{sandbox_clone.TMP_PREFIX}link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this account cannot create symbolic links")
        self.assertEqual(sandbox_clone.sweep_stale(self.tmp), 0)
        self.assertTrue((target / "precious.txt").exists())


class TheSandboxHoldsItsLeaseForTheLifeOfTheServer(SandboxFixture):
    """SC-005: a killed sandbox stops serving and its directory is reclaimed.

    Split deliberately into the half that can be wrong and the half that cannot.

    The half that CAN be wrong is the wiring -- whether the wrapper takes the
    lock around the served panel at all, and whether the panel it runs is the
    in-process one with reuse off. That is asserted here, from inside a stubbed
    `panel.main`, deterministically and in milliseconds.

    The half that cannot be wrong is the release: `exclusive_lock` is an OS lock
    and the OS drops it when the process exits, which its own docstring records
    and the whole project already depends on. A subprocess test of that was
    written first and thrown away: it raced the wrapper's own lifecycle (the
    panel returns, `finally` deletes the clone) and spent seconds re-proving
    what the operating system guarantees. `test_a_sandbox_whose_lease_is_held_survives`
    already covers the consequence that matters -- a held lease is skipped by
    the sweep.
    """

    def test_the_lease_is_held_while_the_panel_runs_and_the_sweep_skips_it(self):
        self.fill()
        seen = {}

        def fake_panel_main(argv, *, reuse_existing=True):
            seen["argv"] = argv
            seen["reuse_existing"] = reuse_existing
            clone = Path(argv[argv.index("--data-dir") + 1])
            seen["clone"] = clone
            # The decisive assertion: from here, mid-serve, the sweep must find
            # this directory busy and leave it alone.
            seen["swept"] = sandbox_clone.sweep_stale(clone.parent)
            seen["catalog_alive"] = (clone / "catalog.db").is_file()
            seen["dotenv_off"] = os.environ.get("NUMERA_EMOJI_MAPPER_NO_DOTENV")
            return 0

        before = dict(os.environ)
        # Contained on purpose. Left alone, the wrapper clones into the REAL
        # system temp and the sweep above would run there -- so this test could
        # delete a sandbox the owner was actually using.
        with mock.patch.object(panel_sandbox.panel, "main", fake_panel_main),                 mock.patch.object(panel_sandbox.tempfile, "gettempdir",
                                  return_value=str(self.tmp)),                 contextlib.redirect_stdout(io.StringIO()):
            code = panel_sandbox.main(["--source", str(self.source), "--port", "8799"])

        self.assertEqual(code, 0)
        self.assertIs(seen["reuse_existing"], False, "no listener may be adopted")
        self.assertEqual(seen["swept"], 0, "the sweep reclaimed a LIVE sandbox")
        self.assertTrue(seen["catalog_alive"])
        self.assertEqual(seen["dotenv_off"], "1", "credentials must be off while serving")
        self.assertEqual(os.environ, before, "the environment must be restored")
        self.assertFalse(seen["clone"].exists(), "the clone is deleted on exit")


class TheLifetimeLeaseDoesNotBlockThePanelItStarts(SandboxFixture):
    """The regression this class exists for.

    The wrapper held `lock_path(tmp)` for the server's lifetime -- which is
    `tmp/.maintenance.lock`, the exact file `Catalog(tmp/"catalog.db")` takes
    through `maintenance.writer()`. `exclusive_lock` is not reentrant, so the
    panel died with `LockBusy` naming the wrapper's own pid and the sandbox
    served nothing.

    It shipped green because the test that covered the lifetime wiring stubbed
    `panel.main` with a function that never opened a Catalog. A stub that does
    not do the one thing the real code does first proves the one thing that
    cannot fail.
    """

    def test_the_panel_can_open_its_catalog_while_the_lease_is_held(self):
        self.fill()
        dest = self.dest()
        sandbox_clone.clone_catalog(self.source, dest)
        with exclusive_lock(sandbox_clone.lifetime_lock_path(dest)):
            # Exactly what panel.main() does before it serves anything.
            with Catalog(dest / "catalog.db") as cat:
                self.assertEqual(len(cat.all_items()), 1)

    def test_the_wrapper_lets_the_real_panel_reach_its_catalog(self):
        """Through the wrapper, not around it: the stub does what the panel
        does first, so the wrapper's own lock is what is under test."""
        self.fill()
        seen = {}

        def panel_that_opens_its_catalog(argv, *, reuse_existing=True):
            clone = Path(argv[argv.index("--data-dir") + 1])
            with Catalog(clone / "catalog.db") as cat:
                seen["items"] = len(cat.all_items())
            return 0

        with mock.patch.object(panel_sandbox.panel, "main", panel_that_opens_its_catalog), \
                mock.patch.object(panel_sandbox.tempfile, "gettempdir",
                                  return_value=str(self.tmp)), \
                contextlib.redirect_stdout(io.StringIO()):
            code = panel_sandbox.main(["--source", str(self.source), "--port", "8797"])
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("items"), 1,
                         "the panel never got to open its catalog")

    def test_the_lifetime_lock_is_not_the_catalogs_writer_lock(self):
        """Named so a future edit cannot quietly point them at one file again."""
        from emojikit.maintenance import lock_path
        self.assertNotEqual(sandbox_clone.lifetime_lock_path(self.tmp).name,
                            lock_path(self.tmp).name)


class TheScrubLeavesTheProcessAbleToRun(unittest.TestCase):
    """The scrub removes credentials, not the operating system.

    `scrubbed_process_environment` cleared `os.environ` and only then called
    `scrubbed_environment()`, which reads `os.environ` -- by that point empty.
    The panel therefore ran with one variable, and Winsock could not create a
    socket at all (`WinError 10106`). The existing test called
    `scrubbed_environment()` directly, where the environment is still intact, so
    it passed while the context manager it stands for was wiping everything.
    """

    def test_the_machine_environment_survives_inside_the_context(self):
        with mock.patch.dict(os.environ, {"GENERAL_BOT_TOKEN": "111:live"}, clear=False):
            with panel_sandbox.scrubbed_process_environment():
                inside = dict(os.environ)
        self.assertEqual(inside.get("NUMERA_EMOJI_MAPPER_NO_DOTENV"), "1")
        self.assertNotIn("GENERAL_BOT_TOKEN", inside, "credentials must still go")
        present = {k.upper() for k in inside}
        self.assertIn("PATH", present, "PATH is gone; the process cannot run")
        if sys.platform == "win32":
            # Windows-only, and the one that actually bit: without SystemRoot
            # Winsock cannot load its service providers and the panel died with
            # WinError 10106 before it could open a socket. There is no such
            # variable on Linux, so asserting it unconditionally fails CI for a
            # reason that has nothing to do with the scrub.
            self.assertIn("SYSTEMROOT", present,
                          "SystemRoot is gone; Winsock cannot initialise")
        self.assertGreater(len(inside), 5, "the environment was wiped, not scrubbed")

    def test_the_environment_is_restored_afterwards(self):
        before = dict(os.environ)
        with panel_sandbox.scrubbed_process_environment():
            pass
        self.assertEqual(dict(os.environ), before)
