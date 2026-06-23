# Watchdog driver for make_emoji_pngs.py.
# Restarts the converter whenever it hangs on a pathological SVG (no progress
# for STALL_SECONDS). The converter records the in-progress ticker in a marker
# file, so the next run blacklists it and continues. Stops when DONE.

$ErrorActionPreference = 'SilentlyContinue'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location -LiteralPath $ScriptRoot
$py = Join-Path $ScriptRoot '.venv\Scripts\python.exe'
$emojiDir = Join-Path $ScriptRoot 'logos\emoji'
$STALL = 30

for ($iter = 1; $iter -le 100; $iter++) {
    $proc = Start-Process -FilePath $py -ArgumentList 'make_emoji_pngs.py' -PassThru -NoNewWindow `
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
        Write-Host "[watchdog] conversion complete: $tail"
        break
    }
}
