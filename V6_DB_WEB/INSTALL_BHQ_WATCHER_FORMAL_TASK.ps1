param(
  [switch]$EnableAfterInstall
)

$ErrorActionPreference = "Stop"
$ExpectedRoot = "C:\Users\auntstella\Desktop\V6_DB_WEB"
$Root = (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
if ($Root.TrimEnd('\') -ine $ExpectedRoot.TrimEnd('\')) {
  throw "拒絕建立排程：正式專案必須是 $ExpectedRoot；目前為 $Root"
}
$Database = "C:\Users\auntstella\Desktop\V6_DB_WEB\data\orders.db"
if (-not (Test-Path -LiteralPath (Join-Path $Root "data") -PathType Container)) {
  throw "拒絕建立排程：固定 data 資料夾不存在，且不會自動建立。"
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf) -or (Get-Item -LiteralPath $Database).Length -le 0) {
  throw "拒絕建立排程：固定正式 orders.db 不存在或為 0 位元組。"
}
$Config = Join-Path $Root "bhq_watcher_formal.json"
$Runner = Join-Path $Root "RUN_BHQ_WATCHER_FORMAL.ps1"
$Python = & (Join-Path $Root "tools\Resolve-BhqRuntime.ps1") -ProjectRoot $Root
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
  throw "找不到 bhq_watcher_formal.json；請先複製 example、填入本機 BHQ 匯出資料夾並完成核准。"
}
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
  throw "找不到正式監看啟動程式：$Runner"
}

if (-not (Get-LocalUser -Name "auntstella" -ErrorAction SilentlyContinue)) {
  throw "本機找不到 Windows 使用者 auntstella，拒絕建立正式排程。"
}

& $Python -X utf8 (Join-Path $Root "bhq_watch.py") `
  --config $Config --validate-database-only --enable-formal-import
if ($LASTEXITCODE -ne 0) {
  throw "固定正式資料庫未通過前置驗證，未建立排程。"
}

& $Python -X utf8 (Join-Path $Root "bhq_watch.py") `
  --config $Config --validate-only --enable-formal-import
if ($LASTEXITCODE -ne 0) {
  throw "正式 BHQ 監看設定未通過安全檢查，未安裝排程。"
}

$TaskName = "BHQ Excel 自動匯入"
$Action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" `
  -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "auntstella"
$Principal = New-ScheduledTaskPrincipal `
  -UserId "SYSTEM" `
  -LogonType ServiceAccount `
  -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew `
  -RestartCount 5 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Principal $Principal `
  -Description "Watch a local BHQ .xls export folder and import into the shared formal orders.db under the common database write lock." `
  -Force | Out-Null

if ($EnableAfterInstall) {
  Enable-ScheduledTask -TaskName $TaskName | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  Write-Output "TASK_INSTALLED_AND_STARTED name=$TaskName trigger=auntstella_logon run_as=SYSTEM"
} else {
  Disable-ScheduledTask -TaskName $TaskName | Out-Null
  Write-Output "TASK_INSTALLED_DISABLED name=$TaskName trigger=auntstella_logon run_as=SYSTEM"
}
