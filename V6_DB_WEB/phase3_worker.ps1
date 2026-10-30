param(
  [switch]$ResumeLatest,
  [ValidateSet("daily_7_days", "monthly_6_months")]
  [string]$Mode = "daily_7_days"
)

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
  throw "找不到 Python。請先執行環境檢查。"
}

$createdNew = $false
$mutex = New-Object System.Threading.Mutex(
  $true,
  "Local\WeddingOrderV6Phase3Daily",
  [ref]$createdNew
)
if (-not $createdNew) {
  $mutex.Dispose()
  throw "V6 流程已經在執行，不能同時啟動兩次。"
}

function Clear-ControlFlag {
  param([Parameter(Mandatory = $true)][string]$Path)
  for ($attempt = 1; $attempt -le 12; $attempt++) {
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($attempt -lt 12) {
      Start-Sleep -Milliseconds ([Math]::Min(800, 40 * [Math]::Pow(2, $attempt - 1)))
    }
  }
  throw "無法清除舊的停止旗標：$Path"
}

try {
  $StopFlag = Join-Path $Root "control\STOP_V6_NOW.flag"
  Clear-ControlFlag -Path $StopFlag

  $EnvironmentReports = Join-Path $Root "environment_reports"
  New-Item -ItemType Directory -Force -Path $EnvironmentReports | Out-Null
  $EnvironmentReport = Join-Path $EnvironmentReports (
    "phase3_environment_auto_{0}.json" -f (Get-Date -Format "yyyyMMdd_HHmmss_fff")
  )
  & $Python -X utf8 (Join-Path $Root "v6.py") check-phase3 `
    --config $ConfigPath --report $EnvironmentReport
  if ($LASTEXITCODE -ne 0) {
    throw "ERP 執行環境檢查未通過，請查看：$EnvironmentReport"
  }

  Clear-ControlFlag -Path $StopFlag
  $arguments = @(
    (Join-Path $Root "v6.py"),
    "run-daily-erp",
    "--config",
    $ConfigPath,
    "--mode",
    $Mode
  )
  if ($ResumeLatest) { $arguments += "--resume" }
  & $Python -X utf8 @arguments
  exit $LASTEXITCODE
} finally {
  try { $mutex.ReleaseMutex() } catch { }
  $mutex.Dispose()
}
