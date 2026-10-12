# Packs everything the server needs into deploy\bundle\ (run on your Windows PC, repo root):
#   .\deploy\export-local.ps1
# - app.tar.gz       : the code (git archive of the last commit - no .env, no secrets)
# - n8n_local.dump   : n8n database (workflows + encrypted Google credentials)
# - screener_local.dump : agent checkpoints, evaluations, interview links
# - secrets\         : Google token + OAuth client (copied as-is)
# - .env.server      : your .env with production values switched on (review it!)
# The bundle folder is git-ignored. It contains secrets - don't share it.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "deploy\bundle"
New-Item -ItemType Directory -Force $out | Out-Null
Set-Location $root

Write-Host "==> Code (git archive of HEAD)"
git archive --format=tar.gz -o (Join-Path $out "app.tar.gz") HEAD

Write-Host "==> Database dumps"
$envVars = @{}
Get-Content .env | Where-Object { $_ -match '^([A-Z0-9_]+)=(.*)$' } | ForEach-Object { $envVars[$Matches[1]] = $Matches[2] }
$pgUser = if ($envVars['POSTGRES_USER']) { $envVars['POSTGRES_USER'] } else { 'postgres' }
foreach ($db in @('n8n', 'screener')) {
    $file = Join-Path $out "${db}_local.dump"
    # Dump inside the container, then copy out (binary-safe on Windows)
    docker compose exec -T postgres pg_dump -U $pgUser -d $db -Fc -f "/tmp/${db}.dump"
    docker compose cp "postgres:/tmp/${db}.dump" $file | Out-Null
    Write-Host ("    {0} ({1:N0} KB)" -f $file, ((Get-Item $file).Length / 1KB))
}

Write-Host "==> Secrets"
Copy-Item -Recurse -Force (Join-Path $root "secrets") (Join-Path $out "secrets")

Write-Host "==> .env.server (production values)"
$domain = Read-Host "Your domain (e.g. muneeb-screener.duckdns.org)"
$email = Read-Host "Email for HTTPS certificate notices"
$bucket = Read-Host "S3 backup bucket name (leave empty to skip backups)"
$overrides = [ordered]@{
    APP_ENV = 'prod'; DOMAIN = $domain; ACME_EMAIL = $email
    PUBLIC_BASE_URL = "https://$domain"; N8N_WEBHOOK_URL = "https://$domain/"
    N8N_HOST = $domain; N8N_PROTOCOL = 'https'; BACKUP_S3_BUCKET = $bucket
}
$lines = Get-Content .env | Where-Object { -not ($_ -match '^([A-Z0-9_]+)=' -and $overrides.Contains($Matches[1])) }
$lines += ''; $lines += '# ---- production overrides (export-local.ps1) ----'
$lines += $overrides.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }
[IO.File]::WriteAllLines((Join-Path $out ".env.server"), $lines, (New-Object Text.UTF8Encoding $false))

Write-Host "`nBundle ready in deploy\bundle\ - next: upload it (see deploy\README.md, step 6)." -ForegroundColor Green
