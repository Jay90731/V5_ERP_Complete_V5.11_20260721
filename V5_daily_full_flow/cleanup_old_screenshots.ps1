param(
  [Parameter(Mandatory = $true)]
  [string]$RunsRoot,
  [Parameter(Mandatory = $true)]
  [ValidateRange(1, 3650)]
  [int]$RetentionDays,
  [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"
$cutoff = (Get-Date).AddDays(-$RetentionDays)
$deletedFiles = 0
$deletedBytes = [int64]0
$cleanedRuns = 0
$skippedRuns = 0

function Write-CleanupLog {
  param([string]$Message)
  $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
  Write-Output $line
  if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
  }
}

if (-not (Test-Path -LiteralPath $RunsRoot)) {
  Write-CleanupLog "Screenshot cleanup skipped: runs folder does not exist."
  exit 0
}

foreach ($stateFile in @(Get-ChildItem -LiteralPath $RunsRoot -Filter "run_state.json" -File -Recurse -ErrorAction SilentlyContinue)) {
  try {
    $state = Get-Content -LiteralPath $stateFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$state.status -ne "completed" -or [string]::IsNullOrWhiteSpace([string]$state.finished_at)) {
      $skippedRuns++
      continue
    }

    $finishedAt = [datetimeoffset]::Parse([string]$state.finished_at)
    if ($finishedAt.LocalDateTime -ge $cutoff) { continue }

    $runDir = Split-Path -Parent $stateFile.FullName
    $captureDirs = @(Get-ChildItem -LiteralPath $runDir -Directory -Filter "captures" -Recurse -ErrorAction SilentlyContinue)
    $runDeleted = 0
    foreach ($captureDir in $captureDirs) {
      foreach ($image in @(Get-ChildItem -LiteralPath $captureDir.FullName -File -Filter "*.png" -ErrorAction SilentlyContinue)) {
        $deletedBytes += [int64]$image.Length
        Remove-Item -LiteralPath $image.FullName -Force
        $deletedFiles++
        $runDeleted++
      }
    }
    if ($runDeleted -gt 0) {
      $cleanedRuns++
      Write-CleanupLog "Cleaned completed run $($state.run_id): deleted_png=$runDeleted finished_at=$($state.finished_at)"
    }
  } catch {
    $skippedRuns++
    Write-CleanupLog "Screenshot cleanup warning for $($stateFile.FullName): $($_.Exception.Message)"
  }
}

$freedMb = [Math]::Round($deletedBytes / 1MB, 2)
Write-CleanupLog "Screenshot cleanup complete: retention_days=$RetentionDays cleaned_runs=$cleanedRuns deleted_png=$deletedFiles freed_mb=$freedMb skipped_unfinished_or_invalid=$skippedRuns"
