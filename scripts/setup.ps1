param([string]$PythonCommand = 'python')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

& $PythonCommand -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize .venv.' }
& .\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
npm.cmd --prefix frontend ci
if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
Write-Host 'Dependencies initialized. Start the backend and frontend in separate terminals.'
