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
    Non-interactive environment "doctor" for CI / scripted use. Never prompts and
    never installs anything; exits 0 when the environment is usable, else 1.
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

# --- ANSI 256/truecolor palette (rendered in Windows Terminal / PowerShell 7) -
$script:E = [char]27
function Paint ($code, $text) { "$($script:E)[${code}m$text$($script:E)[0m" }

# Palette (exact FFmWiz colors where applicable).
$script:CTitle   = '1;38;2;255;50;115'   # bold pink-red banner title + rule (WIZARD_TITLE)
$script:CLogNote = '38;5;227'            # NOTE_YELLOW "Logging to:" line
$script:CHeading = '38;5;123'            # cyan action/screen titles (like the sample)
$script:CBuild   = '38;5;222'            # Build section header (amber)
$script:CColl    = '38;5;123'            # Collection section header (cyan)
$script:CBot     = '38;5;219'            # Bot section header (pink/magenta)
$script:CKeyA    = '38;5;154'            # Build keys (chartreuse)
$script:CKeyB    = '38;5;87'             # Collection keys (aqua)
$script:CKeyC    = '38;5;209'            # Bot keys (coral)
$script:CText    = '38;5;252'            # menu item text (near-white)
$script:CDim     = '38;5;244'            # dim / separators
$script:CPrompt  = '38;5;117'            # prompt label
$script:CBack    = '38;5;166'            # BACK_PROMPT (orange)
$script:CQuit    = '38;5;32'             # EXIT_PROMPT (blue)
$script:CExample = '38;5;117'            # LIGHT_BLUE example/hint text

# Colored "{back=0, quit=exit}" hint, mirroring FFmWiz back_text().
function Nav-Hint ([switch]$NoBack) {
    $parts = @()
    if (-not $NoBack) { $parts += (Paint $script:CBack 'back=0') }
    $parts += (Paint $script:CQuit 'quit=exit')
    '{' + ($parts -join (Paint $script:CDim ', ')) + '}'
}

# --- Consistent color + log helpers ---------------------------------------
# Screen/action title: cyan text (no background), matching the preferred sample.
function Write-Title  ($m) { Write-Host ''; Write-Host (Paint $script:CHeading $m); Write-Log 'INFO' "== $m ==" }
function Write-Info   ($m) { Write-Host "[*] $m" -ForegroundColor Cyan;    Write-Log 'INFO' $m }
function Write-Ok     ($m) { Write-Host "[OK] $m" -ForegroundColor Green;  Write-Log 'INFO' "OK: $m" }
function Write-Warn   ($m) { Write-Host "[!] $m" -ForegroundColor Yellow;  Write-Log 'WARNING' $m }
function Write-Err    ($m) { Write-Host "[X] $m" -ForegroundColor Red;     Write-Log 'ERROR' $m }
function Write-Step   ($m) { Write-Host "==> $m" -ForegroundColor Magenta; Write-Log 'INFO' "step: $m" }

# Quiet OK: goes to the log only (keeps the console clean at startup).
function Log-Ok       ($m) { Write-Log 'INFO' "OK: $m" }

function Show-Banner {
    $title = 'Emoji Mapper'
    $width = 100
    try { if ([Console]::WindowWidth -gt 0) { $width = [Console]::WindowWidth } } catch { }
    $pad = [Math]::Max(0, [int](($width - $title.Length) / 2))
    Write-Host ''
    Write-Host ((' ' * $pad) + (Paint $script:CTitle $title))
    Write-Host (Paint $script:CTitle ('=' * $width))
    if ($script:LogFile) { Write-Host (Paint $script:CLogNote "Logging to: $script:LogFile") }
}

# Run a Python entry point, logging the command (no secrets) and its exit code.
# The child's output is pushed straight to the host instead of being left on the
# success stream: otherwise the caller receives "stdout + exit code" as one array
# and a test like `(Invoke-Py ...) -ne 0` reports failure for every successful
# command that happened to print something.
function Invoke-Py ($py, [string[]]$Argv) {
    Write-Log 'INFO' ("run: python " + ($Argv -join ' '))
    & $py @Argv | Out-Host
    $code = [int]$LASTEXITCODE
    Write-Log 'INFO' ("exit $code (" + $Argv[0] + ")")
    return $code
}

# Same, plus a user-visible verdict. Menu actions used to pipe Invoke-Py to
# Out-Null, which threw the real exit code away and always looked successful.
# Returns nothing on purpose: a stray value would be picked up as extra output
# by the wizard step that calls it.
function Invoke-PyReport ($py, [string[]]$Argv, $what) {
    $code = Invoke-Py $py $Argv
    if ($code -eq 0) { Write-Ok "$what finished." }
    else { Write-Err "$what failed (exit $code)." }
}

# Ask for text with a colored {back=0, quit=exit} hint. Returns the raw answer;
# the caller (a wizard step) treats '0' as "go back one step". Typing exit/quit
# ends the launcher from anywhere.
function Ask ($label) {
    $ans = Read-Host ("$label " + (Nav-Hint))
    $t = if ($null -ne $ans) { $ans.Trim() } else { '' }
    if ($t -match '^(exit|quit)$') { throw 'NAV_QUIT' }
    return $ans
}

# Yes/No with the same nav. Returns $true / $false, or the string 'back' for 0.
function Ask-YesNo ($label) {
    $ans = Read-Host ("$label [Y/n] " + (Nav-Hint))
    $t = if ($null -ne $ans) { $ans.Trim() } else { '' }
    if ($t -match '^(exit|quit)$') { throw 'NAV_QUIT' }
    if ($t -eq '0') { return 'back' }
    return ([string]::IsNullOrWhiteSpace($t) -or $t -match '^(y|yes)$')
}

# Step engine: run ordered step scriptblocks. Each returns 'ok' (advance),
# 'back' (previous step), or 'stay' (re-ask this step). 'back' from the first
# step returns $false (caller aborts to the menu) — i.e. back always moves ONE
# screen back, and the screen before step 0 is the menu.
function Run-Wizard ($steps) {
    $i = 0
    while ($i -lt $steps.Count) {
        switch (& $steps[$i]) {
            'back' { $i--; if ($i -lt 0) { return $false } }
            'stay' { }
            default { $i++ }
        }
    }
    return $true
}

# --- Prefer Windows Terminal + PowerShell 7 (single relaunch, loop-safe) ---
if (-not $NoRelaunch -and -not $env:EMOJI_MAPPER_RELAUNCHED) {
    $pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwsh -and $PSVersionTable.PSVersion.Major -lt 7) {
        $env:EMOJI_MAPPER_RELAUNCHED = '1'
        $self = $MyInvocation.MyCommand.Definition
        # Forward every switch the caller passed. Passing only -NoRelaunch used to
        # drop -Check, so a scripted doctor run silently became an interactive menu.
        $fwd = @('-NoRelaunch')
        foreach ($e in $PSBoundParameters.GetEnumerator()) {
            if ($e.Key -eq 'NoRelaunch') { continue }
            if ($e.Value -is [System.Management.Automation.SwitchParameter] -and $e.Value.IsPresent) {
                $fwd += "-$($e.Key)"
            }
        }
        try {
            if ($Check) {
                # Doctor mode stays attached and non-interactive: a detached window
                # would hand the caller exit 0 no matter what the check found.
                & $pwsh.Source -NoProfile -File $self @fwd
                exit $LASTEXITCODE
            }
            $wt = Get-Command wt.exe -ErrorAction SilentlyContinue
            if ($wt) {
                & $wt.Source $pwsh.Source -NoExit -File $self @fwd
            } else {
                & $pwsh.Source -NoExit -File $self @fwd
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
        # Out-Host, or venv's chatter would be returned alongside $py.
        & (Get-Command $base.Exe).Source @($base.Args + @('-m','venv','.venv')) | Out-Host
        $py = Get-PythonExe
        if (-not $py) { Write-Err "Failed to create .venv."; return $null }
    }
    return $py
}

# Returns $true only when the requirements install succeeded; the caller aborts
# otherwise instead of opening a menu whose every action would fail.
function Install-Deps ($py) {
    Write-Step "Installing requirements ..."
    Invoke-Py $py @('-m','pip','install','--upgrade','pip') | Out-Null   # self-upgrade is best-effort
    $code = Invoke-Py $py @('-m','pip','install','-r',(Join-Path $ScriptRoot 'requirements.txt'))
    if ($code -eq 0) { Write-Ok "Dependencies installed."; return $true }
    Write-Err "Dependency install failed (exit $code)."
    return $false
}

function Test-Deps ($py) {
    & $py -c "import requests,PIL,numpy,resvg_py" *> $null
    return ($LASTEXITCODE -eq 0)
}

function Check-Env ($py) {
    if (-not (Test-Path -LiteralPath (Join-Path $ScriptRoot '.env'))) {
        Write-Warn ".env not found. Copy .env.example to .env and fill in tokens."
        return
    }
    Log-Ok ".env present."   # quiet: log only, keep the console clean
}

function Test-Ffmpeg {
    return ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and
            (Get-Command ffprobe -ErrorAction SilentlyContinue))
}

# --- Actions --------------------------------------------------------------
# Every input prompt supports {back=0, quit=exit}: 0 aborts to the menu, exit
# quits. Each Python launch is logged (command + exit code) via Invoke-Py.
function Action-BuildGeneral ($py) {
    Write-Title "Build a general emoji pack (@YourEmojiBot)"
    $st = @{ emoji = '😀' }
    $steps = @(
        { $v = Ask "Source image folder (e.g. input\myset)"; if ($v -eq '0') { return 'back' }
          if ([string]::IsNullOrWhiteSpace($v) -or -not (Test-Path -LiteralPath $v)) {
              Write-Err "Folder not found: $v"; return 'stay' }
          $st.inDir = $v; 'ok' }.GetNewClosure(),
        { $v = Ask "Pack base name (letters/digits/_), e.g. myset"; if ($v -eq '0') { return 'back' }
          if ([string]::IsNullOrWhiteSpace($v)) { Write-Err "Base name required."; return 'stay' }
          $st.base = $v; 'ok' }.GetNewClosure(),
        { $v = Ask "Pack title, e.g. My Emojis"; if ($v -eq '0') { return 'back' }
          if ([string]::IsNullOrWhiteSpace($v)) { Write-Err "Title required."; return 'stay' }
          $st.title = $v; 'ok' }.GetNewClosure(),
        { $v = Ask "Associated standard emoji (default 😀)"; if ($v -eq '0') { return 'back' }
          if (-not [string]::IsNullOrWhiteSpace($v)) { $st.emoji = $v }; 'ok' }.GetNewClosure(),
        { $yn = Ask-YesNo "Convert + dry-run + upload now?"; if ($yn -eq 'back') { return 'back' }
          if (-not $yn) { Write-Info "Cancelled."; return 'ok' }
          $build = Join-Path 'build' $st.base
          Write-Step "Converting images -> $build ..."
          if ((Invoke-Py $py @('make_emoji_pngs.py','--in',$st.inDir,'--out',$build)) -ne 0) {
              Write-Err "Conversion failed."; return 'ok' }
          Write-Step "Dry-run preview ..."
          if ((Invoke-Py $py @('build_pack.py','--base',$st.base,'--title',$st.title,'--source-dir',$build,
                               '--token-env','GENERAL_BOT_TOKEN','--emoji',$st.emoji,'--dry-run')) -ne 0) {
              Write-Err "Dry-run failed (check .env / source)."; return 'ok' }
          if ((Invoke-Py $py @('build_pack.py','--base',$st.base,'--title',$st.title,'--source-dir',$build,
                               '--token-env','GENERAL_BOT_TOKEN','--emoji',$st.emoji)) -eq 0) {
              Write-Ok "Pack build finished." } else { Write-Err "Build failed." }
          'ok' }.GetNewClosure()
    )
    Run-Wizard $steps | Out-Null
}

function Action-ConvertOnly ($py) {
    Write-Title "Convert images to 100x100 PNGs"
    $st = @{}
    $steps = @(
        { $v = Ask "Source image folder"; if ($v -eq '0') { return 'back' }
          if (-not (Test-Path -LiteralPath $v)) { Write-Err "Folder not found."; return 'stay' }
          $st.inDir = $v; 'ok' }.GetNewClosure(),
        { $v = Ask "Output folder (blank = <folder>_emoji)"; if ($v -eq '0') { return 'back' }
          $st.outDir = $v
          $argv = @('make_emoji_pngs.py','--in',$st.inDir)
          if (-not [string]::IsNullOrWhiteSpace($st.outDir)) { $argv += @('--out',$st.outDir) }
          Invoke-PyReport $py $argv "Conversion"
          'ok' }.GetNewClosure()
    )
    Run-Wizard $steps | Out-Null
}

function Action-CoinRebuild ($py) {
    Write-Title "Crypto-coin pack rebuild (TELEGRAM_BOT_TOKEN)"
    Write-Warn "This uses the crypto-coin bot and the coins/ component."
    $script = Join-Path $ScriptRoot 'coins\rebuild_dedup.py'
    if (-not (Test-Path -LiteralPath $script)) { Write-Err "coins\rebuild_dedup.py not found."; return }
    $yn = Ask-YesNo "Run coins/rebuild_dedup.py now? (duplicate-proof: build + map + links)"
    if ($yn -eq 'back' -or -not $yn) { return }   # back or no -> return to menu
    Invoke-PyReport $py @($script) "Coin pack rebuild"
}

function Action-CollectPacks ($py) {
    Write-Title "Collect emoji from existing Telegram packs"
    Write-Info "Paste pack links/names (t.me/addemoji/...). Blank line to finish; 0 removes the last one."
    $st = @{ packs = @() }
    $steps = @(
        { $line = Ask "Pack (blank = done)"
          if ($line -eq '0') {
              # Drop the last entry. 0..(Count-2) is wrong for a single entry:
              # 0..-1 counts down and yields indices 0 and -1, i.e. that one entry twice.
              if ($st.packs.Count -gt 0) { $st.packs = @($st.packs | Select-Object -SkipLast 1); Write-Info "Removed last." }
              return 'stay' }                       # 0 = undo last entry (one step)
          if ([string]::IsNullOrWhiteSpace($line)) {
              if ($st.packs.Count -eq 0) { Write-Warn "No packs entered."; return 'back' }
              return 'ok' }
          $st.packs += $line.Trim(); return 'stay' }.GetNewClosure(),
        { $v = Ask "Token env var (default GENERAL_BOT_TOKEN)"; if ($v -eq '0') { return 'back' }
          $tokenEnv = if ([string]::IsNullOrWhiteSpace($v)) { 'GENERAL_BOT_TOKEN' } else { $v }
          Invoke-PyReport $py (@('fetch_pack.py') + $st.packs + @('--token-env',$tokenEnv)) "Collect"
          'ok' }.GetNewClosure()
    )
    Run-Wizard $steps | Out-Null
}

function Action-AddMedia ($py) {
    Write-Title "Build emoji from scratch (folder of images/animations/videos)"
    if (-not (Test-Ffmpeg)) {
        Write-Warn "ffmpeg/ffprobe not found: video emoji (.webm) will fail."
        Write-Warn "Install with: winget install Gyan.FFmpeg"
    }
    $st = @{ emoji = '😀' }
    $steps = @(
        { $v = Ask "Source folder"; if ($v -eq '0') { return 'back' }
          if (-not (Test-Path -LiteralPath $v)) { Write-Err "Folder not found."; return 'stay' }
          $st.inDir = $v; 'ok' }.GetNewClosure(),
        { $v = Ask "Associated standard emoji (default 😀)"; if ($v -eq '0') { return 'back' }
          if (-not [string]::IsNullOrWhiteSpace($v)) { $st.emoji = $v }
          Invoke-PyReport $py @('add_media.py','--in',$st.inDir,'--emoji',$st.emoji) "Add media"
          'ok' }.GetNewClosure()
    )
    Run-Wizard $steps | Out-Null
}

function Action-PublishCollection ($py) {
    Write-Title "Publish the collection into new packs (multi-format)"
    $st = @{}
    $steps = @(
        { $v = Ask "Pack base name (letters/digits only), e.g. mypack"; if ($v -eq '0') { return 'back' }
          if ([string]::IsNullOrWhiteSpace($v)) { Write-Err "Base name required."; return 'stay' }
          $st.base = $v; 'ok' }.GetNewClosure(),
        { $v = Ask "Pack title, e.g. My Collection"; if ($v -eq '0') { return 'back' }
          if ([string]::IsNullOrWhiteSpace($v)) { Write-Err "Title required."; return 'stay' }
          $st.title = $v; 'ok' }.GetNewClosure(),
        { $v = Ask "Token env var (default GENERAL_BOT_TOKEN)"; if ($v -eq '0') { return 'back' }
          $st.tokenEnv = if ([string]::IsNullOrWhiteSpace($v)) { 'GENERAL_BOT_TOKEN' } else { $v }; 'ok' }.GetNewClosure(),
        { $yn = Ask-YesNo "Dry-run then upload now?"; if ($yn -eq 'back') { return 'back' }
          if (-not $yn) { Write-Info "Cancelled."; return 'ok' }
          Write-Step "Dry-run preview ..."
          if ((Invoke-Py $py @('build_collection.py','--base',$st.base,'--title',$st.title,
                               '--token-env',$st.tokenEnv,'--dry-run')) -ne 0) {
              Write-Err "Dry-run failed (run a collect/add step first?)."; return 'ok' }
          if ((Invoke-Py $py @('build_collection.py','--base',$st.base,'--title',$st.title,
                               '--token-env',$st.tokenEnv)) -eq 0) {
              Write-Ok "Collection published." } else { Write-Err "Publish failed." }
          'ok' }.GetNewClosure()
    )
    Run-Wizard $steps | Out-Null
}

function Action-Panel ($py) {
    Write-Title "Curate panel (pick which emoji go into the pack)"
    Write-Info "Opening the web panel in your browser... (Ctrl+C here to stop it)"
    Invoke-PyReport $py @('panel.py') "Web panel"
}

function Action-RunBot ($py) {
    Write-Title "Run the Emoji Mapper bot (premium-emoji ID extractor)"
    Write-Info "Send the bot a premium emoji or a post with emoji, or add it to a channel/group."
    Write-Info "Press Ctrl+C to stop the bot."
    Invoke-PyReport $py @('emoji_bot.py') "Bot"
}

# --- Menu -----------------------------------------------------------------
# Each row: Key, Text, Action. Grouped by section; per-section numbering with a
# one-letter section prefix so keys stay unique (B/C/R).
function Menu-Item ($keyColor, $key, $text) {
    Write-Host ("  " + (Paint $keyColor "$key)") + " " + (Paint $script:CText $text))
}

function Show-Menu {
    Write-Host ''
    Write-Host (Paint $script:CBuild 'Build a single pack')
    Menu-Item $script:CKeyA 'A1' 'Build a general emoji pack  (new bot)'
    Menu-Item $script:CKeyA 'A2' 'Convert images to 100x100 PNGs only'
    Menu-Item $script:CKeyA 'A3' 'Crypto-coin pack rebuild    (coin bot)'
    Write-Host ''
    Write-Host (Paint $script:CColl 'Collection (multi-format, duplicate-proof)')
    Menu-Item $script:CKeyB 'B1' 'Collect emoji from existing packs (download)'
    Menu-Item $script:CKeyB 'B2' 'Add media from a folder (build from scratch)'
    Menu-Item $script:CKeyB 'B3' 'Publish the collection into new packs'
    Menu-Item $script:CKeyB 'B4' 'Open web panel to pick & reorder emoji (browser)'
    Write-Host ''
    Write-Host (Paint $script:CBot 'Bot')
    Menu-Item $script:CKeyC 'C1' 'Run the Emoji Mapper bot (premium-emoji ID extractor)'
    Write-Host ''
}

function Invoke-Choice ($choice, $py) {
    switch ($choice.ToLower().Trim()) {
        'a1' { Action-BuildGeneral $py }
        'a2' { Action-ConvertOnly $py }
        'a3' { Action-CoinRebuild $py }
        'b1' { Action-CollectPacks $py }
        'b2' { Action-AddMedia $py }
        'b3' { Action-PublishCollection $py }
        'b4' { Action-Panel $py }
        'c1' { Action-RunBot $py }
        { $_ -in @('q','quit','exit','0') } { return $false }
        default { Write-Warn "Unknown option: $choice  (use e.g. A1, B3, C1, or exit)" }
    }
    return $true
}

# --- Bootstrap ------------------------------------------------------------
Initialize-Log
Show-Banner

# Non-interactive environment check ("doctor") for CI / scripted use. It runs
# before Ensure-Environment / the dependency prompt on purpose: those ask
# questions, and a doctor that blocks on stdin is useless in a script.
if ($Check) {
    $py = Get-PythonExe
    if (-not $py) {
        Write-Err ".venv not found. Run the launcher once without -Check to create it."
        Write-Log 'INFO' 'doctor: exit 1'
        exit 1
    }
    Check-Env $py            # logs .env status (warns only if missing)
    Log-Ok ("Python: " + (& $py --version))
    if (Test-Ffmpeg) { Log-Ok "ffmpeg present (video emoji enabled)." }
    else { Write-Warn "ffmpeg not found: video emoji disabled (winget install Gyan.FFmpeg)." }
    if (-not (Test-Deps $py)) {
        Write-Err "Some dependencies are missing."
        Write-Log 'INFO' 'doctor: exit 1'
        exit 1
    }
    Write-Ok "All Python dependencies import correctly."
    Write-Ok "Environment check complete."
    Write-Log 'INFO' 'doctor: exit 0'
    exit 0
}

$py = Ensure-Environment
if (-not $py) { Write-Log 'CRITICAL' 'no Python environment; exiting'; exit 1 }
if (-not (Test-Deps $py)) {
    if (Confirm-YesDefault "Install/repair Python dependencies?") {
        # A broken install must not reach the menu: every action would fail on import.
        if (-not (Install-Deps $py)) { Write-Log 'CRITICAL' 'dependency install failed; exiting'; exit 1 }
    } else {
        Write-Warn "Continuing without the missing dependencies; actions may fail."
    }
}
Check-Env $py            # logs .env status (warns only if missing)
Log-Ok ("Python: " + (& $py --version))   # quiet: log only
if (Test-Ffmpeg) { Log-Ok "ffmpeg present (video emoji enabled)." }  # quiet
else { Write-Warn "ffmpeg not found: video emoji disabled (winget install Gyan.FFmpeg)." }

# --- Menu loop ------------------------------------------------------------
$running = $true
while ($running) {
    Show-Menu
    $choice = Read-Host ((Paint $script:CPrompt 'Select') + ' ' + (Nav-Hint -NoBack))
    Write-Log 'INFO' "menu selection: '$choice'"
    try {
        $running = Invoke-Choice $choice $py
    } catch {
        $msg = $_.Exception.Message
        if ($msg -eq 'NAV_QUIT') {
            $running = $false                              # 'exit' typed inside an action
            Write-Log 'INFO' 'nav: quit from action'
        } else {
            Write-Err "Action failed: $msg"
            Write-Log 'ERROR' ("exception: " + ($_ | Out-String).Trim())
        }
    }
}
Write-Info "Bye."
Write-Log 'INFO' 'launcher shutdown (normal)'
