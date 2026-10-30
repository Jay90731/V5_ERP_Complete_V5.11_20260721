$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Name = "BHQ_WATCHER_OFFLINE_FIXED_PATH_$Stamp"
$Output = Join-Path $Root "dist\$Name"
$Payload = Join-Path $Output "payload"
New-Item -ItemType Directory -Force -Path $Payload | Out-Null

$files = @(
  "bhq_watch.py",
  "bhq_watcher_formal.json",
  "RUN_BHQ_WATCHER_FORMAL.ps1",
  "RUN_BHQ_WATCHER_FORMAL.bat",
  "INSTALL_BHQ_WATCHER_FORMAL_TASK.ps1",
  "ACTIVATE_BHQ_WATCHER_FORMAL.ps1",
  "BHQ_WATCHER_CONTROL.ps1",
  "啟動BHQ自動匯入.bat",
  "停止BHQ自動匯入.bat",
  "查看BHQ匯入狀態.bat",
  "開啟BHQ匯入資料夾.bat",
  "重試BHQ失敗檔案.bat",
  "src\v6_db_web\bhq_watcher.py",
  "src\v6_db_web\bhq_excel.py",
  "src\v6_db_web\bhq_import_service.py",
  "src\v6_db_web\database_lock.py",
  "src\v6_db_web\db.py",
  "tools\Resolve-BhqRuntime.ps1",
  "tools\bhq_formal_database_guard.py",
  "tools\bhq_watcher_acceptance.py",
  "docs\BHQ_LOCAL_FOLDER_WATCHER.md",
  "tests\test_bhq_watcher.py",
  "tests\test_bhq_formal_guard.py"
)
foreach ($relative in $files) {
  if ($relative -match '^(data|migrations)[\\/]') {
    throw "部署清單不得包含 data、orders.db 或 migration：$relative"
  }
  $source = Join-Path $Root $relative
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "部署檔案不存在：$source"
  }
  $destination = Join-Path $Payload $relative
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
  Copy-Item -LiteralPath $source -Destination $destination -Force
}

$installer = @'
$ErrorActionPreference = "Stop"
$ExpectedRoot = "C:\Users\auntstella\Desktop\V6_DB_WEB"
$ProjectRoot = $ExpectedRoot
$Database = Join-Path $ProjectRoot "data\orders.db"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Payload = Join-Path $PackageRoot "payload"

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
  throw "固定正式專案不存在；請先部署最新 R4＋BHQ 統一專案：$ProjectRoot"
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ($ProjectRoot.TrimEnd('\') -ine $ExpectedRoot.TrimEnd('\')) {
  throw "拒絕 R3、DUAL_R* 或其他專案：$ProjectRoot"
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "data") -PathType Container)) {
  throw "固定 data 資料夾不存在；部署器不會建立它。"
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
  throw "固定正式 orders.db 不存在；部署器不會建立 SQLite：$Database"
}
if ((Get-Item -LiteralPath $Database).Length -le 0) {
  throw "固定正式 orders.db 是 0 位元組，拒絕部署。"
}
foreach ($required in @("v6.py", "src\v6_db_web")) {
  if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $required))) {
    throw "最新統一專案不完整，缺少：$required"
  }
}

$Python = & (Join-Path $Payload "tools\Resolve-BhqRuntime.ps1") -ProjectRoot $ProjectRoot
& $Python -X utf8 (Join-Path $Payload "tools\bhq_formal_database_guard.py") `
  --project-root $ProjectRoot --config (Join-Path $Payload "bhq_watcher_formal.json")
if ($LASTEXITCODE -ne 0) {
  throw "固定正式資料庫未通過離線保護檢查；未複製監看器、未建立排程。"
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$codeBackup = Join-Path $ProjectRoot "backups\bhq_watcher_deployment_code_$stamp"
$manifest = Get-ChildItem -LiteralPath $Payload -File -Recurse
foreach ($item in $manifest) {
  $relative = $item.FullName.Substring($Payload.Length).TrimStart('\')
  if ($relative -match '^(data|migrations)[\\/]' -or $item.Name -ieq "orders.db") {
    throw "部署包含禁止資料：$relative"
  }
  $target = Join-Path $ProjectRoot $relative
  if (Test-Path -LiteralPath $target -PathType Leaf) {
    $saved = Join-Path $codeBackup $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $saved) | Out-Null
    Copy-Item -LiteralPath $target -Destination $saved -Force
  }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
  Copy-Item -LiteralPath $item.FullName -Destination $target -Force
}
Write-Output "僅完成監看器程式檔離線部署。"
Write-Output "既有程式備份：$codeBackup"
Write-Output "未建立或啟用排程、未匯入BHQ、未移動INBOX、未修改orders.db。"
Write-Output "日後另經核准，才可使用 ACTIVATE_BHQ_WATCHER_FORMAL.ps1。"
'@
Set-Content -LiteralPath (Join-Path $Output "Deploy-BHQ-Watcher-Files-Only.ps1") -Encoding UTF8 -Value $installer
$bat = @'
@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Deploy-BHQ-Watcher-Files-Only.ps1"
if errorlevel 1 (
  echo.
  echo Offline deployment stopped. Nothing was activated.
  pause
  exit /b 1
)
echo.
echo Files deployed only. Watcher and scheduled task remain disabled.
pause
'@
Set-Content -LiteralPath (Join-Path $Output "離線部署程式檔_不啟用.bat") -Encoding ASCII -Value $bat

$forbidden = Get-ChildItem -LiteralPath $Output -File -Recurse | Where-Object {
  $_.FullName -match '[\\/](data|migrations)[\\/]' -or
  $_.Name -ieq "orders.db" -or
  $_.Extension -ieq ".xls" -or
  $_.Extension -ieq ".xlsx"
}
if ($forbidden) {
  throw "離線部署包含有禁止的資料庫、migration或正式資料：$($forbidden.FullName)"
}

$manifestRows = Get-ChildItem -LiteralPath $Output -File -Recurse | ForEach-Object {
  [pscustomobject]@{
    path = $_.FullName.Substring($Output.Length).TrimStart('\')
    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToUpperInvariant()
    size = $_.Length
  }
}
$manifestRows | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $Output "SHA256_MANIFEST.json") -Encoding UTF8
$zip = "$Output.zip"
Compress-Archive -LiteralPath $Output -DestinationPath $zip -CompressionLevel Optimal -Force
$zipHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToUpperInvariant()
Write-Output "PACKAGE=$zip"
Write-Output "PACKAGE_SHA256=$zipHash"
