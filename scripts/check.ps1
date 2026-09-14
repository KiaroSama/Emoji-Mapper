<#
.SYNOPSIS
    Run the project's own checks: byte-compile every source file, lint it, then
    run the full unit suite. This is the single command CI and a developer both
    run, so the two cannot drift.

.DESCRIPTION
    - Resolves the repository root from this file's own location, so it works
      from any current directory and from a path containing spaces.
    - Prefers the repository .venv (Windows or POSIX layout), else a Python on
      PATH.
    - Non-interactive and bounded: each step gets a wall-clock ceiling and its
      process tree is terminated if it is exceeded (exit 124).
    - Propagates a real exit code: 0 only when every step passed.

.PARAMETER Python
    Interpreter to use instead of the auto-detected one.

.PARAMETER TimeoutSeconds
    Wall-clock ceiling for each step. Default 1800 (the suite takes ~75 s).
#>

[CmdletBinding()]
param(
    [string]$Python = '',
    [int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)

# The suite prints emoji and writes UTF-8 logs; without this the child inherits
# the console code page and dies on encode instead of on a real failure.
$env:PYTHONUTF8 = '1'
$testTemp = Join-Path $Root 'logs/test-temp'
New-Item -ItemType Directory -Path $testTemp -Force | Out-Null
$env:TEMP = $testTemp
$env:TMP = $testTemp
$env:TMPDIR = $testTemp

function Resolve-Python {
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python)) {
            $cmd = Get-Command $Python -ErrorAction SilentlyContinue
            if (-not $cmd) { return $null }
            return $cmd.Source
        }
        return (Resolve-Path -LiteralPath $Python).Path
    }
    foreach ($rel in @('.venv/Scripts/python.exe', '.venv/bin/python')) {
        $full = Join-Path $Root $rel
        if (Test-Path -LiteralPath $full) { return $full }
    }
    # 'python' before 'python3': on Windows the bare 'python3' is usually the
    # Microsoft Store alias stub, which launches the Store instead of running
    # anything. Skip anything under WindowsApps for the same reason. On Linux,
    # 'python' is absent or is the selected interpreter, so the order is moot.
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { return $cmd.Source }
    }
    return $null
}

# The repo venv first, PATH second -- same order as the interpreter, so a venv
# that pins a ruff version is not silently overruled by a different one on PATH.
function Resolve-Ruff {
    foreach ($rel in @('.venv/Scripts/ruff.exe', '.venv/bin/ruff')) {
        $full = Join-Path $Root $rel
        if (Test-Path -LiteralPath $full) { return $full }
    }
    $cmd = Get-Command ruff -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# Run one step under a wall-clock ceiling. Returns its real exit code, or 124 if
# it had to be killed -- a timeout is a failure, never a silent pass.
function Invoke-Step {
    param([string]$Label, [string]$Exe, [string[]]$Argv)

    Write-Host "==> $Label" -ForegroundColor Magenta
    $p = Start-Process -FilePath $Exe -ArgumentList $Argv -WorkingDirectory $Root `
                       -NoNewWindow -PassThru
    if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
        Write-Host "[X] $Label exceeded ${TimeoutSeconds}s; terminating." -ForegroundColor Red
        # Kill($true) takes the whole tree (PS7/.NET 5+); the fallback covers a
        # host that only has the single-process overload.
        try { $p.Kill($true) } catch { try { $p.Kill() } catch { } }
        return 124
    }
    $code = $p.ExitCode
    if ($code -eq 0) { Write-Host "[OK] $Label" -ForegroundColor Green }
    else { Write-Host "[X] $Label failed (exit $code)." -ForegroundColor Red }
    return $code
}

$py = Resolve-Python
if (-not $py) {
    Write-Host "[X] No Python found. Create the venv first (run.ps1), or pass -Python <path>." -ForegroundColor Red
    exit 2
}
Write-Host "Python: $py" -ForegroundColor DarkGray

# Git's source list includes new unignored files and excludes runtime snapshots,
# environments, private data and caches. Compiling '.' traversed all of them.
$sources = @(& git -c core.quotePath=false -C $Root ls-files --cached --others --exclude-standard -- '*.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$sourceArgs = @($sources | Where-Object { Test-Path -LiteralPath (Join-Path $Root $_) } |
    ForEach-Object { '"' + $_ + '"' })
if (-not $sourceArgs.Count) { throw 'No Python source files were discovered.' }
$code = Invoke-Step 'Byte-compile all sources' $py (@('-m', 'compileall', '-q') + $sourceArgs)
if ($code -ne 0) { exit $code }

# `ruff check .` and nothing else: ruff.toml at the repo root carries the rule
# set and the exclusions. Passing --select/--exclude here is how CI and a local
# run start linting two different things.
$ruff = Resolve-Ruff
if (-not $ruff) {
    Write-Host "[X] ruff not found. Install it: python -m pip install ruff" -ForegroundColor Red
    exit 2
}
$code = Invoke-Step 'Lint (ruff)' $ruff @('check', '.')
if ($code -ne 0) { exit $code }

# -t . is REQUIRED, not cosmetic: without it the tests directory becomes the top
# level, modules load as `test_x` instead of `tests.test_x`, and tests/__init__.py
# -- which scrubs credentials and refuses non-loopback sockets -- never runs.
$code = Invoke-Step 'Unit tests' $py @('-m', 'unittest', 'discover', '-s', 'tests', '-t', '.', '-p', 'test_*.py')
if ($code -ne 0) { exit $code }

Write-Host "[OK] All checks passed." -ForegroundColor Green
exit 0
