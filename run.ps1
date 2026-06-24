<#
.SYNOPSIS
    Emoji Mapper launcher: prepares the Python environment and offers a menu to
    build Telegram custom-emoji packs (general or crypto-coin workflows).

.DESCRIPTION
    - Resolves paths relative to this file, works from any current directory.
    - Prefers Windows Terminal + PowerShell 7 (pwsh); falls back gracefully.
    - Creates/reuses .venv and offers to install requirements (Enter = Yes).
    - Reads tokens from .env (never printed).
#>

[CmdletBinding()]
param(
    [switch]$NoRelaunch,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition

# --- Consistent color helpers ---------------------------------------------
function Write-Title  ($m) { Write-Host ""; Write-Host " $m " -ForegroundColor Black -BackgroundColor Cyan }
function Write-Info   ($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok     ($m) { Write-Host "[OK] $m" -ForegroundColor Green }
function Write-Warn   ($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Err    ($m) { Write-Host "[X] $m" -ForegroundColor Red }
function Write-Step   ($m) { Write-Host "==> $m" -ForegroundColor Magenta }

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
            Write-Warn "Could not relaunch in PowerShell 7; continuing here."
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
    return ([string]::IsNullOrWhiteSpace($ans) -or $ans -match '^(y|yes)$')
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

# --- Actions --------------------------------------------------------------
function Action-BuildGeneral ($py) {
    Write-Title "Build a general emoji pack (@GodVerifyEmojiMapperbot)"
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

function Test-Ffmpeg {
    return ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and
            (Get-Command ffprobe -ErrorAction SilentlyContinue))
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

# --- Bootstrap ------------------------------------------------------------
Write-Title "Emoji Mapper"
$py = Ensure-Environment
if (-not $py) { exit 1 }
if (-not (Test-Deps $py)) {
    if (Confirm-YesDefault "Install/repair Python dependencies?") { Install-Deps $py }
}
Check-Env $py
Write-Ok ("Python: " + (& $py --version))
if (Test-Ffmpeg) { Write-Ok "ffmpeg present (video emoji enabled)." }
else { Write-Warn "ffmpeg not found: video emoji disabled (winget install Gyan.FFmpeg)." }

# Non-interactive environment check ("doctor") for CI / scripted use.
if ($Check) {
    if (Test-Deps $py) { Write-Ok "All Python dependencies import correctly." }
    else { Write-Err "Some dependencies are missing."; exit 1 }
    Write-Ok "Environment check complete."
    exit 0
}

# --- Menu -----------------------------------------------------------------
$running = $true
while ($running) {
    Write-Title "Menu"
    Write-Host "  Build a single pack" -ForegroundColor DarkCyan
    Write-Host "  1) Build a general emoji pack  (new bot)" -ForegroundColor White
    Write-Host "  2) Convert images to 100x100 PNGs only"   -ForegroundColor White
    Write-Host "  3) Crypto-coin pack rebuild     (coin bot)" -ForegroundColor White
    Write-Host "  Collection (multi-format, duplicate-proof)" -ForegroundColor DarkCyan
    Write-Host "  4) Collect emoji from existing packs (download)" -ForegroundColor White
    Write-Host "  5) Add media from a folder (build from scratch)" -ForegroundColor White
    Write-Host "  6) Publish the collection into new packs"        -ForegroundColor White
    Write-Host "  q) Quit" -ForegroundColor DarkGray
    $choice = Read-Host "Select"
    switch ($choice) {
        '1' { Action-BuildGeneral $py }
        '2' { Action-ConvertOnly $py }
        '3' { Action-CoinRebuild $py }
        '4' { Action-CollectPacks $py }
        '5' { Action-AddMedia $py }
        '6' { Action-PublishCollection $py }
        { $_ -in @('q','quit','0','exit') } { Write-Info "Bye."; $running = $false }
        default { Write-Warn "Unknown option: $choice" }
    }
}
