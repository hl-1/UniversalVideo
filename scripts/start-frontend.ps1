$ErrorActionPreference = 'Stop'
Set-Location (Join-Path (Split-Path -Parent $PSScriptRoot) 'frontend')
if (Get-NetTCPConnection -State Listen -LocalPort 5174 -ErrorAction SilentlyContinue) {
  throw 'Port 5174 is already in use. Reuse or stop the existing project frontend before starting.'
}
if (-not (Test-Path -LiteralPath 'node_modules\.bin\vite.cmd')) {
  throw 'Frontend dependencies are missing. Run scripts/setup.ps1 first.'
}
npm.cmd run dev
exit $LASTEXITCODE
