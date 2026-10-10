# Creates .env from .env.example and fills every secret with a random value.
# Usage (from the repo root):   .\scripts\init-env.ps1
# Refuses to overwrite an existing .env (that would change N8N_ENCRYPTION_KEY
# and break n8n's saved credentials). Use -Force only on a fresh setup.
param([switch]$Force)

$root = Split-Path -Parent $PSScriptRoot
$example = Join-Path $root ".env.example"
$target = Join-Path $root ".env"

if ((Test-Path $target) -and -not $Force) {
    Write-Host ".env already exists - not touching it. (Use -Force to regenerate on a FRESH setup.)" -ForegroundColor Yellow
    exit 1
}

function New-Secret([int]$bytes = 32) {
    # Cryptographically secure random bytes -> hex (URL/DB safe, no special chars)
    $buf = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    -join ($buf | ForEach-Object { $_.ToString("x2") })
}

$secrets = @{
    "POSTGRES_PASSWORD"         = New-Secret 24
    "N8N_DB_PASSWORD"           = New-Secret 24
    "SCREENER_DB_PASSWORD"      = New-Secret 24
    "N8N_ENCRYPTION_KEY"        = New-Secret 32
    "SCREENER_API_KEY"          = New-Secret 32
    "META_WEBHOOK_VERIFY_TOKEN" = New-Secret 16
    "VAPI_WEBHOOK_SECRET"       = New-Secret 32
}

$lines = Get-Content $example | ForEach-Object {
    if ($_ -match '^([A-Z0-9_]+)=' -and $secrets.ContainsKey($Matches[1])) {
        "$($Matches[1])=$($secrets[$Matches[1]])"
    } else { $_ }
}
# UTF-8 without BOM so Docker Compose reads the first line correctly
[System.IO.File]::WriteAllLines($target, $lines, (New-Object System.Text.UTF8Encoding $false))
Write-Host "Created .env with fresh secrets. Back it up somewhere safe (password manager)." -ForegroundColor Green
