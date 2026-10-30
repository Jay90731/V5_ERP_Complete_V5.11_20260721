param(
  [Parameter(Mandatory = $true)]
  [string]$AcceptanceFile
)

$ErrorActionPreference = "Stop"
$ExpectedRoot = "C:\Users\auntstella\Desktop\V6_DB_WEB"
$Root = (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
if ($Root.TrimEnd('\') -ine $ExpectedRoot.TrimEnd('\')) {
  throw "正式監看器只能從固定專案啟用：$ExpectedRoot；拒絕目前路徑：$Root"
}
$Database = "C:\Users\auntstella\Desktop\V6_DB_WEB\data\orders.db"
$Config = Join-Path $Root "bhq_watcher_formal.json"
$Watcher = Join-Path $Root "bhq_watch.py"
$Helper = Join-Path $Root "tools\bhq_watcher_acceptance.py"
$Base = "C:\Users\auntstella\BHQ\_Export"
$Inbox = Join-Path $Base "INBOX"
$Results = Join-Path $Base "Results"
$Logs = Join-Path $Base "Logs"
$TaskName = "BHQ Excel 自動匯入"
$ApprovedSha256 = "9F3C1CA4B567B3E57F3EBF05AE07333BFCA7DC61202B2E2CFA0440063C1977BB"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  $arguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $MyInvocation.MyCommand.Path),
    "-AcceptanceFile", ('"{0}"' -f $AcceptanceFile)
  ) -join " "
  $elevated = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -Verb RunAs -Wait -PassThru
  exit $elevated.ExitCode
}

if (-not (Get-LocalUser -Name "auntstella" -ErrorAction SilentlyContinue)) {
  throw "本機找不到 Windows 使用者 auntstella；這不是核准的目標電腦。"
}
if (-not (Test-Path -LiteralPath (Join-Path $Root "data") -PathType Container)) {
  throw "固定正式 data 資料夾不存在；拒絕自動建立。"
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
  throw "固定正式 orders.db 不存在；拒絕自動建立：$Database"
}
if ((Get-Item -LiteralPath $Database).Length -le 0) {
  throw "固定正式 orders.db 是 0 位元組，拒絕啟用。"
}
foreach ($path in @($Database, $Config, $Watcher, $Helper, $AcceptanceFile)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "缺少必要檔案：$path" }
}
$sourceHash = (Get-FileHash -LiteralPath $AcceptanceFile -Algorithm SHA256).Hash.ToUpperInvariant()
if ($sourceHash -ne $ApprovedSha256) {
  throw "驗收來源 SHA-256 不符，拒絕啟用。actual=$sourceHash expected=$ApprovedSha256"
}

$Python = & (Join-Path $Root "tools\Resolve-BhqRuntime.ps1") -ProjectRoot $Root
& $Python -X utf8 $Watcher --config $Config --validate-database-only --enable-formal-import
if ($LASTEXITCODE -ne 0) {
  throw "固定正式資料庫前置驗證失敗；未建立監看資料夾或排程。"
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask -and $existingTask.State -eq "Running") {
  throw "既有 BHQ 監看排程正在執行；請先用安全停止工具停止後再驗收。"
}
$writerPatterns = @(
  "phase3_worker", "phase3_pipeline", "phase3_launcher",
  "run-daily-erp", "run-history", "run-six-months"
)
$activeWriters = Get-CimInstance Win32_Process | Where-Object {
  $line = [string]$_.CommandLine
  $line -and ($writerPatterns | Where-Object { $line -match [regex]::Escape($_) })
} | Select-Object ProcessId, Name, CommandLine
if ($activeWriters) {
  throw "偵測到 ERP 掃描/寫入程序，正式驗收停止：$($activeWriters | ConvertTo-Json -Compress)"
}

foreach ($folder in @("INBOX", "Processing", "Completed", "Failed", "Results", "Logs")) {
  New-Item -ItemType Directory -Force -Path (Join-Path $Base $folder) | Out-Null
}

$acceptedInInbox = Get-ChildItem -LiteralPath $Inbox -Filter *.xls -File | Where-Object { -not $_.Name.StartsWith('~$') }
$acceptedInProcessing = Get-ChildItem -LiteralPath (Join-Path $Base "Processing") -Filter *.xls -File | Where-Object { -not $_.Name.StartsWith('~$') }
if ($acceptedInInbox -or $acceptedInProcessing) {
  throw "INBOX 或 Processing 已有待處理 XLS；為避免混合驗收，請先人工確認。"
}

Set-Location -LiteralPath $Root
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
& $Python -X utf8 $Watcher --config $Config --validate-only --enable-formal-import
if ($LASTEXITCODE -ne 0) { throw "正式設定或資料庫前置驗證失敗。" }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$AcceptanceDir = Join-Path $Results "FORMAL_ACTIVATION_$stamp"
New-Item -ItemType Directory -Force -Path $AcceptanceDir | Out-Null
$before = Join-Path $AcceptanceDir "01_before.json"
$afterFirst = Join-Path $AcceptanceDir "02_after_first.json"
$afterSecond = Join-Path $AcceptanceDir "03_after_second.json"
$afterRestart = Join-Path $AcceptanceDir "04_after_restart.json"
& $Python -X utf8 $Helper snapshot --db $Database --output $before | Out-Null
if ($LASTEXITCODE -ne 0) { throw "正式庫啟用前快照失敗。" }

$backupText = & $Python -X utf8 (Join-Path $Root "v6.py") backup-formal --db $Database --notes "Before BHQ watcher formal activation" | Out-String
if ($LASTEXITCODE -ne 0) { throw "正式庫備份失敗。" }
$backup = $backupText | ConvertFrom-Json
if (-not $backup.valid -or $backup.quick_check -ne "ok" -or $backup.foreign_key_errors -ne 0) {
  throw "正式備份驗證失敗：$backupText"
}

function Invoke-Drain([string]$label) {
  $output = Join-Path $AcceptanceDir "$label`_watcher.json"
  $text = & $Python -X utf8 $Watcher --config $Config --drain --timeout 240 --enable-formal-import | Out-String
  Set-Content -LiteralPath $output -Encoding UTF8 -Value $text
  if ($LASTEXITCODE -ne 0) { throw "$label 監看驗收失敗，詳見 $output" }
  return ($text | ConvertFrom-Json)
}

Copy-Item -LiteralPath $AcceptanceFile -Destination (Join-Path $Inbox (Split-Path -Leaf $AcceptanceFile))
$first = Invoke-Drain "first"
$firstOutcome = @($first)[-1]
if ($firstOutcome.status -ne "completed") { throw "首次驗收狀態不是 completed：$($firstOutcome.status)" }
if ($firstOutcome.sha256 -ne $ApprovedSha256) { throw "首次驗收處理的檔案雜湊不符。" }
$dry = $firstOutcome.dry_run
if ($dry.source_row_count -ne 903 -or $dry.order_count -ne 257 -or $dry.valid_item_count -ne 551 -or $dry.empty_product_code_count -ne 352 -or $dry.store_count -ne 18 -or $dry.anomaly_count -ne 0) {
  throw "首次 dry-run 基準不符：$($dry | ConvertTo-Json -Compress)"
}
$stats = $firstOutcome.import
if ($stats.inserted_orders -ne 0 -or $stats.inserted_versions -ne 0 -or $stats.inserted_items -ne 0 -or $stats.inserted_stores -ne 0 -or $stats.inserted_products -ne 0 -or $stats.skipped_orders -ne 257) {
  throw "已匯入七月檔案不是核心零增量：$($stats | ConvertTo-Json -Compress)"
}
& $Python -X utf8 $Helper snapshot --db $Database --output $afterFirst | Out-Null
$firstCompare = Join-Path $AcceptanceDir "first_compare.json"
& $Python -X utf8 $Helper compare --before $before --after $afterFirst --output $firstCompare
if ($LASTEXITCODE -ne 0) { throw "首次核心/ERP保護驗證失敗。" }

Copy-Item -LiteralPath $AcceptanceFile -Destination (Join-Path $Inbox (Split-Path -Leaf $AcceptanceFile))
$second = Invoke-Drain "second"
$secondOutcome = @($second)[-1]
if ($secondOutcome.status -ne "already_completed") { throw "第二次同檔未直接判定 already_completed。" }
& $Python -X utf8 $Helper snapshot --db $Database --output $afterSecond | Out-Null
$secondCompare = Join-Path $AcceptanceDir "second_compare.json"
& $Python -X utf8 $Helper compare --before $afterFirst --after $afterSecond --output $secondCompare --strict
if ($LASTEXITCODE -ne 0) { throw "第二次同檔仍增加資料庫紀錄。" }

Copy-Item -LiteralPath $AcceptanceFile -Destination (Join-Path $Inbox (Split-Path -Leaf $AcceptanceFile))
$restart = Invoke-Drain "restart"
$restartOutcome = @($restart)[-1]
if ($restartOutcome.status -ne "already_completed") { throw "重啟模擬未直接判定 already_completed。" }
& $Python -X utf8 $Helper snapshot --db $Database --output $afterRestart | Out-Null
$restartCompare = Join-Path $AcceptanceDir "restart_compare.json"
& $Python -X utf8 $Helper compare --before $afterSecond --after $afterRestart --output $restartCompare --strict
if ($LASTEXITCODE -ne 0) { throw "服務重啟後仍增加資料庫紀錄。" }

$ready = Join-Path $AcceptanceDir "erp_lock_ready.json"
$holdOut = Join-Path $AcceptanceDir "erp_lock_holder.log"
$holdErr = Join-Path $AcceptanceDir "erp_lock_holder.err.log"
$holder = Start-Process -FilePath $Python -ArgumentList @(
  "-X", "utf8", $Helper, "hold-lock", "--db", $Database,
  "--seconds", "8", "--ready", $ready
) -WindowStyle Hidden -PassThru -RedirectStandardOutput $holdOut -RedirectStandardError $holdErr
$lockDeadline = (Get-Date).AddSeconds(15)
while (-not (Test-Path -LiteralPath $ready) -and (Get-Date) -lt $lockDeadline) { Start-Sleep -Milliseconds 200 }
if (-not (Test-Path -LiteralPath $ready)) { throw "模擬 ERP 未能取得共用寫入鎖。" }
$lockResult = Join-Path $AcceptanceDir "erp_lock_wait.json"
& $Python -X utf8 $Helper wait-lock --db $Database --timeout 30 --output $lockResult
if ($LASTEXITCODE -ne 0) { throw "BHQ 等待 ERP 共用鎖驗證失敗。" }
$holder.WaitForExit()
if ($holder.ExitCode -ne 0) { throw "模擬 ERP 鎖程序失敗，詳見 $holdErr" }

& (Join-Path $Root "INSTALL_BHQ_WATCHER_FORMAL_TASK.ps1") -EnableAfterInstall
$serviceDeadline = (Get-Date).AddSeconds(45)
$serviceRunning = $false
do {
  Start-Sleep -Seconds 1
  $statusPath = Join-Path $Logs "bhq_watcher_status.json"
  if (Test-Path -LiteralPath $statusPath) {
    try {
      $serviceStatus = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
      $serviceRunning = ($serviceStatus.status -eq "running" -and [int]$serviceStatus.pid -gt 0)
    } catch {}
  }
} until ($serviceRunning -or (Get-Date) -ge $serviceDeadline)
if (-not $serviceRunning) { throw "排程已建立，但正式 BHQ 監看程序未在 45 秒內回報 running。" }
$task = Get-ScheduledTask -TaskName $TaskName
$taskInfo = $task | Get-ScheduledTaskInfo
$taskReport = [ordered]@{
  name = $TaskName
  state = [string]$task.State
  trigger_user = [string]$task.Triggers.UserId
  execute = [string]$task.Actions.Execute
  arguments = [string]$task.Actions.Arguments
  working_directory = [string]$task.Actions.WorkingDirectory
  multiple_instances = [string]$task.Settings.MultipleInstances
  restart_count = $task.Settings.RestartCount
  next_run_time = $taskInfo.NextRunTime
  watcher_status = $serviceStatus
}
if ($taskReport.trigger_user -notmatch "auntstella" -or $taskReport.working_directory -ne $Root -or $taskReport.multiple_instances -ne "IgnoreNew") {
  throw "Windows 排程設定驗證失敗：$($taskReport | ConvertTo-Json -Compress)"
}

$finalSnapshot = Join-Path $AcceptanceDir "05_final.json"
& $Python -X utf8 $Helper snapshot --db $Database --output $finalSnapshot | Out-Null
$final = Get-Content -LiteralPath $finalSnapshot -Raw | ConvertFrom-Json
$firstCheck = Get-Content -LiteralPath $firstCompare -Raw | ConvertFrom-Json
$secondCheck = Get-Content -LiteralPath $secondCompare -Raw | ConvertFrom-Json
$restartCheck = Get-Content -LiteralPath $restartCompare -Raw | ConvertFrom-Json
$lockCheck = Get-Content -LiteralPath $lockResult -Raw | ConvertFrom-Json
$report = [ordered]@{
  status = "passed"
  completed_at = (Get-Date).ToString("o")
  project_root = $Root
  formal_config = (Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json)
  source_file = $AcceptanceFile
  source_sha256 = $sourceHash
  pre_activation_backup = $backup
  first_zero_delta = $firstCheck
  first_import = $firstOutcome
  second_already_completed = $secondOutcome
  second_no_database_change = $secondCheck
  restart_already_completed = $restartOutcome
  restart_no_database_change = $restartCheck
  erp_lock_wait = $lockCheck
  scheduled_task = $taskReport
  final_database = $final
  report_folder = $AcceptanceDir
}
$reportJson = Join-Path $AcceptanceDir "BHQ_WATCHER_FORMAL_ACTIVATION_REPORT.json"
$report | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $reportJson -Encoding UTF8
$reportMd = Join-Path $AcceptanceDir "BHQ_WATCHER_FORMAL_ACTIVATION_REPORT.md"
$md = @"
# BHQ Excel 自動正式匯入啟用驗證報告

- 結果：通過
- 完成時間：$($report.completed_at)
- 正式設定：$Config
- 監看資料夾：$Inbox
- 正式資料庫：$Database
- 核准來源 SHA-256：$sourceHash
- 啟用前備份：$($backup.backup)
- 備份 SHA-256：$($backup.sha256)
- 首次匯入：核心 orders / versions / items / stores / products 增量皆 0，257 張跳過
- 第二次同檔：already_completed，資料庫及稽核紀錄增量 0
- 服務重啟模擬：already_completed，資料庫增量 0
- ERP 持鎖模擬：先等待=$($lockCheck.deferred_observed)，釋放後取得=$($lockCheck.eventually_acquired)
- 排程：$TaskName；登入 auntstella 觸發；IgnoreNew；工作目錄 $Root
- integrity_check：$($final.integrity_check)
- foreign_key_check：$($final.foreign_key_errors)

## 回復方式

1. 執行「停止BHQ自動匯入.bat」，等待狀態顯示 stopped。
2. 停用排程「$TaskName」。
3. 只有在確認需要回復資料庫時，使用既有正式回復工具與上述備份：$($backup.backup)。
4. 回復前須再次備份目前正式庫，且 ERP/BHQ 均不得持有共用寫入鎖。
"@
Set-Content -LiteralPath $reportMd -Encoding UTF8 -Value $md
Write-Output "BHQ 正式監看服務已在目標機完成驗收並啟用。"
Write-Output "JSON_REPORT=$reportJson"
Write-Output "MARKDOWN_REPORT=$reportMd"
