param(
  [string]$Source = "",
  [string]$Database = "",
  [string]$Report = ""
)

$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($Source)) {
  $Source = Join-Path (Split-Path -Parent $Root) "V5_ERP_Complete_V5.10_20260721"
}
if ([string]::IsNullOrWhiteSpace($Database)) {
  $Database = Join-Path $Root "data\orders_test.db"
}
if ([string]::IsNullOrWhiteSpace($Report)) {
  $Report = Join-Path $Root ("reports\v510_import_{0}.json" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
}

python (Join-Path $Root "v6.py") migrate --db $Database
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python (Join-Path $Root "v6.py") import-v5 --db $Database --source $Source --report $Report
exit $LASTEXITCODE
