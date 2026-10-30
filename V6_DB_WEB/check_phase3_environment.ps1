$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Root "phase3_config.json"
$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 |
  ConvertFrom-Json

function Resolve-Candidate {
  param([string]$Value)
  if ([System.IO.Path]::IsPathRooted($Value)) {
    if (Test-Path -LiteralPath $Value -PathType Leaf) { return $Value }
    return ""
  }
  if ($Value -match '[\\/]') {
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $Root $Value))
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    return ""
  }
  $command = Get-Command $Value -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  return ""
}

$Python = ""
foreach ($candidate in @($Config.python_candidates)) {
  $Python = Resolve-Candidate -Value ([string]$candidate)
  if (-not [string]::IsNullOrWhiteSpace($Python)) { break }
}
if ([string]::IsNullOrWhiteSpace($Python)) {
  throw "找不到 Python。"
}

$ReportDir = Join-Path $Root "environment_reports"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
$Report = Join-Path $ReportDir (
  "phase3_environment_{0}.json" -f (Get-Date -Format "yyyyMMdd_HHmmss")
)
& $Python -X utf8 (Join-Path $Root "v6.py") check-phase3 `
  --config $ConfigPath `
  --report $Report
exit $LASTEXITCODE
