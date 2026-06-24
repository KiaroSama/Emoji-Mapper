# Self-restarting runner for the deduplicated pack rebuild.
# Repeatedly runs the resumable, duplicate-proof 'build' phase until it reports
# completion (exit 0), then maps cids + fills inventory and sends final links.
# Safe to interrupt and re-run: build reconciles progress from live Telegram counts.

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
# .venv is at the project root, one level up from coins/.
$py = Join-Path $root "..\.venv\Scripts\python.exe"
$log = Join-Path $root "rebuild_dedup_out.txt"

$maxLoops = 200
$loop = 0
do {
    $loop++
    "=== build attempt $loop @ $(Get-Date -Format o) ===" | Out-File -FilePath $log -Append -Encoding utf8
    & $py (Join-Path $root "rebuild_dedup.py") build *>> $log
    $code = $LASTEXITCODE
    "=== build attempt $loop exited code=$code ===" | Out-File -FilePath $log -Append -Encoding utf8
    if ($code -ne 0) { Start-Sleep -Seconds 15 }
} while ($code -ne 0 -and $loop -lt $maxLoops)

if ($code -eq 0) {
    "=== build complete; mapping + filling inventory ===" | Out-File -FilePath $log -Append -Encoding utf8
    & $py (Join-Path $root "rebuild_dedup.py") map *>> $log
    "=== sending final combined links ===" | Out-File -FilePath $log -Append -Encoding utf8
    & $py (Join-Path $root "rebuild_dedup.py") links *>> $log
    "=== ALL DONE ===" | Out-File -FilePath $log -Append -Encoding utf8
} else {
    "=== gave up after $loop attempts ===" | Out-File -FilePath $log -Append -Encoding utf8
}
