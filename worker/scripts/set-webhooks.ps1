# Register ONE webhook per bot, each on its own path with its own secret.
#
# WHY A SCRIPT: the two registrations must agree with the two secrets already
# pushed to Cloudflare. Typing them by hand means the mismatch is silent --
# Telegram accepts setWebhook happily, then every delivery is rejected 401 by
# the Worker and the bot simply looks dead.
#
# WHAT IT REFUSES TO DO SILENTLY: a token that already has a DIFFERENT webhook,
# or one currently being polled, is reported and skipped unless -Force. A token
# can serve getUpdates OR a webhook, never both: registering one here makes
# emojikit/emoji_bot.py go deaf on that token, and that must be a decision, not a
# surprise.
#
# Usage:
#   .\scripts\set-webhooks.ps1 -BaseUrl https://<worker>.workers.dev
#   .\scripts\set-webhooks.ps1 -BaseUrl ... -Only coin       # one bot
#   .\scripts\set-webhooks.ps1 -Status                       # just report
#   .\scripts\set-webhooks.ps1 -Delete -Only general         # hand it back

[CmdletBinding()]
param(
    [string]$BaseUrl,
    [ValidateSet('general', 'coin', 'both')][string]$Only = 'both',
    [string]$EnvFile,
    # Report getWebhookInfo for each bot and exit. No change.
    [switch]$Status,
    # deleteWebhook: hand the token back to emojikit/emoji_bot.py's poller.
    [switch]$Delete,
    # Replace a webhook that already points somewhere else.
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$workerDir = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $workerDir) '.env' }
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "no .env at $EnvFile" }

# Same parse as build_pack.load_env.
$cfg = @{}
foreach ($line in [System.IO.File]::ReadAllLines($EnvFile, [System.Text.Encoding]::UTF8)) {
    $t = $line.Trim()
    if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
        $k, $v = $t.Split('=', 2)
        $cfg[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
    }
}

$bots = @(
    @{ name = 'general'; token = $cfg['GENERAL_BOT_TOKEN'];  secret = $cfg['GENERAL_WEBHOOK_SECRET']; path = '/tg/general' }
    @{ name = 'coin';    token = $cfg['TELEGRAM_BOT_TOKEN']; secret = $cfg['COIN_WEBHOOK_SECRET'];    path = '/tg/coin' }
) | Where-Object { $Only -eq 'both' -or $_.name -eq $Only }

function Invoke-Bot {
    param([string]$Token, [string]$Method, [hashtable]$Body = @{})
    # The token is in the URL because the Bot API has no other way to carry it.
    # It is never written to output: only $r.result / $r.description are shown.
    $uri = "https://api.telegram.org/bot$Token/$Method"
    try {
        $r = Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json' `
                               -Body ($Body | ConvertTo-Json -Compress) -TimeoutSec 30
    } catch {
        # A 4xx from Telegram arrives as a terminating error whose body holds
        # the real reason; surfacing "400 Bad Request" alone is useless.
        $detail = $_.ErrorDetails.Message
        if ($detail) { return (ConvertFrom-Json $detail) }
        throw
    }
    return $r
}

foreach ($b in $bots) {
    if (-not $b.token) { Write-Host "  $($b.name): no token in .env - skipped"; continue }

    $info = (Invoke-Bot -Token $b.token -Method 'getWebhookInfo').result
    $current = if ($info.url) { $info.url } else { '(none - polling)' }

    if ($Status) {
        $pending = if ($null -ne $info.pending_update_count) { $info.pending_update_count } else { '?' }
        $lastErr = if ($info.last_error_message) { " | last error: $($info.last_error_message)" } else { '' }
        Write-Host "  $($b.name): $current | pending $pending$lastErr"
        continue
    }

    if ($Delete) {
        $r = Invoke-Bot -Token $b.token -Method 'deleteWebhook'
        Write-Host "  $($b.name): deleteWebhook -> $($r.ok)  (emojikit/emoji_bot.py can poll this token again)"
        continue
    }

    if (-not $BaseUrl) { throw "-BaseUrl is required (e.g. https://emoji-mapper-bots.<sub>.workers.dev)" }
    if (-not $b.secret) { throw "$($b.name): no webhook secret in .env - run .\scripts\put-secrets.ps1 first" }
    $target = $BaseUrl.TrimEnd('/') + $b.path

    if ($info.url -and $info.url -ne $target -and -not $Force) {
        Write-Host "  $($b.name): ALREADY points at $current - refusing to replace it. Re-run with -Force."
        continue
    }

    # allowed_updates mirrors emojikit/emoji_bot.py: nothing else is acted on, and every
    # extra type is an update Telegram delivers for the Worker to discard.
    $r = Invoke-Bot -Token $b.token -Method 'setWebhook' -Body @{
        url                  = $target
        secret_token         = $b.secret
        allowed_updates      = @('message', 'edited_message', 'channel_post')
        drop_pending_updates = $false
    }
    if ($r.ok) {
        Write-Host "  $($b.name): -> $target"
        if ($current -eq '(none - polling)') {
            Write-Host "      note: this token is no longer pollable. emojikit/emoji_bot.py will receive nothing on it."
        }
    } else {
        throw "$($b.name): setWebhook failed - $($r.description)"
    }
}

if (-not $Status -and -not $Delete) {
    Write-Host "`nVerify with:  .\scripts\set-webhooks.ps1 -Status"
}
