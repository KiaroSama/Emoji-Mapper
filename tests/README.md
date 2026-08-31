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
| `test_media.py` | format detection (`emojikit/media.py`) and identity (`emojikit/identity.py`): content/perceptual hashing, `same_image`, static→PNG, GIF→WEBM, Lottie→TGS, and the animated contract (512×512 canvas, frame rate, duration, gzip packaging, bounded decompression) |
| `test_catalog.py` | catalog dedup (exact + perceptual), `file_unique_id` skip, pending/upload tracking, persistence |
| `test_resume_safety.py` | the duplicate-upload paths, driven through `build_pack.main()`: recorded-cursor resume, write-ahead in-flight record, atomic state writes, refusal to guess on unexplained drift, per-set limits |
| `test_telegram_client.py` | `telegram_api.Telegram` on its own: token redaction, the STICKERSET_INVALID retry scope, and what the client accepts as evidence that an upload landed |
| `test_pack_locks.py` | `packstate.exclusive_lock` mechanics: refusal, release on error, stale reclaim and its races, ownership, heartbeat, and the lock-path helpers. (`test_lock_order.py` checks the documented ORDER of the same locks, by AST.) |
| `test_rebuild_dedup_state.py` | the `coins/rebuild_dedup.py` mutation walk: plan → validate → delete the old packs → upload, plus its in-flight reconcile and run lock |
| `test_rebuild_dedup_map.py` | the second phase of the same module: `map_and_fill` resolving `ticker_to_id.json` by image identity under the map lock, and the shared-logo-group guard |
| `test_publish_dedup.py` | verified retries for non-idempotent Bot API calls, live-set reconcile, adopt-on-occupied, recorded fuids |
| `test_build_collection_state.py` | `build_collection`'s state machine: plan/state files failing closed, live-set drift, identity on a recorded position, the unattributed tail, and the CLI contract — everything driven through `_CatalogFixture` |
| `test_publish_contracts.py` | the publisher contracts that are NOT collection state: pack titles as one sequence, the mixed-family layout, the blank-video guard, and the announcement path all three publishers share |
| `test_entry_point_contracts.py` | exit codes and argument validation at the CLI boundary of `fetch_pack`, `make_emoji_pngs`, `panel` and `logsetup` |
| `test_coin_cli_args.py` | the coin tools refuse an unrecognised argument instead of falling through to the live branch — a typo must not publish |
| `test_coin_logo_cache.py` | `fetch_logos` resume: a cached file is re-validated before it is trusted as a logo |
| `test_coin_http.py` | the one pooled `coins/_http.py` client: retry ladder, `Retry-After`, and the paging delay |
| `test_coin_ticker_map.py` | every writer of `ticker_to_id.json` — alias/enhance/provider — serialised so none loses another's update, and one inventory implementation |
| `test_verify_logos.py` | `verify_logos`: the inversion-aware distance, the durable replacement intent bound to its own `--map`, and the fix path's exit codes |
| `test_panel.py` | what the panel builds from the catalog: brand-logo preview, inert item JSON (no script breakout), the similarity order, the published-item filter, and the save-during-reorder window |
| `test_panel_guard.py` | the POST guard (token, loopback Host/Origin, content type, body cap, exact-permutation order) and the two behaviours built on it. `MutationGuard` owns nine `test_*` methods and is subclassed twice, so all three classes must stay in one module — importing the base elsewhere would collect it again rather than move it |
| `test_panel_page.py` | assertions against the served page (`panel.PAGE`): drag-and-drop, undo/redo, the viewport observer, the pack separators and the jump buttons. Those live in the page's own JavaScript, so the document is the only level at which the behaviour exists |
| `test_panel_server.py` | the panel as a process: which Host may reach it, and who owns the port. Real subprocesses and real sockets, so the slowest of the four |
| `test_logsetup.py` | secret redaction, plus a guard that fails if any `.env` secret value appears in a git-tracked file |

`_pack_fixtures.py`, `_rebuild_fixtures.py`, `_cli_fixtures.py`,
`_bc_fixtures.py` and `_panel_fixtures.py` hold the
fakes shared by the modules above them (the PNG builders, `FakeTelegram`,
`RebuildCase`, and the standalone-script loader every entry-point contract
module imports). One copy each, because a duplicated fake drifts away from the
thing it stands in for. The leading underscore is load-bearing:
`-p "test_*.py"` must not collect them as test modules.

A fixture may only be shared if it carries no `test_*` methods and no
TestCase base. One that does multiplies with every importer instead of
moving — which is why `MutationGuard` stays whole in `test_panel_guard.py`.

**Patch the namespace the CALLER reads, not the one that defines the name.**
`build_pack` does `from telegram_api import Telegram`, so the name lives in
`build_pack`'s globals and `patch.object(telegram_api, "Telegram")` leaves
the real client in place — the suite then dials the discard port and sleeps
through the retry ladder. `announce_via_worker` is the mirror image: its
only caller is `announce.announce_packs`, so it must be patched on
`announce`.

`FakeTelegram` binds the **real** `build_pack.Telegram.send_message` rather than
reimplementing it. The retry count and the link-preview flag are guarantees
those tests assert, and a hand-written copy would keep asserting them long after
the shipped method stopped providing them.

## The Cloudflare Worker has its own suite

`worker/` is TypeScript and is **not** collected by the command above, nor by
`scripts\check.ps1` — running it needs Node, which nothing else in this project
does. Run it yourself after touching `worker/`:

```powershell
cd worker
npm ci               # exactly the committed lockfile, like CI
npm run typecheck    # tsc --noEmit
npx vitest run       # 34 tests
```

Same hermetic rule, enforced the same way: the tests stub `fetch`, so nothing in
them can reach Telegram. CI runs these in a dedicated `worker:` job — separate
from the Python matrix, which needs Python and ffmpeg and would otherwise run
this suite once per Python version.

## Fixtures

| Fixture | Purpose | Safe to commit |
|---------|---------|----------------|
| `fixtures/lottie/red_circle_512.json` | minimal valid 512×512 Lottie animation used to exercise TGS packaging/validation and animated content hashing. 512×512 is Telegram's required canvas for animated emoji — a 100×100 fixture would encode the wrong contract | yes (synthetic, no secrets) |

Static images and animated GIFs used by the tests are generated on the fly with
Pillow in temporary directories and are **not** committed. No network access or
real Telegram credentials are required to run the suite.
