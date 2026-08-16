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

# NOT a script-wide SilentlyContinue. That swallowed every failure below,
# including a Start-Process that never started: $proc stayed $null, and
# '-not $null.HasExited' is $true, so the watchdog waited out the full PERFILE
# deadline 100 times -- 3.3 hours of nothing, 100 '(unknown)' quarantine
# entries, then exit 3 ("sources need review") on a first run that simply had no
# .venv yet. Only the reads that legitimately race the converter are silenced,
# one call at a time.
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

# This script is run directly, not through run.ps1, so the venv it needs may
# simply not exist yet. Say so in one line instead of watchdogging a process
# that was never started.
foreach ($required in @($py, $convert)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        Write-Host "[watchdog] required file not found: $required"
        Write-Host "[watchdog] create the project venv and install requirements first (see README)."
        exit 2
    }
}

# Marker state = "<mtime ticks>|<name>". Any change means real progress.
# The converter clears and rewrites this file constantly, so both reads can
# legitimately land on a file that is being replaced.
function Get-MarkerState ($path) {
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    $item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
    if (-not $item) { return '' }
    $text = Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue
    return "$($item.LastWriteTimeUtc.Ticks)|$text"
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
    if (-not $proc) {
        # No process object means nothing is running, so every $proc member
        # below reads as $null and the watchdog would "supervise" a corpse.
        Write-Host "[watchdog] could not start '$py' - see emoji_err.txt."
        exit 2
    }
    $state = Get-MarkerState $marker
    $since = Get-Date
    $killed = $false
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds $POLL
        $now = Get-MarkerState $marker
        if ($now -ne $state) { $state = $now; $since = Get-Date; continue }
        if (((Get-Date) - $since).TotalSeconds -lt $PERFILE) { continue }
        # Races the converter clearing the marker; '' is a real answer here.
        $stuck = "$(Get-Content -LiteralPath $marker -Raw -ErrorAction SilentlyContinue)".Trim()
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
    # Still being flushed if WaitForExit timed out: absent is not an error here.
    $tail = Get-Content 'emoji_conv.txt' -Tail 1 -ErrorAction SilentlyContinue
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
$review = Get-Content 'emoji_conv.txt' -ErrorAction SilentlyContinue |
    Select-String -Pattern '^(REVIEW|QUARANTINE):'
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
