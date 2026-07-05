$ErrorActionPreference = "Stop"

Set-Location (Join-Path (Split-Path -Parent $PSScriptRoot) "frontend")
npm.cmd install
npm.cmd run dev
