function Resolve-BackendPython {
  param([string]$PythonPath)

  $projectRoot = Split-Path -Parent $PSScriptRoot
  $candidates = if ($PythonPath) { @($PythonPath) } else {
    @((Join-Path $projectRoot '.venv\Scripts\python.exe'),
      (Join-Path $projectRoot '.run\startup-venv\Scripts\python.exe'))
  }
  foreach ($candidate in $candidates) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    try {
      & $candidate -c "import sys; sys.exit(0)" 2>$null
      if ($LASTEXITCODE -eq 0) { return (Resolve-Path -LiteralPath $candidate).Path }
    } catch { continue }
  }
  throw 'No working project Python found. Run scripts/setup.ps1 or pass -PythonPath explicitly.'
}
