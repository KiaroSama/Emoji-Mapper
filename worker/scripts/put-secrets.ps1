# Push the Worker's secrets from .env into Cloudflare, without any value ever
# appearing on screen, in a shell history, in a log or in git.
#
# WHY THIS EXISTS, rather than "just run wrangler secret put six times":
#   - the values are already in .env; retyping them is how a token gets pasted
#     into the wrong terminal;
#   - ADMIN_USER_IDS has to be COMPOSED from PACK_OWNER_USER_ID +
#     BOT_ALLOWED_USER_IDS, and getting that wrong is silent: the list fails
#     closed, so a typo means the bots answer nobody and nothing says why;
#   - PACK_LINKS_CHAT_ID is deliberately a secret and NOT a [vars] entry, so the
#     channel name stays out of the tracked wrangler.toml.
#
# Each value is written to wrangler's stdin. It is never echoed, never passed as
# an argument (arguments are visible in the process list), and never logged.
#
# Usage:  cd worker; .\scripts\put-secrets.ps1  [-DryRun] [-EnvFile ..\.env]

[CmdletBinding()]
param(
    [string]$EnvFile,
    # Report which keys would be set, and from where, without calling wrangler.
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$workerDir = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $workerDir) '.env' }

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "no .env at $EnvFile - copy .env.example and fill it in first"
}

# Same parse as build_pack.load_env: KEY=VALUE, '#' comments, optional quotes.
$env_ = @{}
foreach ($line in [System.IO.File]::ReadAllLines($EnvFile, [System.Text.Encoding]::UTF8)) {
    $t = $line.Trim()
    if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
        $k, $v = $t.Split('=', 2)
        $env_[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
    }
}

# ADMIN_USER_IDS = the owner plus any extra allowed ids, deduped, order kept.
# Only plain positive integers survive - the Worker's parseAdmins() rejects
# anything else anyway, and a value dropped here is better than one dropped
# silently in production.
$ids = [System.Collections.Generic.List[string]]::new()
foreach ($raw in @($env_['PACK_OWNER_USER_ID']) + ($env_['BOT_ALLOWED_USER_IDS'] -split ',')) {
    $id = "$raw".Trim()
    if ($id -match '^\d+$' -and $id -ne '0' -and -not $ids.Contains($id)) { $ids.Add($id) }
}

$plan = [ordered]@{
    GENERAL_BOT_TOKEN      = @{ value = $env_['GENERAL_BOT_TOKEN'];   from = 'GENERAL_BOT_TOKEN' }
    COIN_BOT_TOKEN         = @{ value = $env_['TELEGRAM_BOT_TOKEN'];  from = 'TELEGRAM_BOT_TOKEN (the coin bot)' }
    PACK_LINKS_CHAT_ID     = @{ value = $env_['PACK_LINKS_CHAT_ID'];  from = 'PACK_LINKS_CHAT_ID' }
    ADMIN_USER_IDS         = @{ value = ($ids -join ',');             from = "PACK_OWNER_USER_ID + BOT_ALLOWED_USER_IDS ($($ids.Count) id(s))" }
    PUBLISH_SECRET         = @{ value = $env_['WORKER_PUBLISH_SECRET']; from = 'WORKER_PUBLISH_SECRET'; generate = $true }
    GENERAL_WEBHOOK_SECRET = @{ value = $env_['GENERAL_WEBHOOK_SECRET']; from = '.env'; generate = $true }
    COIN_WEBHOOK_SECRET    = @{ value = $env_['COIN_WEBHOOK_SECRET'];    from = '.env'; generate = $true }
}

# A missing webhook/publish secret is not an error: nothing else owns it, so
# mint one. A missing TOKEN or CHANNEL is an error - inventing those is nonsense.
$generated = @{}
foreach ($name in @($plan.Keys)) {
    if (-not $plan[$name].value -and $plan[$name].generate) {
        $bytes = [byte[]]::new(32)
        [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        $plan[$name].value = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        $plan[$name].from = 'generated (32 random bytes)'
        $generated[$name] = $plan[$name].value
    }
}

$missing = @($plan.Keys | Where-Object { -not $plan[$_].value })
if ($missing) { throw "cannot continue - no value for: $($missing -join ', '). Fill them in $EnvFile." }

foreach ($name in $plan.Keys) {
    # Length only. Never the value, not even a prefix.
    Write-Host ("  {0,-22} <- {1}  ({2} chars)" -f $name, $plan[$name].from, $plan[$name].value.Length)
}

if ($DryRun) { Write-Host "`ndry run - wrangler was not called."; exit 0 }

Push-Location $workerDir
try {
    foreach ($name in $plan.Keys) {
        # stdin, not an argument: arguments show up in the process list.
        $plan[$name].value | & npx wrangler secret put $name
        if ($LASTEXITCODE -ne 0) { throw "wrangler secret put $name failed (exit $LASTEXITCODE)" }
    }
} finally { Pop-Location }

if ($generated.Count) {
    # These have to reach .env too, or the next deploy mints DIFFERENT ones and
    # every webhook delivery starts failing its secret check.
    $lines = [System.Collections.Generic.List[string]]::new(
        [string[]][System.IO.File]::ReadAllLines($EnvFile, [System.Text.Encoding]::UTF8))
    foreach ($name in $generated.Keys) {
        $key = if ($name -eq 'PUBLISH_SECRET') { 'WORKER_PUBLISH_SECRET' } else { $name }
        $i = $lines.FindIndex({ param($l) $l.TrimStart().StartsWith("$key=") })
        if ($i -ge 0) { $lines[$i] = "$key=$($generated[$name])" } else { $lines.Add("$key=$($generated[$name])") }
    }
    [System.IO.File]::WriteAllLines($EnvFile, $lines, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "`nwrote $($generated.Count) generated secret(s) back to .env (values not shown)"
}

Write-Host "`ndone. Next: wrangler deploy, then register the two webhooks (worker/README.md)."
