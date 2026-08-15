# Tests

Run from the repository root:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Tests use Python's stdlib `unittest` (no extra dependencies). Video tests are
skipped automatically when `ffmpeg`/`ffprobe` are not on `PATH`.

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
