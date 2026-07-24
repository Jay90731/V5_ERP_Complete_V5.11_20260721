$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Get-Content -LiteralPath (Join-Path $Root "v5_config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$ReportDir = Join-Path $Root "environment_reports"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$JsonPath = Join-Path $ReportDir "environment_$Stamp.json"
$TextPath = Join-Path $ReportDir "environment_$Stamp.txt"

function Resolve-ConfiguredPath {
  param([string]$Value)
  if ([System.IO.Path]::IsPathRooted($Value)) { return [System.IO.Path]::GetFullPath($Value) }
  return [System.IO.Path]::GetFullPath((Join-Path $Root $Value))
}

function Find-First {
  param([object[]]$Candidates)
  foreach ($candidate in @($Candidates)) {
    $path = Resolve-ConfiguredPath ([string]$candidate)
    if (Test-Path -LiteralPath $path) { return $path }
  }
  return ""
}

$python = Find-First $Config.pythonCandidates
$tesseract = Find-First $Config.tesseractCandidates
$tessdata = Find-First $Config.tessdataCandidates
$official = Resolve-ConfiguredPath ([string]$Config.officialStateDir)
$erp = Resolve-ConfiguredPath ([string]$Config.erpExePath)
$menuitem = Join-Path $Root "reference_data\MENUITEM.XLS"
$productLookup = Join-Path $Root "reference_data\product_lookup.json"
$xlrdReady = $false
if ($python) {
  & $python -c "import xlrd" 2>$null
  $xlrdReady = ($LASTEXITCODE -eq 0)
}

$checks = [ordered]@{
  erp_exe = [bool](Test-Path -LiteralPath $erp)
  python = [bool]$python
  tesseract = [bool]$tesseract
  tessdata = [bool]$tessdata
  official_history = [bool](Test-Path -LiteralPath (Join-Path $official "order_history.json"))
  official_excel = [bool](Test-Path -LiteralPath (Join-Path $official "current_orders.xlsx"))
  menuitem = [bool](Test-Path -LiteralPath $menuitem)
  product_lookup = [bool](Test-Path -LiteralPath $productLookup)
  xlrd = [bool]$xlrdReady
}
$critical = @("python", "tesseract", "tessdata", "official_history", "official_excel", "menuitem", "product_lookup", "xlrd")
$readyReplay = -not @($critical | Where-Object { -not $checks[$_] }).Count
$readyErp = $readyReplay -and $checks.erp_exe
$result = [ordered]@{
  checked_at = (Get-Date).ToString("o")
  ready_for_replay = $readyReplay
  ready_for_erp_daily_run = $readyErp
  checks = $checks
  resolved = [ordered]@{
    erp_exe = $erp
    python = $python
    tesseract = $tesseract
    tessdata = $tessdata
    official_state = $official
  }
}
$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $JsonPath -Encoding UTF8
$lines = @(
  "V5 environment check",
  "checked_at=$($result.checked_at)",
  "ready_for_replay=$readyReplay",
  "ready_for_erp_daily_run=$readyErp"
)
foreach ($key in $checks.Keys) { $lines += "$key=$($checks[$key])" }
$lines += "json_report=$JsonPath"
$lines | Set-Content -LiteralPath $TextPath -Encoding UTF8
Get-Content -LiteralPath $TextPath -Encoding UTF8
if (-not $readyReplay) { exit 1 }
if (-not $readyErp) { exit 2 }
