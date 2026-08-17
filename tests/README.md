# Tests

Run from the repository root:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py"
```

**`-t .` is required.** Without it the tests directory becomes the top-level,
modules load as `test_x` instead of `tests.test_x`, and `tests/__init__.py`
never runs — which disables the guard described below.
`SuiteIsHermetic.test_the_guard_was_installed_before_the_test_modules` fails
loudly if the suite is started without it, and names the correct command.

That canary reads a flag sampled **while the test modules were being imported**,
not the live state of the patch. The earlier form asked whether `socket.connect`
was patched at assert time — but one test does `import tests`, so the package
installed itself mid-run and the canary passed while the real `.env` values had
already been read into the environment. A check something later can satisfy
cannot fail at the moment it is needed.

Tests use Python's stdlib `unittest` (no extra dependencies). Video tests are
skipped automatically when `ffmpeg`/`ffprobe` are not on `PATH`.

## The suite never touches the real network

`tests/__init__.py` runs before any test module and scrubs every
token/API-key-shaped environment variable, points `TELEGRAM_API_BASE` at the
discard port, stops `build_pack.load_env()` from reading `.env`, and refuses
outbound sockets to anything but loopback (`NetworkAccessDenied`).

The `.env` half no longer depends on that file running at all:
`build_pack.load_env()` refuses to read `.env` whenever a test runner owns the
process, decided from the entry point's own `__spec__`. The package-level scrub
could only remove credential-*shaped* names and only protected what was imported
after it, so anything else in `.env` stayed visible for a whole run.

This exists because a test meant only to check a CLI usage error once reached
live Telegram and replaced a sticker in a published pack. Inject a fake session
rather than adding an opt-out.

## Layout

| File | Covers |
|------|--------|
| `test_media.py` | format detection, content/perceptual hashing, static→PNG, GIF→WEBM, Lottie→TGS, and the animated contract (512×512 canvas, frame rate, duration, gzip packaging, bounded decompression) |
| `test_catalog.py` | catalog dedup (exact + perceptual), `file_unique_id` skip, pending/upload tracking, persistence |
| `test_resume_safety.py` | the duplicate-upload paths, driven through `build_pack.main()`: recorded-cursor resume, write-ahead in-flight record, atomic state writes, refusal to guess on unexplained drift, per-set limits |
| `test_telegram_client.py` | `build_pack.Telegram` on its own: token redaction, the STICKERSET_INVALID retry scope, and what the client accepts as evidence that an upload landed |
| `test_pack_locks.py` | `exclusive_lock` mechanics: refusal, release on error, stale reclaim and its races, ownership, heartbeat, and the lock-path helpers. (`test_lock_order.py` checks the documented ORDER of the same locks, by AST.) |
| `test_rebuild_dedup_state.py` | the `coins/rebuild_dedup.py` mutation walk: plan → validate → delete the old packs → upload, plus its in-flight reconcile and run lock |
| `test_rebuild_dedup_map.py` | the second phase of the same module: `map_and_fill` resolving `ticker_to_id.json` by image identity under the map lock, and the shared-logo-group guard |
| `test_publish_dedup.py` | verified retries for non-idempotent Bot API calls, live-set reconcile, adopt-on-occupied, recorded fuids |
| `test_panel.py` | brand-logo preview, inert item JSON (no script breakout), and the mutation guard (token, loopback Host/Origin, content type, body cap, exact-permutation order) |
| `test_logsetup.py` | secret redaction, plus a guard that fails if any `.env` secret value appears in a git-tracked file |

`_pack_fixtures.py` and `_rebuild_fixtures.py` hold the fakes shared by the
modules above them (the PNG builders, `FakeTelegram`, `RebuildCase`). One copy
each, because a duplicated fake drifts away from the thing it stands in for.
The leading underscore is load-bearing: `-p "test_*.py"` must not collect them
as test modules.

## Fixtures

| Fixture | Purpose | Safe to commit |
|---------|---------|----------------|
| `fixtures/lottie/red_circle_512.json` | minimal valid 512×512 Lottie animation used to exercise TGS packaging/validation and animated content hashing. 512×512 is Telegram's required canvas for animated emoji — a 100×100 fixture would encode the wrong contract | yes (synthetic, no secrets) |

Static images and animated GIFs used by the tests are generated on the fly with
Pillow in temporary directories and are **not** committed. No network access or
real Telegram credentials are required to run the suite.
