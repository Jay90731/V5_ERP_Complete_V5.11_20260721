param(
  [Parameter(Mandatory=$true)][string]$OutputDirectory,
  [switch]$MaintenanceConfirmed
)
$ErrorActionPreference = 'Stop'
if (-not $MaintenanceConfirmed) { throw 'Plan approval and manual maintenance stop are required before capture.' }
$Kit = Split-Path -Parent $MyInvocation.MyCommand.Path
$Program = Join-Path $Kit 'V6_DB_WEB'
$Python = Join-Path $Program 'runtime\python\python.exe'
$SourceRoot = 'C:\Users\auntstella\Desktop\V6_DB_WEB_DUAL_R3_20260825_165102'
$SourceDb = Join-Path $SourceRoot 'data\orders.db'
if (-not (Test-Path -LiteralPath $SourceDb -PathType Leaf)) { throw "Existing R3 database not found: $SourceDb" }
if ((Get-Item -LiteralPath $SourceDb).Length -le 0) { throw 'R3 database is empty.' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Bundled Python runtime missing.' }
$running = @(Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -ne $PID -and $_.CommandLine -and
  ($_.CommandLine -match 'phase3_worker|phase3_launcher|run-daily-erp|run-six-months-erp|web_v1[\\/]app.py|cleanup_success_images|bhq_watch.py')
} | Select-Object ProcessId,Name,CommandLine)
if ($running.Count) { throw "ERP/web/cleanup/watcher must exit safely first: $($running | ConvertTo-Json -Compress)" }
& $Python -X utf8 (Join-Path $Program 'tools\r4_deployment_preflight.py') capture-r3 `
  --output-directory $OutputDirectory --maintenance-confirmed
if ($LASTEXITCODE -ne 0) { throw 'Read-only R3 capture failed; do not deploy from it.' }
Write-Output 'Capture only completed. No R3 SQLite writes, migration, deployment or schedule changes were performed.'
