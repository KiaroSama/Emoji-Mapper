# Watchdog driver for the crypto-coin SVG->PNG conversion.
# Restarts the converter whenever it hangs on a pathological SVG (no progress
# for STALL seconds). The converter records the in-progress name in a marker
# file, so the next run blacklists it and continues. Stops when DONE.
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
$STALL = 30

# Pass 1: SVGs (hang-prone) under the watchdog.
for ($iter = 1; $iter -le 100; $iter++) {
    $proc = Start-Process -FilePath $py `
        -ArgumentList @($convert, '--in', $svgDir, '--out', $emojiDir) `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput 'emoji_conv.txt' -RedirectStandardError 'emoji_err.txt'
    $last = (Get-ChildItem $emojiDir -Filter *.png).Count
    $stall = 0
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds 5
        $now = (Get-ChildItem $emojiDir -Filter *.png).Count
        if ($now -gt $last) { $last = $now; $stall = 0 } else { $stall += 5 }
        if ($stall -ge $STALL) {
            Stop-Process -Id $proc.Id -Force
            Write-Host "[watchdog] killed hung converter (iter $iter), restarting..."
            break
        }
    }
    Start-Sleep -Seconds 2
    $tail = Get-Content 'emoji_conv.txt' -Tail 1
    if ($tail -match '^DONE:') {
        Write-Host "[watchdog] SVG conversion complete: $tail"
        break
    }
}

# Pass 2: raster PNGs (fast, no watchdog needed).
if (Test-Path $pngDir) {
    & $py $convert --in $pngDir --out $emojiDir
}
