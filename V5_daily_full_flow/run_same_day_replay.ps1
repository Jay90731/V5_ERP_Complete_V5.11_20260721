$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Get-Content -LiteralPath (Join-Path $Root "v5_config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$source = [string]$Config.sameDayReplayInput
if (-not [System.IO.Path]::IsPathRooted($source)) { $source = [System.IO.Path]::GetFullPath((Join-Path $Root $source)) }
& (Join-Path $Root "v5_worker.ps1") -ReplayInputJson $source -ExpectUnchanged
$workerExit = $LASTEXITCODE
if ($workerExit -ne 0) { exit $workerExit }

$runDir = (Get-Content -LiteralPath (Join-Path $Root "latest_run_path.txt") -Raw -Encoding UTF8).Trim()
$state = Get-Content -LiteralPath (Join-Path $runDir "run_state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$stateDir = [string]$Config.officialStateDir
if (-not [System.IO.Path]::IsPathRooted($stateDir)) { $stateDir = [System.IO.Path]::GetFullPath((Join-Path $Root $stateDir)) }
$backupDir = [string]$state.official_backup_dir
if ([string]::IsNullOrWhiteSpace($backupDir)) {
  $backupDir = Get-ChildItem -LiteralPath (Join-Path $stateDir "backups") -Directory |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName
}
$python = ""
foreach ($candidate in @($Config.pythonCandidates)) {
  $resolved = [string]$candidate
  if (-not [System.IO.Path]::IsPathRooted($resolved)) { $resolved = [System.IO.Path]::GetFullPath((Join-Path $Root $resolved)) }
  if (Test-Path -LiteralPath $resolved) { $python = $resolved; break }
}
if ([string]::IsNullOrWhiteSpace($python)) { throw "Python was not found for replay verification." }
& $python (Join-Path $Root "verify_same_day_replay.py") --state-dir $stateDir --run-dir $runDir --backup-dir $backupDir
exit $LASTEXITCODE
