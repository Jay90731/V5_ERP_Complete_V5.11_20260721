param(
  [ValidateSet("Start", "Stop", "Status", "Open", "Retry")]
  [Parameter(Mandatory = $true)]
  [string]$Action
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base = "C:\Users\auntstella\BHQ\_Export"
$Logs = Join-Path $Base "Logs"
$StopFile = Join-Path $Logs "bhq_watcher.stop"
$TaskName = "BHQ Excel 自動匯入"

switch ($Action) {
  "Start" {
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    Remove-Item -LiteralPath $StopFile -Force -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "已要求啟動 $TaskName。"
  }
  "Stop" {
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    Set-Content -LiteralPath $StopFile -Encoding ASCII -Value "graceful-stop-requested"
    Write-Output "已送出安全停止要求；目前 transaction 會完成後才停止。"
    Write-Output "本指令不會停止 ERP、網站或其他 Python 程序。"
  }
  "Status" {
    $Python = & (Join-Path $Root "tools\Resolve-BhqRuntime.ps1") -ProjectRoot $Root
    Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Format-List *
    & $Python -X utf8 (Join-Path $Root "bhq_watch.py") --config (Join-Path $Root "bhq_watcher_formal.json") --status --enable-formal-import
    $latest = Get-ChildItem -LiteralPath (Join-Path $Base "Results") -Filter *.txt -File -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($latest) { Write-Output "最新結果：$($latest.FullName)" }
  }
  "Open" {
    Start-Process -FilePath "explorer.exe" -ArgumentList @($Base)
  }
  "Retry" {
    & $MyInvocation.MyCommand.Path -Action Stop
    $deadline = (Get-Date).AddMinutes(3)
    do {
      Start-Sleep -Seconds 2
      $statusPath = Join-Path $Logs "bhq_watcher_status.json"
      $stopped = $false
      if (Test-Path -LiteralPath $statusPath) {
        try { $stopped = ((Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json).status -eq "stopped") } catch {}
      }
      $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
      if (-not $task -or $task.State -ne "Running") { $stopped = $true }
    } until ($stopped -or (Get-Date) -ge $deadline)
    if (-not $stopped) { throw "監看服務尚未安全停止；未重排失敗檔案。" }
    $Python = & (Join-Path $Root "tools\Resolve-BhqRuntime.ps1") -ProjectRoot $Root
    & $Python -X utf8 (Join-Path $Root "bhq_watch.py") --config (Join-Path $Root "bhq_watcher_formal.json") --retry-failed --enable-formal-import
    if ($LASTEXITCODE -ne 0) { throw "重排失敗檔案失敗。" }
    Write-Output "失敗檔案已人工排回 INBOX；請再執行啟動指令。"
  }
}
