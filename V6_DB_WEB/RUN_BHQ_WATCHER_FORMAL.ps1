$ErrorActionPreference = "Stop"
$ExpectedRoot = "C:\Users\auntstella\Desktop\V6_DB_WEB"
$Root = (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
if ($Root.TrimEnd('\') -ine $ExpectedRoot.TrimEnd('\')) {
  throw "正式監看器只能從固定專案執行：$ExpectedRoot；目前為 $Root"
}
$Database = "C:\Users\auntstella\Desktop\V6_DB_WEB\data\orders.db"
if (-not (Test-Path -LiteralPath (Join-Path $Root "data") -PathType Container)) {
  throw "固定 data 資料夾不存在；拒絕自動建立。"
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf) -or (Get-Item -LiteralPath $Database).Length -le 0) {
  throw "固定正式 orders.db 不存在或為 0 位元組；拒絕啟動。"
}
$Python = & (Join-Path $Root "tools\Resolve-BhqRuntime.ps1") -ProjectRoot $Root
$Config = Join-Path $Root "bhq_watcher_formal.json"
$LogFolder = "C:\Users\auntstella\BHQ\_Export\Logs"
$LogFile = Join-Path $LogFolder "bhq_watcher_service.log"

New-Item -ItemType Directory -Force -Path $LogFolder | Out-Null
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
  throw "找不到正式設定檔：$Config"
}

& $Python -X utf8 (Join-Path $Root "bhq_watch.py") `
  --config $Config --validate-database-only --enable-formal-import
if ($LASTEXITCODE -ne 0) {
  throw "固定正式資料庫驗證失敗；未啟動監看服務。"
}

Set-Location -LiteralPath $Root
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$started = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff zzz"
Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value "[$started] BHQ watcher task starting"
& $Python -X utf8 (Join-Path $Root "bhq_watch.py") `
  --config $Config --enable-formal-import *>> $LogFile
$exitCode = $LASTEXITCODE
$finished = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff zzz"
Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value "[$finished] BHQ watcher task exited code=$exitCode"
exit $exitCode
