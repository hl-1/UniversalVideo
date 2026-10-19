param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
. "$PSScriptRoot\resolve-python.ps1"

if (Get-NetTCPConnection -State Listen -LocalPort 8001 -ErrorAction SilentlyContinue) {
  throw 'Port 8001 is already in use. Reuse or stop the existing project backend before starting.'
}
$backendPython = Resolve-BackendPython -PythonPath $PythonPath
& $backendPython -c "import fastapi, uvicorn, mcp"
if ($LASTEXITCODE -ne 0) { throw 'Backend dependencies are missing. Run scripts/setup.ps1 first.' }
Write-Host "Backend Python: $backendPython"
& $backendPython -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
exit $LASTEXITCODE
