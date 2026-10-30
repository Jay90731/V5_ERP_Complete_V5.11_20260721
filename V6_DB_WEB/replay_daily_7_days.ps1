param(
  [string]$OcrJson = "",
  [string]$Database = "",
  [string]$DateFrom = "2026-07-15",
  [string]$DateTo = "2026-07-21",
  [string]$Report = ""
)

$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($Database)) {
  $Database = Join-Path $Root "data\orders_test.db"
}
if ([string]::IsNullOrWhiteSpace($OcrJson)) {
  $OcrJson = Join-Path (Split-Path -Parent $Root) "V5_ERP_Complete_V5.10_20260721\V5_daily_full_flow\runs\2026-07-21\run_20260721_114213_624\ocr\data\orders.json"
}
if ([string]::IsNullOrWhiteSpace($Report)) {
  $Report = Join-Path $Root ("reports\daily_7_days_replay_{0}.json" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
}

python (Join-Path $Root "v6.py") replay-ocr `
  --db $Database `
  --input $OcrJson `
  --date-from $DateFrom `
  --date-to $DateTo `
  --job-type daily_7_days `
  --report $Report
exit $LASTEXITCODE

