# Self-restarting runner for the deduplicated pack rebuild.
# Repeatedly runs the resumable, duplicate-proof 'build' phase until it reports
# completion, then maps cids + fills inventory and sends final links.
# Safe to interrupt and re-run: build reconciles progress from live Telegram counts.
#
# 'build' exit codes: 0 = whole plan walked, 3 = more work left (resume me).
# Anything else is terminal (bad usage, missing token, unreadable state) and
# retrying it only burns Telegram API calls, so the loop stops instead.

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
# .venv is at the project root, one level up from coins/.
$py = Join-Path $root "..\.venv\Scripts\python.exe"
$log = Join-Path $root "rebuild_dedup_out.txt"
$statePath = Join-Path $root "rebuild_dedup_state.json"

function Write-Note ($text) { $text | Out-File -FilePath $log -Append -Encoding utf8 }

# Plan position recorded by build. Used to tell "resuming" from "stuck":
# a checkpoint that did not advance the cursor did no work at all.
function Get-Cursor {
    try { [int]((Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json).cursor) }
    catch { -1 }
}

$maxLoops = 200
$loop = 0
$stalled = 0
$lastCursor = Get-Cursor
$code = 3

while ($true) {
    $loop++
    Write-Note "=== build attempt $loop @ $(Get-Date -Format o) ==="
    & $py (Join-Path $root "rebuild_dedup.py") build *>> $log
    $code = $LASTEXITCODE
    Write-Note "=== build attempt $loop exited code=$code ==="

    if ($code -eq 0) { break }
    if ($code -ne 3) { Write-Note "=== terminal error (exit $code); not retrying ==="; break }

    $cursor = Get-Cursor
    if ($cursor -gt $lastCursor) { $stalled = 0 } else { $stalled++ }
    $lastCursor = $cursor
    if ($stalled -ge 2) {
        Write-Note "=== no progress at plan position $cursor for $stalled checkpoints; stopping ==="
        break
    }
    if ($loop -ge $maxLoops) { Write-Note "=== gave up after $loop attempts ==="; break }
    Start-Sleep -Seconds 15   # backoff between resume attempts, not a readiness wait
}

if ($code -ne 0) {
    Write-Note "=== BUILD INCOMPLETE (last exit $code after $loop attempts); map + links skipped ==="
    exit $code
}

Write-Note "=== build complete; mapping + filling inventory ==="
& $py (Join-Path $root "rebuild_dedup.py") map *>> $log
$mapCode = $LASTEXITCODE
if ($mapCode -ne 0) {
    Write-Note "=== map failed (exit $mapCode); links skipped ==="
    exit $mapCode
}

Write-Note "=== sending final combined links ==="
& $py (Join-Path $root "rebuild_dedup.py") links *>> $log
$linksCode = $LASTEXITCODE
if ($linksCode -ne 0) {
    Write-Note "=== links failed (exit $linksCode) ==="
    exit $linksCode
}

Write-Note "=== ALL DONE ==="
