# Tests

Run from the repository root:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py"
```

**`-t .` is required.** Without it the tests directory becomes the top-level,
modules load as `test_x` instead of `tests.test_x`, and `tests/__init__.py`
never runs — which silently disables the guard described below.
`SuiteIsHermetic.test_the_guard_is_installed_at_all` fails loudly if the suite
is started without it.

Tests use Python's stdlib `unittest` (no extra dependencies). Video tests are
skipped automatically when `ffmpeg`/`ffprobe` are not on `PATH`.

## The suite never touches the real network

`tests/__init__.py` runs before any test module and scrubs every
token/API-key-shaped environment variable, points `TELEGRAM_API_BASE` at the
discard port, stops `build_pack.load_env()` from reading `.env`, and refuses
outbound sockets to anything but loopback (`NetworkAccessDenied`).

This exists because a test meant only to check a CLI usage error once reached
live Telegram and replaced a sticker in a published pack. Inject a fake session
rather than adding an opt-out.

## Layout

| File | Covers |
|------|--------|
| `test_media.py` | format detection, content/perceptual hashing, static→PNG, GIF→WEBM, Lottie→TGS, and the animated contract (512×512 canvas, frame rate, duration, gzip packaging, bounded decompression) |
| `test_catalog.py` | catalog dedup (exact + perceptual), `file_unique_id` skip, pending/upload tracking, persistence |
| `test_resume_safety.py` | the duplicate-upload paths: recorded-cursor resume, write-ahead in-flight record, atomic state writes, refusal to guess on unexplained drift, per-set limits, token redaction, and the shared-logo-group guard |
| `test_publish_dedup.py` | verified retries for non-idempotent Bot API calls, live-set reconcile, adopt-on-occupied, recorded fuids |
| `test_panel.py` | brand-logo preview, inert item JSON (no script breakout), and the mutation guard (token, loopback Host/Origin, content type, body cap, exact-permutation order) |
| `test_logsetup.py` | secret redaction, plus a guard that fails if any `.env` secret value appears in a git-tracked file |

## Fixtures

| Fixture | Purpose | Safe to commit |
|---------|---------|----------------|
| `fixtures/lottie/red_circle_512.json` | minimal valid 512×512 Lottie animation used to exercise TGS packaging/validation and animated content hashing. 512×512 is Telegram's required canvas for animated emoji — a 100×100 fixture would encode the wrong contract | yes (synthetic, no secrets) |

Static images and animated GIFs used by the tests are generated on the fly with
Pillow in temporary directories and are **not** committed. No network access or
real Telegram credentials are required to run the suite.
