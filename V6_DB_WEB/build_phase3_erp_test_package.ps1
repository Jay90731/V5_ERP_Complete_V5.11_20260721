param(
  [string]$Destination = "",
  [string]$RuntimeSource = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($Destination)) {
  $Destination = Join-Path $Root (
    "dist\V6_PHASE3_ERP_TEST_{0}" -f (Get-Date -Format "yyyyMMdd")
  )
}
if ([string]::IsNullOrWhiteSpace($RuntimeSource)) {
  $RuntimeSource = Join-Path (Split-Path -Parent $Root) `
    "V5_ERP_Complete_V5.10_20260721\V5_daily_full_flow\runtime"
}
$Destination = [System.IO.Path]::GetFullPath($Destination)
$RuntimeSource = [System.IO.Path]::GetFullPath($RuntimeSource)

if (Test-Path -LiteralPath $Destination) {
  throw "測試包目的資料夾已存在，為避免覆蓋資料請改用新資料夾：$Destination"
}
if (-not (Test-Path -LiteralPath $RuntimeSource -PathType Container)) {
  throw "找不到 V5.10 穩定 runtime：$RuntimeSource"
}
if (-not (Test-Path -LiteralPath (Join-Path $Root "data\orders_test.db"))) {
  throw "找不到 data\orders_test.db"
}
# 第四階段之後專案會合法存在正式 orders.db。ERP 測試包仍只能明確
# 複製 orders_test.db；完成後再確認正式資料庫沒有被帶進目的資料夾。

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$files = @(
  "v6.py",
  "pyproject.toml",
  "phase3_config.json",
  "phase3_labels.json",
  "phase3_worker.ps1",
  "phase3_launcher.ps1",
  "check_phase3_environment.ps1",
  "START_V6_PHASE3_TEST.bat",
  "STOP_V6_PHASE3_NOW.bat",
  "CHECK_V6_PHASE3_ENVIRONMENT.bat"
)
foreach ($relative in $files) {
  Copy-Item -LiteralPath (Join-Path $Root $relative) `
    -Destination (Join-Path $Destination $relative)
}

foreach ($directory in @("components", "migrations", "src")) {
  $result = & robocopy (Join-Path $Root $directory) `
    (Join-Path $Destination $directory) /E /R:1 /W:1 /NFL /NDL /NJH /NJS
  if ($LASTEXITCODE -gt 7) {
    throw "複製 $directory 失敗，Robocopy=$LASTEXITCODE"
  }
}

$runtimeResult = & robocopy $RuntimeSource `
  (Join-Path $Destination "runtime") /E /R:1 /W:1 /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -gt 7) {
  throw "複製 runtime 失敗，Robocopy=$LASTEXITCODE"
}

New-Item -ItemType Directory -Force -Path `
  (Join-Path $Destination "data"), `
  (Join-Path $Destination "runs"), `
  (Join-Path $Destination "control"), `
  (Join-Path $Destination "environment_reports"), `
  (Join-Path $Destination "reports") | Out-Null
Copy-Item -LiteralPath (Join-Path $Root "data\orders_test.db") `
  -Destination (Join-Path $Destination "data\orders_test.db")
if (Test-Path -LiteralPath (Join-Path $Destination "data\orders.db")) {
  throw "安全檢查失敗：ERP 測試包不得包含正式 orders.db"
}
Copy-Item -LiteralPath (Join-Path $Root "docs\PHASE3_ERP_TEST_GUIDE.md") `
  -Destination (Join-Path $Destination "ERP_TEST_GUIDE.md")

$manifest = Get-ChildItem -LiteralPath $Destination -File -Recurse |
  Where-Object {
    $_.FullName -notlike "*\data\*" -and
    $_.FullName -notlike "*\runs\*" -and
    $_.FullName -notlike "*\control\*"
  } |
  ForEach-Object {
    [pscustomobject][ordered]@{
      relative_path = $_.FullName.Substring($Destination.Length + 1)
      size = $_.Length
      sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
  }
$manifest | ConvertTo-Json -Depth 4 |
  Set-Content -LiteralPath (Join-Path $Destination "package_manifest.json") `
    -Encoding UTF8

Write-Output "PACKAGE_READY=$Destination"
Write-Output "START=$(Join-Path $Destination 'START_V6_PHASE3_TEST.bat')"
Write-Output "CHECK=$(Join-Path $Destination 'CHECK_V6_PHASE3_ENVIRONMENT.bat')"
exit 0
