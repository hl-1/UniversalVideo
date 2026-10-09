$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

$backendPython = ".venv\Scripts\python.exe"
if (Test-Path ".run\startup-venv\Scripts\python.exe") {
  $backendPython = ".run\startup-venv\Scripts\python.exe"
}
if (-not (Test-Path $backendPython)) {
  python -m venv .venv
}

& $backendPython -m pip install -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Backend dependency installation failed." }
& $backendPython -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
