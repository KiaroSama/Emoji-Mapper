# Watchdog driver for the crypto-coin SVG->PNG conversion.
#
# The converter writes the name of the file it is working on into a marker file
# and clears it afterwards, so the marker is a per-file heartbeat. The watchdog
# gives every single file PERFILE seconds; it no longer watches the output-file
# count, which killed a legitimately slow (or blank-result) source just because
# no new PNG had appeared for 30 seconds.
#
# A killed file is quarantined by the next converter run and reported here for
# REVIEW. Exits non-zero when the conversion never completed, when a file was
# quarantined, or when the converter itself reports failures -- never pretend a
# dead run succeeded.
#
# This script lives in coins/. The shared converter (make_emoji_pngs.py) and the
# virtual environment (.venv) are at the PROJECT ROOT, one level up. Coin images
# live in coins/logos/{svg,png} and are written to coins/logos/emoji.

$ErrorActionPreference = 'SilentlyContinue'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptRoot
Set-Location -LiteralPath $ScriptRoot

$py       = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$convert  = Join-Path $ProjectRoot 'make_emoji_pngs.py'
$svgDir   = Join-Path $ScriptRoot 'logos\svg'
$pngDir   = Join-Path $ScriptRoot 'logos\png'
$emojiDir = Join-Path $ScriptRoot 'logos\emoji'
$marker   = Join-Path $emojiDir '.svg_cur'
$PERFILE  = 120   # seconds one source may take before we call it stuck
$POLL     = 2

# Marker state = "<mtime ticks>|<name>". Any change means real progress.
function Get-MarkerState ($path) {
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    $item = Get-Item -LiteralPath $path
    return "$($item.LastWriteTimeUtc.Ticks)|$((Get-Content -LiteralPath $path -Raw))"
}

$exit = 0
$quarantined = @()
$finished = $false

# Pass 1: SVGs (hang-prone) under the watchdog. Start-Process joins -ArgumentList
# with plain spaces, so every path must carry its own quotes -- this project
# lives under "G:\Program Files\...".
$svgArgs = @("`"$convert`"", '--in', "`"$svgDir`"", '--out', "`"$emojiDir`"")
for ($iter = 1; $iter -le 100; $iter++) {
    $proc = Start-Process -FilePath $py -ArgumentList $svgArgs `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput 'emoji_conv.txt' -RedirectStandardError 'emoji_err.txt'
    $state = Get-MarkerState $marker
    $since = Get-Date
    $killed = $false
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds $POLL
        $now = Get-MarkerState $marker
        if ($now -ne $state) { $state = $now; $since = Get-Date; continue }
        if (((Get-Date) - $since).TotalSeconds -lt $PERFILE) { continue }
        $stuck = (Get-Content -LiteralPath $marker -Raw).Trim()
        if (-not $stuck) { $stuck = '(unknown)' }
        Stop-Process -Id $proc.Id -Force
        $quarantined += $stuck
        $killed = $true
        Write-Host "[watchdog] '$stuck' made no progress for ${PERFILE}s (iter $iter) - killed."
        break
    }
    [void]$proc.WaitForExit(10000)   # let the redirected output flush before reading
    if ($killed) { continue }        # the culprit is quarantined; retry the rest

    # Only a kill justifies a restart. A converter that exited on its own without
    # printing DONE hit a real error, and rerunning it 99 more times just hides it.
    $tail = Get-Content 'emoji_conv.txt' -Tail 1
    if ($tail -match '^DONE:') {
        Write-Host "[watchdog] SVG conversion complete: $tail"
        if ($proc.ExitCode -gt $exit) { $exit = $proc.ExitCode }
        $finished = $true
    } else {
        Write-Host "[watchdog] converter exited (code $($proc.ExitCode)) without finishing - see emoji_err.txt"
        $exit = if ($proc.ExitCode -gt 1) { $proc.ExitCode } else { 1 }
    }
    break
}

# Surface the converter's own quarantine notice (entries recorded by an earlier
# run) plus anything this run killed: a skipped source must never be dropped
# silently, and the run is not "clean" while one is waiting for review.
$review = Get-Content 'emoji_conv.txt' | Select-String -Pattern '^(REVIEW|QUARANTINE):'
foreach ($line in $review) { Write-Host "[watchdog] $($line.Line)" }
if ($quarantined.Count) {
    Write-Host "[watchdog] REVIEW killed sources: $($quarantined -join ', ') - listed in $(Join-Path $emojiDir '.svg_skip.txt'); delete a line to retry one."
}
if (($review -or $quarantined.Count) -and $exit -lt 3) { $exit = 3 }

if (-not $finished) {
    if ($exit -eq 0) { $exit = 1 }
    Write-Host "[watchdog] SVG conversion did NOT complete - giving up (exit $exit)."
    exit $exit
}

# Pass 2: raster PNGs (fast, no watchdog needed). Also the fallback for SVGs
# that produced nothing, so it runs even when pass 1 reported failures.
if (Test-Path -LiteralPath $pngDir) {
    & $py $convert --in $pngDir --out $emojiDir
    if ($LASTEXITCODE -gt $exit) { $exit = $LASTEXITCODE }
}

exit $exit
