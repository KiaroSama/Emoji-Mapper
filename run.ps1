<#
.SYNOPSIS
    Emoji Mapper launcher: prepares the Python environment and offers a colored,
    sectioned menu to build/collect/publish Telegram custom-emoji packs and run
    the premium-emoji bot.

.DESCRIPTION
    - Resolves paths relative to this file, works from any current directory.
    - Prefers Windows Terminal + PowerShell 7 (pwsh); falls back gracefully.
    - Creates/reuses .venv and offers to install requirements (Enter = Yes).
    - Reads tokens from .env (never printed).
    - Menu is grouped into sections; each section has its own numbering with a
      one-letter prefix (B = Build, C = Collection, R = Run bot), e.g. B1, C3.
    - Writes a per-run UTC log under logs\run_<UTC>.log (values never logged).

.PARAMETER NoRelaunch
    Do not relaunch in Windows Terminal / PowerShell 7.

.PARAMETER Check
    Non-interactive environment "doctor" for CI / scripted use.
#>

[CmdletBinding()]
param(
    [switch]$NoRelaunch,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition

# --- Logging --------------------------------------------------------------
# One UTC-named log file per execution, e.g. logs\run_2026-07-03_14-32-08_UTC.log
$script:LogFile = $null

function Initialize-Log {
    try {
        $logDir = Join-Path $ScriptRoot 'logs'
        if (-not (Test-Path -LiteralPath $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
        $ts = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss')
        $path = Join-Path $logDir "run_${ts}_UTC.log"
        $n = 1
        while (Test-Path -LiteralPath $path) {
            $path = Join-Path $logDir "run_${ts}_UTC_$n.log"; $n++
        }
        $script:LogFile = $path
        Write-Log 'INFO' "launcher started (PowerShell $($PSVersionTable.PSVersion), $([Environment]::OSVersion.VersionString))"
        Write-Log 'INFO' "project root: $ScriptRoot"
    } catch {
        # Logging must never take down the launcher; fall back to console only.
        $script:LogFile = $null
        Write-Host "[!] Could not initialize log file: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Write-Log ($level, $msg) {
    if (-not $script:LogFile) { return }
    $ts = [DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss')
    try { Add-Content -LiteralPath $script:LogFile -Value "[$ts UTC] [$level] $msg" -Encoding utf8 } catch { }
}

# --- Consistent color + log helpers ---------------------------------------
function Write-Title  ($m) { Write-Host ""; Write-Host " $m " -ForegroundColor Black -BackgroundColor Cyan; Write-Log 'INFO' "== $m ==" }
function Write-Info   ($m) { Write-Host "[*] $m" -ForegroundColor Cyan;    Write-Log 'INFO' $m }
function Write-Ok     ($m) { Write-Host "[OK] $m" -ForegroundColor Green;  Write-Log 'INFO' "OK: $m" }
function Write-Warn   ($m) { Write-Host "[!] $m" -ForegroundColor Yellow;  Write-Log 'WARNING' $m }
function Write-Err    ($m) { Write-Host "[X] $m" -ForegroundColor Red;     Write-Log 'ERROR' $m }
function Write-Step   ($m) { Write-Host "==> $m" -ForegroundColor Magenta; Write-Log 'INFO' "step: $m" }

# --- Prefer Windows Terminal + PowerShell 7 (single relaunch, loop-safe) ---
if (-not $NoRelaunch -and -not $env:EMOJI_MAPPER_RELAUNCHED) {
    $pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwsh -and $PSVersionTable.PSVersion.Major -lt 7) {
        $env:EMOJI_MAPPER_RELAUNCHED = '1'
        $wt = Get-Command wt.exe -ErrorAction SilentlyContinue
        $self = $MyInvocation.MyCommand.Definition
        try {
            if ($wt) {
                & $wt.Source $pwsh.Source -NoExit -File $self -NoRelaunch
            } else {
                & $pwsh.Source -NoExit -File $self -NoRelaunch
            }
            return
        } catch {
            Write-Host "[!] Could not relaunch in PowerShell 7; continuing here." -ForegroundColor Yellow
        }
    }
}

Set-Location -LiteralPath $ScriptRoot

# --- Locate Python --------------------------------------------------------
function Get-PythonExe {
    $venv = Join-Path $ScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    return $null
}

function Find-BasePython {
    foreach ($c in @(
        @{ Exe = 'py';     Args = @('-3.11') },
        @{ Exe = 'py';     Args = @('-3') },
        @{ Exe = 'python'; Args = @() }
    )) {
        $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if ($cmd) {
            try {
                & $cmd.Source @($c.Args + '--version') *> $null
                if ($LASTEXITCODE -eq 0) { return $c }
            } catch { }
        }
    }
    return $null
}

function Confirm-YesDefault ($prompt) {
    $ans = Read-Host "$prompt [Y/n]"
    $yes = ([string]::IsNullOrWhiteSpace($ans) -or $ans -match '^(y|yes)$')
    Write-Log 'INFO' "prompt '$prompt' -> '$ans' (yes=$yes)"
    return $yes
}

function Ensure-Environment {
    $py = Get-PythonExe
    if (-not $py) {
        Write-Warn ".venv not found."
        $base = Find-BasePython
        if (-not $base) {
            Write-Err "No Python found. Install Python 3.11+ (winget install Python.Python.3.12)."
            return $null
        }
        if (-not (Confirm-YesDefault "Create .venv and install dependencies now?")) {
            Write-Err "Cannot continue without a virtual environment."
            return $null
        }
        Write-Step "Creating .venv ..."
        & (Get-Command $base.Exe).Source @($base.Args + @('-m','venv','.venv'))
        $py = Get-PythonExe
        if (-not $py) { Write-Err "Failed to create .venv."; return $null }
    }
    return $py
}

function Install-Deps ($py) {
    Write-Step "Installing requirements ..."
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $ScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -eq 0) { Write-Ok "Dependencies installed." }
    else { Write-Err "Dependency install failed (exit $LASTEXITCODE)." }
}

function Test-Deps ($py) {
    & $py -c "import requests,PIL,numpy,svglib,reportlab" *> $null
    return ($LASTEXITCODE -eq 0)
}

function Check-Env ($py) {
    if (-not (Test-Path -LiteralPath (Join-Path $ScriptRoot '.env'))) {
        Write-Warn ".env not found. Copy .env.example to .env and fill in tokens."
        return
    }
    Write-Ok ".env present."
}

function Test-Ffmpeg {
    return ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and
            (Get-Command ffprobe -ErrorAction SilentlyContinue))
}

# --- Actions --------------------------------------------------------------
function Action-BuildGeneral ($py) {
    Write-Title "Build a general emoji pack (@YourEmojiBot)"
    $inDir = Read-Host "Source image folder (e.g. input\myset)"
    if ([string]::IsNullOrWhiteSpace($inDir) -or -not (Test-Path -LiteralPath $inDir)) {
        Write-Err "Folder not found: $inDir"; return
    }
    $base  = Read-Host "Pack base name (letters/digits/_), e.g. myset"
    $title = Read-Host "Pack title, e.g. My Emojis"
    $emoji = Read-Host "Associated standard emoji (default 😀)"
    if ([string]::IsNullOrWhiteSpace($emoji)) { $emoji = '😀' }
    $build = Join-Path 'build' $base

    Write-Step "Converting images -> $build ..."
    & $py make_emoji_pngs.py --in $inDir --out $build
    if ($LASTEXITCODE -ne 0) { Write-Err "Conversion failed."; return }

    Write-Step "Dry-run preview ..."
    & $py build_pack.py --base $base --title $title --source-dir $build `
        --token-env GENERAL_BOT_TOKEN --emoji $emoji --dry-run
    if ($LASTEXITCODE -ne 0) { Write-Err "Dry-run failed (check .env / source)."; return }

    if (Confirm-YesDefault "Upload to Telegram now?") {
        & $py build_pack.py --base $base --title $title --source-dir $build `
            --token-env GENERAL_BOT_TOKEN --emoji $emoji
        if ($LASTEXITCODE -eq 0) { Write-Ok "Pack build finished." }
        else { Write-Err "Build failed (exit $LASTEXITCODE)." }
    } else {
        Write-Info "Skipped upload. Re-run when ready."
    }
}

function Action-ConvertOnly ($py) {
    Write-Title "Convert images to 100x100 PNGs"
    $inDir = Read-Host "Source image folder"
    if (-not (Test-Path -LiteralPath $inDir)) { Write-Err "Folder not found."; return }
    $outDir = Read-Host "Output folder (blank = <folder>_emoji)"
    if ([string]::IsNullOrWhiteSpace($outDir)) { & $py make_emoji_pngs.py --in $inDir }
    else { & $py make_emoji_pngs.py --in $inDir --out $outDir }
}

function Action-CoinRebuild ($py) {
    Write-Title "Crypto-coin pack rebuild (TELEGRAM_BOT_TOKEN)"
    Write-Warn "This uses the crypto-coin bot and the coins/ component."
    $script = Join-Path $ScriptRoot 'coins\rebuild_packs.py'
    if (-not (Test-Path -LiteralPath $script)) { Write-Err "coins\rebuild_packs.py not found."; return }
    if (Confirm-YesDefault "Run coins/rebuild_packs.py now?") {
        & $py $script
    }
}

function Action-CollectPacks ($py) {
    Write-Title "Collect emoji from existing Telegram packs"
    Write-Info "Paste pack links/names (t.me/addemoji/...). Blank line to finish."
    $packs = @()
    while ($true) {
        $line = Read-Host "Pack"
        if ([string]::IsNullOrWhiteSpace($line)) { break }
        $packs += $line.Trim()
    }
    if ($packs.Count -eq 0) { Write-Warn "No packs entered."; return }
    $tokenEnv = Read-Host "Token env var (default GENERAL_BOT_TOKEN)"
    if ([string]::IsNullOrWhiteSpace($tokenEnv)) { $tokenEnv = 'GENERAL_BOT_TOKEN' }
    & $py fetch_pack.py @packs --token-env $tokenEnv
}

function Action-AddMedia ($py) {
    Write-Title "Build emoji from scratch (folder of images/animations/videos)"
    if (-not (Test-Ffmpeg)) {
        Write-Warn "ffmpeg/ffprobe not found: video emoji (.webm) will fail."
        Write-Warn "Install with: winget install Gyan.FFmpeg"
    }
    $inDir = Read-Host "Source folder"
    if (-not (Test-Path -LiteralPath $inDir)) { Write-Err "Folder not found."; return }
    $emoji = Read-Host "Associated standard emoji (default 😀)"
    if ([string]::IsNullOrWhiteSpace($emoji)) { $emoji = '😀' }
    & $py add_media.py --in $inDir --emoji $emoji
}

function Action-PublishCollection ($py) {
    Write-Title "Publish the collection into new packs (multi-format)"
    $base  = Read-Host "Pack base name (letters/digits only), e.g. mypack"
    $title = Read-Host "Pack title, e.g. My Collection"
    $tokenEnv = Read-Host "Token env var (default GENERAL_BOT_TOKEN)"
    if ([string]::IsNullOrWhiteSpace($tokenEnv)) { $tokenEnv = 'GENERAL_BOT_TOKEN' }
    Write-Step "Dry-run preview ..."
    & $py build_collection.py --base $base --title $title --token-env $tokenEnv --dry-run
    if ($LASTEXITCODE -ne 0) { Write-Err "Dry-run failed (run a collect/add step first?)."; return }
    if (Confirm-YesDefault "Upload to Telegram now?") {
        & $py build_collection.py --base $base --title $title --token-env $tokenEnv
        if ($LASTEXITCODE -eq 0) { Write-Ok "Collection published." }
        else { Write-Err "Publish failed (exit $LASTEXITCODE)." }
    } else {
        Write-Info "Skipped upload. Re-run when ready (resumable)."
    }
}

function Action-Panel ($py) {
    Write-Title "Curate panel (pick which emoji go into the pack)"
    Write-Info "Opens a dark neon web panel; tick/untick emoji, then Save. Ctrl+C to stop."
    & $py panel.py
}

function Action-RunBot ($py) {
    Write-Title "Run the Emoji Mapper bot (premium-emoji ID extractor)"
    Write-Info "Send the bot a premium emoji or a post with emoji, or add it to a channel/group."
    Write-Info "Press Ctrl+C to stop the bot."
    & $py emoji_bot.py
}

# --- Menu -----------------------------------------------------------------
# Each row: Key, Text, Action. Grouped by section; per-section numbering with a
# one-letter section prefix so keys stay unique (B/C/R).
function Show-Menu {
    Write-Title "Menu"

    Write-Host "  Build a single pack" -ForegroundColor Yellow
    Write-Host "   B1" -ForegroundColor Yellow -NoNewline; Write-Host ") Build a general emoji pack  (new bot)"      -ForegroundColor White
    Write-Host "   B2" -ForegroundColor Yellow -NoNewline; Write-Host ") Convert images to 100x100 PNGs only"        -ForegroundColor White
    Write-Host "   B3" -ForegroundColor Yellow -NoNewline; Write-Host ") Crypto-coin pack rebuild    (coin bot)"     -ForegroundColor White

    Write-Host "  Collection (multi-format, duplicate-proof)" -ForegroundColor Green
    Write-Host "   C1" -ForegroundColor Green -NoNewline; Write-Host ") Collect emoji from existing packs (download)" -ForegroundColor White
    Write-Host "   C2" -ForegroundColor Green -NoNewline; Write-Host ") Add media from a folder (build from scratch)" -ForegroundColor White
    Write-Host "   C3" -ForegroundColor Green -NoNewline; Write-Host ") Publish the collection into new packs"        -ForegroundColor White
    Write-Host "   C4" -ForegroundColor Green -NoNewline; Write-Host ") Curate panel - pick which emoji to include (web)" -ForegroundColor White

    Write-Host "  Bot" -ForegroundColor Magenta
    Write-Host "   R1" -ForegroundColor Magenta -NoNewline; Write-Host ") Run the Emoji Mapper bot (premium-emoji ID extractor)" -ForegroundColor White

    Write-Host "   q"  -ForegroundColor DarkGray -NoNewline; Write-Host ") Quit" -ForegroundColor DarkGray
}

function Invoke-Choice ($choice, $py) {
    switch ($choice.ToLower().Trim()) {
        'b1' { Action-BuildGeneral $py }
        'b2' { Action-ConvertOnly $py }
        'b3' { Action-CoinRebuild $py }
        'c1' { Action-CollectPacks $py }
        'c2' { Action-AddMedia $py }
        'c3' { Action-PublishCollection $py }
        'c4' { Action-Panel $py }
        'r1' { Action-RunBot $py }
        { $_ -in @('q','quit','exit') } { return $false }
        default { Write-Warn "Unknown option: $choice  (use e.g. B1, C3, R1, or q)" }
    }
    return $true
}

# --- Bootstrap ------------------------------------------------------------
Initialize-Log
Write-Title "Emoji Mapper"
$py = Ensure-Environment
if (-not $py) { Write-Log 'CRITICAL' 'no Python environment; exiting'; exit 1 }
if (-not (Test-Deps $py)) {
    if (Confirm-YesDefault "Install/repair Python dependencies?") { Install-Deps $py }
}
Check-Env $py
$pyVer = (& $py --version)
Write-Ok ("Python: " + $pyVer)
if (Test-Ffmpeg) { Write-Ok "ffmpeg present (video emoji enabled)." }
else { Write-Warn "ffmpeg not found: video emoji disabled (winget install Gyan.FFmpeg)." }

# Non-interactive environment check ("doctor") for CI / scripted use.
if ($Check) {
    if (Test-Deps $py) { Write-Ok "All Python dependencies import correctly." }
    else { Write-Err "Some dependencies are missing."; Write-Log 'INFO' 'doctor: exit 1'; exit 1 }
    Write-Ok "Environment check complete."
    Write-Log 'INFO' 'doctor: exit 0'
    exit 0
}

# --- Menu loop ------------------------------------------------------------
$running = $true
while ($running) {
    Show-Menu
    $choice = Read-Host "Select"
    Write-Log 'INFO' "menu selection: '$choice'"
    try {
        $running = Invoke-Choice $choice $py
    } catch {
        Write-Err "Action failed: $($_.Exception.Message)"
        Write-Log 'ERROR' ("exception: " + ($_ | Out-String).Trim())
    }
}
Write-Info "Bye."
Write-Log 'INFO' 'launcher shutdown (normal)'
