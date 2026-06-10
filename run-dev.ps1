# Local dev launcher: loads .env.secrets into this process, then starts the server.
# Usage: .\run-dev.ps1
$ErrorActionPreference = "Stop"

$secrets = Join-Path $PSScriptRoot ".env.secrets"
if (-not (Test-Path $secrets)) {
    throw ".env.secrets not found. Copy .env.secrets.example and fill in real values."
}

Get-Content $secrets | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        Set-Item -Path "env:$($Matches[1])" -Value $Matches[2].Trim('"')
    }
}

$env:CONFIG_PATH = Join-Path $PSScriptRoot "config.yaml"
uv run (Join-Path $PSScriptRoot "main.py")
