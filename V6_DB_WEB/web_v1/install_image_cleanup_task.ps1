param([string]$RunAt = "20:30")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Batch = Join-Path $Root "RUN_7_DAY_IMAGE_CLEANUP.bat"
if (-not (Test-Path -LiteralPath $Batch -PathType Leaf)) {
  throw "找不到圖片清理程式：$Batch"
}
$time = [datetime]::ParseExact($RunAt, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Batch`""
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask `
  -TaskName "Wedding Orders 7 Day Image Cleanup" `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Delete successfully recognized wedding-order screenshots after 7 days; pending review images are protected." `
  -Force | Out-Null
Write-Output "TASK_INSTALLED name=Wedding Orders 7 Day Image Cleanup run_at=$RunAt"

