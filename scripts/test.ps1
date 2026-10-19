param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
. "$PSScriptRoot\resolve-python.ps1"
$backendPython = Resolve-BackendPython -PythonPath $PythonPath

$backendJob = Start-Job -ArgumentList $projectRoot, $backendPython -ScriptBlock {
  param($root, $pythonExe)
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
  $env:PYTHONIOENCODING = 'utf-8'
  Set-Location $root
  & $pythonExe -m unittest discover -s backend/tests -t . -p 'test_*.py'
  if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
}
$frontendJob = Start-Job -ArgumentList $projectRoot -ScriptBlock {
  param($root)
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
  Set-Location (Join-Path $root 'frontend')
  npm.cmd test
  if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed.' }
  npm.cmd run build
  if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
}
$jobs = @($backendJob, $frontendJob)
try {
  $jobs | Wait-Job | Out-Null
  $jobs | Receive-Job -ErrorAction Continue
  if ($jobs.State -contains 'Failed') { throw 'Validation failed; inspect the output above.' }
  git diff --check
  if ($LASTEXITCODE -ne 0) { throw 'git diff --check failed.' }
} finally {
  $jobs | Remove-Job -Force
}
