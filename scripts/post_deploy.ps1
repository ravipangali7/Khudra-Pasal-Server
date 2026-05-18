# Run on the API host after pulling new code (e.g. khudrapasalserver.360winx.com).
# Usage (from server/):  .\scripts\post_deploy.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "Applying database migrations..."
python manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Collecting static files..."
python manage.py collectstatic --noinput
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Done. Restart the Django/gunicorn service if it is already running."
