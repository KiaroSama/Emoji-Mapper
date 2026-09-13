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

One module is the exception: `test_panel_browser.py` needs playwright and a
Chromium build, and it **raises** rather than skipping when they are missing —
a browser test that reports green on a machine with no browser is worse than no
browser test at all.

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m playwright install chromium
```

Set `EMOJI_MAPPER_NO_BROWSER_TESTS=1` to opt out on purpose. CI does exactly
that in the Python matrix and runs the module in its own `panel-browser:` job
instead: it tests JavaScript, so once per Python version would download
Chromium twice to prove the same thing.

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
| `test_media_reencode.py` | owner rule 1 — never republish another pack's file byte-for-byte — and the requirement pulling against it: the result must be pixel-identical, because the content key is computed from decoded pixels and a lossy re-compress would split one catalog row into two. Also `same_image` across a re-encode |
| `test_media_bounds.py` | the bounds around ingest: decompression limits, the perceptual-hash threshold range, and the signed storage conversion |
| `test_media_fitting.py` | `fit_100` must not touch opacity. It pasted the image using ITSELF as the mask — a composite against the transparent canvas, so colour came back multiplied by alpha and alpha squared |
| `test_identity_video.py` | video identity: alpha is part of the picture, and the two fingerprint APIs sample the same way. ffmpeg's default `vp9` decoder drops the alpha layer in silence, so the decoder is chosen from the PROBED codec, never the extension. Real VP9/VP8 encodes |
| `test_lottie_repaint.py` | `validate_tgs` against a timeline that is not a number (NaN defeats every comparison), and `repaint_in_place` against the two shapes it silently skipped: a keyframed colour and a gradient's opacity ramp |
| `test_catalog.py` | catalog dedup (exact + perceptual), `file_unique_id` skip, pending/upload tracking, persistence |
| `test_catalog_order.py` | the manual publish order (the `position` column) shared by the panel's drag-and-drop and `build_collection`'s publish order |
| `test_identity_repair.py` | `scripts/identity_repair.py`: moving every catalog row that stores a content key when the decoder changes what that key IS. The properties under test are the refusals — a collision stops the migration rather than merging two rows, an undecodable file stops it rather than half-converting, and `--apply` is required before anything is written |
| `test_resume_safety.py` | the duplicate-upload paths, driven through `build_pack.main()`: recorded-cursor resume, write-ahead in-flight record, atomic state writes, refusal to guess on unexplained drift, per-set limits |
| `test_unresolved_mutation.py` | the same engine when the live state is UNKNOWN: an ambiguous create, an unresolved in-flight record, a recorded set that no longer reads back. The guarantee is that the run stops rather than guess |
| `test_telegram_client.py` | `telegram_api.Telegram` on its own: token redaction, the STICKERSET_INVALID retry scope, and what the client accepts as evidence that an upload landed |
| `test_pack_locks.py` | `packstate.exclusive_lock` mechanics: refusal, release on error, stale reclaim and its races, ownership, heartbeat, and the lock-path helpers. (`test_lock_order.py` checks the documented ORDER of the same locks, by AST.) |
| `test_pack_locks_exclusion.py` | that two real processes never hold one pack-family lock at once. The old recovery path could seat two publishers: it decided ownership by reading a file, comparing a token and unlinking, and an unlinked inode is a lock nobody else can see. Two genuine subprocesses, not threads |
| `test_rebuild_dedup_state.py` | the `coins/rebuild_dedup.py` mutation walk: plan → validate → delete the old packs → upload, plus its in-flight reconcile and run lock |
| `test_rebuild_dedup_map.py` | the second phase of the same module: `map_and_fill` resolving `ticker_to_id.json` by image identity under the map lock, and the shared-logo-group guard |
| `test_publish_dedup.py` | verified retries for non-idempotent Bot API calls, live-set reconcile, adopt-on-occupied, recorded fuids |
| `test_build_collection_state.py` | `build_collection`'s state machine: plan/state files failing closed, live-set drift, identity on a recorded position and the unattributed tail |
| `test_publish_cli.py` | the same publisher at its CLI boundary: argument validation, `--new-set`/`--into-pack`, and `main()` end to end. Both halves drive `_CatalogFixture` |
| `test_publish_contracts.py` | the publisher contracts that are NOT collection state: pack titles as one sequence, the mixed-family layout, the blank-video guard, and the announcement path all three publishers share |
| `test_publish_invariants.py` | two invariants the publisher states but only enforced in one place each: a set that closed mid-run is never appended to on the next item, and the blank-media check gets the ITEM's format rather than the family's, so `--mixed` cannot bypass it |
| `test_preflight_outcomes.py` | preflight may not report acceptance it never obtained. The old counter counted ATTEMPTS, so a run in which every `check_uploadable` failed at the transport reported "all accepted" |
| `test_reconcile_identity.py` | recovery must never give a stranger's picture our item's identity. dHash is a grayscale STRUCTURE hash — an opaque red square and an opaque blue one are zero apart — so a perceptual match may only NOMINATE; content verification decides, and "more than one" and "could not examine" stay distinct from "no match" |
| `test_brand_logo.py` | the mandatory logo-first behaviour in `build_collection`: its conversion per format, that the real shipped asset is used, and that the coin bot stays exempt |
| `test_pack_manifest.py` | the `packs/` roster: what it records, and when it admits to being stale |
| `test_pack_archive.py` | when a FULL pack earns an archive move, and when the archive has stopped being true |
| `test_sync_order.py` | reordering an already-published pack with `setStickerPositionInSet`, which moves a sticker without re-uploading it, so every `custom_emoji_id` survives |
| `test_entry_point_contracts.py` | exit codes and argument validation at the CLI boundary of `fetch_pack`, `make_emoji_pngs`, `panel` and `logsetup` |
| `test_entry_points.py` | every executable imports cleanly in its own interpreter, and the suite's hermetic guard was installed before the test modules were imported |
| `test_fetch_emoji_ids.py` | premium-id extraction and de-duplication in the collector |
| `test_fetch_pack_limit.py` | `fetch_pack --limit N` delivers N NEW items — counting already-known stickers against the cap made a re-run a no-op |
| `test_make_emoji_pngs.py` | the image converter: blank guards, output freshness, source priority |
| `test_coin_cli_args.py` | the coin tools refuse an unrecognised argument instead of falling through to the live branch — a typo must not publish |
| `test_coin_logo_cache.py` | `fetch_logos` resume: a cached file is re-validated before it is trusted as a logo |
| `test_coin_http.py` | the one pooled `coins/_http.py` client: retry ladder, `Retry-After`, and the paging delay |
| `test_coin_providers.py` | the coin providers publishing logos: blank-logo refusal, the verified publish, and the canonical map re-read under the lock |
| `test_coin_recovery.py` | `fetch_paprika`'s unverified-upload recovery: an add that MAY be live is reconciled against the live set, never silently re-sent |
| `test_coin_ticker_map.py` | every writer of `ticker_to_id.json` — alias/enhance/provider — serialised so none loses another's update, and one inventory implementation |
| `test_remap_ids.py` | the coin remap / pack audit tools, both of which used to trust their download cache blindly |
| `test_verify_logos.py` | `verify_logos`: the inversion-aware distance, the durable replacement intent bound to its own `--map`, and the fix path's exit codes |
| `test_panel.py` | what `panel_view.build_view` makes of the catalog, and what `panel.py` serves of it: brand-logo preview, inert item JSON (no script breakout), the similarity order, the published-item filter, the pack index travelling onto an already-live card so the grid can draw the boundary between two packs, and the save-during-reorder window |
| `test_panel_guard.py` | the POST guard (token, loopback Host/Origin, content type, body cap, exact-permutation order) and the two behaviours built on it. `MutationGuard` owns nine `test_*` methods and is subclassed twice, so all three classes must stay in one module — importing the base elsewhere would collect it again rather than move it |
| `test_panel_page.py` | assertions against what the panel serves — the page (`panel.PAGE`) and its two scripts concatenated (`panel.SCRIPT`): drag-and-drop (every dragover edits the model and re-projects the grid, the drop records the dragstart snapshot instead of computing an index, a cancel restores it, the pointer's side of a tile decides before-or-after, a carried card is parked rather than unmounted), undo/redo, the virtual grid (only rows near the viewport exist, integer row arithmetic, an unchanged window costs a binary search), zoom (buttons, Ctrl+wheel, clamping, the top item anchored, compact below 75 %), the viewport observers (playback, and video sources attached only near the viewport), the scroll-time animation freeze, selection mode, the pack separators and the jump buttons. Those live in the page's own JavaScript, so the served text is the only level at which the behaviour exists |
| `test_panel_assets.py` | the scripts ship as real files, the page loads both in order with a version that is their content hash (`panel.ASSET_VER`), the inline block carries values not behaviour, and `/static/<script>?v=…` reaches the file on disk (immutable-cached, so a stale script would otherwise be served forever) |
| `test_panel_save_scope.py` | `POST /api/save` carries the FULL selection, so a request with no notion of scope speaks for rows the page never saw. The request now states what it was showing, the server applies the decision only inside that scope, and a page too old to say is refused with 409. Driven through the real handler on a real socket |
| `test_panel_browser.py` | what the panel's client code actually DOES, in headless Chromium: the save pipeline's revisioned queues, the separator nodes, the zoom anchor, the animation freeze, the pack count, and a denied `localStorage`. A source-text assertion cannot tell a correct implementation of any of these from a broken one. Needs `requirements-dev.txt` and `python -m playwright install chromium`; set `EMOJI_MAPPER_NO_BROWSER_TESTS=1` to opt out deliberately, never to make a missing browser look green |
| `test_panel_server.py` | the panel as a process: which Host may reach it, and who owns the port. Real subprocesses and real sockets, so the slowest of the four |
| `test_repaintable.py` | the `--repaintable` gate: the flag is read off the STICKER not the set, only an explicit yes proceeds, `skip`/`keep` never prompt, and an unanswerable prompt (EOF, Ctrl-C) is a no rather than a crash. Also `--tint`, which bakes the repaint instead of skipping: colour parsing, a Lottie recoloured through its tree (fills, strokes and gradient stops, offsets kept), a static filled through its alpha, video refused, and the tint recorded on the item |
| `test_emoji_bot.py` | the bot's pure helpers: entity extraction and reply building |
| `test_logsetup.py` | secret redaction, plus a guard that fails if any `.env` secret value appears in a git-tracked file |

`_pack_fixtures.py`, `_rebuild_fixtures.py`, `_cli_fixtures.py`,
`_bc_fixtures.py`, `_panel_fixtures.py`, `_media_fixtures.py` and
`_coin_fixtures.py` hold the
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
npx vitest run       # the Worker's own suite
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
