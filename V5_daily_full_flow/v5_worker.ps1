param(
  [switch]$ResumeLatest,
  [string]$ReplayInputJson = "",
  [switch]$ExpectUnchanged,
  [switch]$ForceFullSixMonths,
  [switch]$ForceDailyIncremental
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Get-Content -LiteralPath (Join-Path $Root "v5_config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$Labels = Get-Content -LiteralPath (Join-Path $Root "v5_labels.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$RunsRoot = Join-Path $Root "runs"
$StopFlagPath = Join-Path $Root "STOP_V5_NOW.flag"
$LoginFlagPath = Join-Path $Root "LOGIN_CONTINUE.flag"
$LatestRunPathFile = Join-Path $Root "latest_run_path.txt"
$StageKeys = @("erp_prepare", "capture", "ocr", "validate", "compare", "excel", "complete")
$RunState = $null
$RunDir = ""
$StatePath = ""
$LogPath = ""
$ExitCode = 0
$Mutex = $null
$MutexOwned = $false

if ($ForceFullSixMonths -and $ForceDailyIncremental) {
  throw "ForceFullSixMonths and ForceDailyIncremental cannot be used together."
}
$today = Get-Date
$script:RequestedQueryMode = if ($ForceFullSixMonths) {
  "monthly_six_months"
} elseif ($ForceDailyIncremental) {
  "daily_incremental"
} elseif ($today.Day -eq [int]$Config.monthlyFullRunDay) {
  "monthly_six_months"
} else {
  "daily_incremental"
}

function Resolve-RootPath {
  param([string]$PathValue)
  if ([string]::IsNullOrWhiteSpace($PathValue)) { return "" }
  if ([System.IO.Path]::IsPathRooted($PathValue)) {
    return [System.IO.Path]::GetFullPath($PathValue)
  }
  return [System.IO.Path]::GetFullPath((Join-Path $Root $PathValue))
}

function Resolve-FirstExisting {
  param([object[]]$Candidates, [string]$Name)
  foreach ($candidate in @($Candidates)) {
    $resolved = Resolve-RootPath -PathValue ([string]$candidate)
    if ($resolved -and (Test-Path -LiteralPath $resolved)) { return $resolved }
  }
  throw "$Name was not found. Run CHECK_V5_ENVIRONMENT.bat after configuring v5_config.json."
}

function Write-RunLog {
  param([string]$Message)
  if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
    Add-Content -LiteralPath $LogPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
  }
}

function Save-RunState {
  $RunState.updated_at = (Get-Date).ToString("o")
  $temporary = "$StatePath.tmp"
  $RunState | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $temporary -Encoding UTF8
  Move-Item -LiteralPath $temporary -Destination $StatePath -Force
  Set-Content -LiteralPath $LatestRunPathFile -Value $RunDir -Encoding UTF8
}

function Get-Stage {
  param([string]$Key)
  return @($RunState.stages | Where-Object { $_.key -eq $Key })[0]
}

function Set-Stage {
  param(
    [string]$Key,
    [string]$Status,
    [string]$Message,
    [int]$StageProgress
  )
  $stage = Get-Stage -Key $Key
  if ($null -eq $stage) { throw "Unknown V5 stage: $Key" }
  if ($Status -eq "running" -and [string]::IsNullOrWhiteSpace([string]$stage.started_at)) {
    $stage.started_at = (Get-Date).ToString("o")
  }
  if ($Status -in @("completed", "failed", "stopped", "skipped")) {
    $stage.finished_at = (Get-Date).ToString("o")
  }
  $stage.status = $Status
  $stage.progress = [Math]::Min(100, [Math]::Max(0, $StageProgress))
  $stage.message = $Message
  $RunState.current_stage = $Key
  $RunState.message = $Message
  $index = [Array]::IndexOf($StageKeys, $Key)
  $RunState.progress = [Math]::Min(100, [Math]::Round((($index + ($stage.progress / 100.0)) / $StageKeys.Count) * 100))
  Save-RunState
}

function Complete-Stage {
  param([string]$Key, [string]$Message)
  Set-Stage -Key $Key -Status "completed" -Message $Message -StageProgress 100
}

function Skip-Stage {
  param([string]$Key, [string]$Message)
  Set-Stage -Key $Key -Status "skipped" -Message $Message -StageProgress 100
}

function Test-StopRequested {
  return (Test-Path -LiteralPath $StopFlagPath)
}

function Assert-Continue {
  param([string]$SafePoint)
  if (Test-StopRequested) {
    throw "V5_STOPPED: Stop requested at safe point: $SafePoint"
  }
}

function Read-KeyValueFile {
  param([string]$Path)
  $result = @{}
  foreach ($line in @(Get-Content -LiteralPath $Path -Encoding UTF8)) {
    $parts = $line -split "=", 2
    if ($parts.Count -eq 2) { $result[$parts[0]] = $parts[1] }
  }
  return $result
}

function Invoke-PythonChecked {
  param([string[]]$Arguments, [string]$Name)
  $output = & $script:Python @Arguments 2>&1
  $output | ForEach-Object { Write-RunLog "$Name $_" }
  if ($LASTEXITCODE -ne 0) {
    $detail = ($output | Out-String).Trim()
    throw "$Name failed with exit code $LASTEXITCODE. $detail"
  }
}

function Get-ProcessExitCodeSafely {
  param([System.Diagnostics.Process]$Process)
  try {
    $Process.WaitForExit()
    $Process.Refresh()
    if ($Process.HasExited) { return [nullable[int]]([int]$Process.ExitCode) }
  } catch {
    Write-RunLog "Process exit-code warning: $($_.Exception.Message)"
  }
  return $null
}

function Get-LatestCaptureBatch {
  param([string]$BatchesRoot)
  return Get-ChildItem -LiteralPath $BatchesRoot -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
}

function New-StageList {
  $items = @()
  foreach ($key in $StageKeys) {
    $items += [pscustomobject][ordered]@{
      key = $key
      label = [string]$Labels.stages.$key
      status = "pending"
      progress = 0
      message = ""
      started_at = ""
      finished_at = ""
    }
  }
  return $items
}

function Initialize-NewRun {
  $now = Get-Date
  $dateFolder = $now.ToString("yyyy-MM-dd")
  $runId = "run_$($now.ToString('yyyyMMdd_HHmmss_fff'))"
  $script:RunDir = Join-Path (Join-Path $RunsRoot $dateFolder) $runId
  New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
  $script:StatePath = Join-Path $RunDir "run_state.json"
  $script:LogPath = Join-Path $RunDir "v5_run.log"
  $mode = if ([string]::IsNullOrWhiteSpace($ReplayInputJson)) { "daily" } else { "same_day_replay" }
  $queryMode = if ($mode -eq "same_day_replay") { "same_day_replay" } else { $script:RequestedQueryMode }
  $queryStart = if ($queryMode -eq "monthly_six_months") {
    $now.AddMonths(-6).ToString("yyyy/MM/dd")
  } else {
    $now.AddDays(-[int]$Config.dailyLookbackDays).ToString("yyyy/MM/dd")
  }
  $script:RunState = [pscustomobject][ordered]@{
    version = "V5.11"
    run_id = $runId
    run_mode = $mode
    query_mode = $queryMode
    status = "running"
    started_at = $now.ToString("o")
    updated_at = $now.ToString("o")
    finished_at = ""
    current_stage = "erp_prepare"
    progress = 0
    message = ""
    run_dir = $RunDir
    replay_input = $ReplayInputJson
    query_start = $queryStart
    query_end = $now.ToString("yyyy/MM/dd")
    screenshot_count = 0
    capture_batch = ""
    excel_path = ""
    official_backup_dir = ""
    summary_path = ""
    log_path = $LogPath
    stages = New-StageList
  }
  Save-RunState
}

function Initialize-ResumeRun {
  $unfinished = @()
  foreach ($file in @(Get-ChildItem -LiteralPath $RunsRoot -Filter "run_state.json" -File -Recurse -ErrorAction SilentlyContinue)) {
    try {
      $state = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($state.status -eq "completed" -or $state.run_mode -ne "daily") { continue }
      $candidateRunDir = Split-Path -Parent $file.FullName
      $hasCompletedCapture = $false
      foreach ($summaryFile in @(Get-ChildItem -LiteralPath (Join-Path $candidateRunDir "erp_capture\batches") -Filter "summary.txt" -File -Recurse -ErrorAction SilentlyContinue)) {
        if (Select-String -LiteralPath $summaryFile.FullName -Pattern "^status=completed$" -Quiet -ErrorAction SilentlyContinue) {
          $hasCompletedCapture = $true
          break
        }
      }
      $unfinished += [pscustomobject]@{
        File = $file
        HasCompletedCapture = $hasCompletedCapture
      }
    } catch { }
  }
  $candidateInfo = $unfinished |
    Sort-Object -Property @{ Expression = { $_.HasCompletedCapture }; Descending = $true }, @{ Expression = { $_.File.LastWriteTime }; Descending = $true } |
    Select-Object -First 1
  $candidate = if ($candidateInfo) { $candidateInfo.File } else { $null }
  if ($null -eq $candidate) { throw "No unfinished V5 run was found." }
  $script:RunDir = Split-Path -Parent $candidate.FullName
  $script:StatePath = $candidate.FullName
  $script:LogPath = Join-Path $RunDir "v5_run.log"
  $script:RunState = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($RunState.PSObject.Properties.Name -notcontains "capture_batch") {
    $RunState | Add-Member -NotePropertyName "capture_batch" -NotePropertyValue ""
  }
  if ($RunState.PSObject.Properties.Name -notcontains "query_mode") {
    $RunState | Add-Member -NotePropertyName "query_mode" -NotePropertyValue "monthly_six_months"
  }
  $RunState.status = "running"
  $RunState.message = "Resuming unfinished V5 run"
  foreach ($stage in $RunState.stages) {
    if ($stage.status -in @("running", "failed", "stopped")) {
      $stage.status = "pending"
      $stage.progress = 0
      $stage.finished_at = ""
    }
  }
  Save-RunState
}

try {
  New-Item -ItemType Directory -Force -Path $RunsRoot | Out-Null
  $createdNew = $false
  $Mutex = New-Object System.Threading.Mutex($true, "Local\WeddingOrderV5DailyFullFlow", [ref]$createdNew)
  if (-not $createdNew) { throw "Another V5 daily flow is already running." }
  $MutexOwned = $true

  $retentionDays = if ($Config.PSObject.Properties.Name -contains "screenshotRetentionDays") {
    [int]$Config.screenshotRetentionDays
  } else {
    7
  }
  $cleanupScript = Join-Path $Root "cleanup_old_screenshots.ps1"
  $cleanupLog = Join-Path $Root "screenshot_cleanup.log"
  try {
    if (Test-Path -LiteralPath $cleanupScript) {
      & $cleanupScript -RunsRoot $RunsRoot -RetentionDays $retentionDays -LogPath $cleanupLog | Out-Null
    }
  } catch {
    Add-Content -LiteralPath $cleanupLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Screenshot cleanup warning: $($_.Exception.Message)" -Encoding UTF8
  }

  Remove-Item -LiteralPath $StopFlagPath, $LoginFlagPath -Force -ErrorAction SilentlyContinue
  if ($ResumeLatest) { Initialize-ResumeRun } else { Initialize-NewRun }
  Write-RunLog "V5 started mode=$($RunState.run_mode) resume=$ResumeLatest"

  $script:Python = Resolve-FirstExisting -Candidates $Config.pythonCandidates -Name "Python"
  $OfficialStateDir = Resolve-RootPath -PathValue ([string]$Config.officialStateDir)
  $OfficialHistory = Join-Path $OfficialStateDir "order_history.json"
  $OfficialExcel = Join-Path $OfficialStateDir "current_orders.xlsx"
  $OfficialCurrentOrders = Join-Path $OfficialStateDir "current_orders.json"
  $PendingReviewJson = Join-Path $OfficialStateDir "pending_review.json"
  if (-not (Test-Path -LiteralPath $OfficialHistory)) { throw "Official order_history.json is missing: $OfficialHistory" }
  $officialHistoryPayload = Get-Content -LiteralPath $OfficialHistory -Raw -Encoding UTF8 | ConvertFrom-Json
  $OfficialHistoryCount = @($officialHistoryPayload.orders.PSObject.Properties).Count

  $IncrementalDir = Join-Path $Root "components\incremental"
  $Extractor = Join-Path $Root "components\ocr\extract_orders.py"
  $CaptureScript = Join-Path $Root "components\erp_capture\capture_erp_v4_full_batch.ps1"
  $CompareScript = Join-Path $Root "v5_compare_preview.py"
  $ValidateScript = Join-Path $Root "v5_validate_ocr.py"
  $SummaryScript = Join-Path $Root "v5_finalize_summary.py"
  $IncrementalEngine = Join-Path $IncrementalDir "incremental_engine.py"
  $ExcelBuilder = Join-Path $IncrementalDir "build_current_orders_excel.py"
  $PendingReviewScript = Join-Path $Root "components\pending_review.py"
  Write-RunLog "Excel builder: Python openpyxl ($ExcelBuilder)"

  Invoke-PythonChecked -Arguments @(
    $PendingReviewScript,
    "sync",
    "--excel", $OfficialExcel,
    "--review-json", $PendingReviewJson,
    "--current-orders", $OfficialCurrentOrders
  ) -Name "pending_review_sync"
  $Tesseract = Resolve-FirstExisting -Candidates $Config.tesseractCandidates -Name "Tesseract"
  $Tessdata = Resolve-FirstExisting -Candidates $Config.tessdataCandidates -Name "Tesseract language data"

  $CaptureRoot = Join-Path $RunDir "erp_capture"
  $CaptureBatches = Join-Path $CaptureRoot "batches"
  $OcrRoot = Join-Path $RunDir "ocr"
  $OcrInput = Join-Path $OcrRoot "input_images"
  $OcrData = Join-Path $OcrRoot "data\orders.json"
  $ValidationPath = Join-Path $RunDir "validation.json"
  $ComparisonPath = Join-Path $RunDir "comparison_preview.json"
  $PreviewPath = Join-Path $RunDir "current_orders_preview.png"
  New-Item -ItemType Directory -Force -Path $CaptureBatches, $OcrInput, (Split-Path -Parent $OcrData) | Out-Null

  if ($RunState.run_mode -eq "same_day_replay") {
    $replaySource = Resolve-RootPath -PathValue ([string]$RunState.replay_input)
    if (-not (Test-Path -LiteralPath $replaySource)) { throw "Replay input was not found: $replaySource" }
    Skip-Stage -Key "erp_prepare" -Message "Replay test uses existing completed ERP data"
    Skip-Stage -Key "capture" -Message "Replay test skips ERP screenshot capture"
    Copy-Item -LiteralPath $replaySource -Destination $OcrData -Force
    Skip-Stage -Key "ocr" -Message "Replay test uses existing completed OCR data"
  } else {
    $latestCapture = Get-LatestCaptureBatch -BatchesRoot $CaptureBatches
    $captureAlreadyComplete = $false
    if ($latestCapture -and (Test-Path -LiteralPath (Join-Path $latestCapture.FullName "summary.txt"))) {
      $existingSummary = Read-KeyValueFile -Path (Join-Path $latestCapture.FullName "summary.txt")
      $captureAlreadyComplete = $existingSummary.status -eq "completed"
    }

    if (-not $captureAlreadyComplete) {
      Set-Stage -Key "erp_prepare" -Status "running" -Message "Open ERP, log in manually, then click Continue. mode=$($RunState.query_mode)" -StageProgress 10
      $captureStdout = Join-Path $RunDir "capture_stdout.log"
      $captureStderr = Join-Path $RunDir "capture_stderr.log"
      $captureArgs = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $CaptureScript,
        "-ErpExePath", [string]$Config.erpExePath,
        "-BatchesRootOverride", $CaptureBatches,
        "-StopFlagPathOverride", $StopFlagPath,
        "-LoginContinueFlagPath", $LoginFlagPath,
        "-MinimumWaitSeconds", [string]$Config.minimumWaitSeconds,
        "-MaximumWaitSeconds", [string]$Config.maximumWaitSeconds,
        "-MaximumOrders", [string]$Config.maximumOrders
      )
      if ($ResumeLatest -and $latestCapture) {
        $captureArgs += "-ResumeLatest"
      } elseif ($RunState.query_mode -eq "monthly_six_months") {
        $captureArgs += "-FullSixMonths"
      } else {
        $captureArgs += @("-ShortRangeDays", [string]$Config.dailyLookbackDays)
      }
      $captureProcess = Start-Process -FilePath "powershell.exe" -ArgumentList $captureArgs -PassThru -WindowStyle Hidden -RedirectStandardOutput $captureStdout -RedirectStandardError $captureStderr
      $lastCount = -1
      while (-not $captureProcess.HasExited) {
        $runLog = Get-ChildItem -LiteralPath $CaptureBatches -Filter "v4_run_*.log" -File -Recurse -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
        $captured = 0
        if ($runLog) {
          try {
            $captured = @(Select-String -LiteralPath $runLog.FullName -Pattern "CAPTURED:" -SimpleMatch -ErrorAction SilentlyContinue).Count
          } catch {
            $captured = [Math]::Max(0, $lastCount)
          }
        }
        if ($captured -gt 0 -and $captured -ne $lastCount) {
          if ((Get-Stage -Key "erp_prepare").status -ne "completed") {
            Complete-Stage -Key "erp_prepare" -Message "ERP ready and order query loaded"
          }
          $percent = [Math]::Min(95, [Math]::Max(1, [Math]::Round(($captured / [double]$Config.maximumOrders) * 100)))
          Set-Stage -Key "capture" -Status "running" -Message "Captured $captured orders" -StageProgress $percent
          $lastCount = $captured
        }
        Start-Sleep -Milliseconds 750
        $captureProcess.Refresh()
      }
      $captureExitCode = Get-ProcessExitCodeSafely -Process $captureProcess
      if ($null -ne $captureExitCode -and $captureExitCode -ne 0) {
        $stderrText = if (Test-Path $captureStderr) { (Get-Content -LiteralPath $captureStderr -Raw -ErrorAction SilentlyContinue) } else { "" }
        throw "ERP capture process failed with exit code $captureExitCode. $stderrText"
      }
      if ($null -eq $captureExitCode) {
        Write-RunLog "ERP capture exit code was unavailable; validating the capture summary and image count instead."
      }
    }

    $latestCapture = Get-LatestCaptureBatch -BatchesRoot $CaptureBatches
    if ($null -eq $latestCapture) { throw "V4 did not create a capture batch." }
    $captureSummaryPath = Join-Path $latestCapture.FullName "summary.txt"
    if (-not (Test-Path -LiteralPath $captureSummaryPath)) { throw "V4 capture summary is missing." }
    $captureSummary = Read-KeyValueFile -Path $captureSummaryPath
    if ($captureSummary.status -eq "stopped_by_user") { throw "V5_STOPPED: V4 capture stopped safely." }
    if ($captureSummary.status -ne "completed") { throw "V4 capture did not complete: $($captureSummary.status) $($captureSummary.stop_reason)" }
    $captureDir = Join-Path $latestCapture.FullName "captures"
    $screenshotCount = @(Get-ChildItem -LiteralPath $captureDir -File -Filter "GOU*.png").Count
    if ($screenshotCount -ne [int]$captureSummary.success_count) { throw "Screenshot count does not match V4 success count." }
    if ($RunState.query_mode -eq "monthly_six_months" -and $OfficialHistoryCount -ge 100) {
      $minimumExpected = [Math]::Floor($OfficialHistoryCount * 0.70)
      if ($screenshotCount -lt $minimumExpected) {
        throw "Capture completeness guard failed: captured=$screenshotCount minimum_expected=$minimumExpected history=$OfficialHistoryCount. The ERP list did not start from the first queried order."
      }
    }
    $RunState.screenshot_count = $screenshotCount
    $RunState.query_start = [string]$captureSummary.date_start
    $RunState.query_end = [string]$captureSummary.date_end
    $RunState.capture_batch = $latestCapture.FullName
    Complete-Stage -Key "erp_prepare" -Message "ERP ready"
    Complete-Stage -Key "capture" -Message "Captured $screenshotCount unique orders"
    Assert-Continue -SafePoint "after capture"

    $robocopyOutput = & robocopy $captureDir $OcrInput "*.png" /R:1 /W:1 /NFL /NDL /NJH /NJS
    if ($LASTEXITCODE -gt 7) { throw "Could not copy screenshots into the V5 OCR batch. Robocopy code: $LASTEXITCODE" }

    $Tesseract = Resolve-FirstExisting -Candidates $Config.tesseractCandidates -Name "Tesseract"
    $Tessdata = Resolve-FirstExisting -Candidates $Config.tessdataCandidates -Name "Tesseract language data"
    $ocrProgress = Join-Path $RunDir "ocr_progress.json"
    $ocrStdout = Join-Path $RunDir "ocr_stdout.log"
    $ocrStderr = Join-Path $RunDir "ocr_stderr.log"
    Set-Stage -Key "ocr" -Status "running" -Message "OCR started" -StageProgress 0
    $ocrArgs = @(
      $Extractor,
      "--root", $OcrRoot,
      "--tesseract", $Tesseract,
      "--tessdata", $Tessdata,
      "--input-pattern", "GOU*.png",
      "--progress-file", $ocrProgress,
      "--stop-file", $StopFlagPath
    )
    $ocrProcess = Start-Process -FilePath $Python -ArgumentList $ocrArgs -PassThru -WindowStyle Hidden -RedirectStandardOutput $ocrStdout -RedirectStandardError $ocrStderr
    $lastOcr = -1
    while (-not $ocrProcess.HasExited) {
      if (Test-Path -LiteralPath $ocrProgress) {
        try {
          $progress = Get-Content -LiteralPath $ocrProgress -Raw -Encoding UTF8 | ConvertFrom-Json
          if ([int]$progress.current -ne $lastOcr) {
            $percent = if ([int]$progress.total -gt 0) { [Math]::Round(([int]$progress.current / [double]$progress.total) * 100) } else { 0 }
            Set-Stage -Key "ocr" -Status "running" -Message "OCR $($progress.current)/$($progress.total): $($progress.image)" -StageProgress $percent
            $lastOcr = [int]$progress.current
          }
        } catch { }
      }
      Start-Sleep -Milliseconds 750
      $ocrProcess.Refresh()
    }
    $ocrExitCode = Get-ProcessExitCodeSafely -Process $ocrProcess
    $ocrStderrText = if (Test-Path $ocrStderr) { Get-Content -LiteralPath $ocrStderr -Raw -ErrorAction SilentlyContinue } else { "" }
    if ($ocrExitCode -eq 2 -or (Test-StopRequested)) { throw "V5_STOPPED: OCR stopped safely before history update." }
    if ($null -ne $ocrExitCode -and $ocrExitCode -ne 0) {
      throw "OCR failed with exit code $ocrExitCode. $ocrStderrText"
    }
    if (-not (Test-Path -LiteralPath $OcrData)) { throw "OCR did not produce orders.json." }
    $ocrPayload = Get-Content -LiteralPath $OcrData -Raw -Encoding UTF8 | ConvertFrom-Json
    $ocrCompletedImages = [int]$ocrPayload.summary.completedImages
    if (-not [bool]$ocrPayload.summary.completed -or $ocrCompletedImages -ne $screenshotCount) {
      throw "OCR output is incomplete: completed=$($ocrPayload.summary.completed) images=$ocrCompletedImages expected=$screenshotCount. $ocrStderrText"
    }
    if ($null -eq $ocrExitCode) {
      if (-not [string]::IsNullOrWhiteSpace($ocrStderrText)) { throw "OCR exit code was unavailable and stderr was not empty: $ocrStderrText" }
      Write-RunLog "OCR exit code was unavailable, but complete output was verified for $ocrCompletedImages images."
    }
    Complete-Stage -Key "ocr" -Message "OCR completed for $screenshotCount orders"
  }

  Assert-Continue -SafePoint "before data validation"
  Set-Stage -Key "validate" -Status "running" -Message "Validating OCR completeness and duplicates" -StageProgress 20
  $validationArgs = @($ValidateScript, "--input", $OcrData, "--output", $ValidationPath, "--source-pattern", "^GOU[0-9A-Z-]+\.png$")
  if ($RunState.run_mode -ne "same_day_replay") { $validationArgs += @("--expected-images", [string]$RunState.screenshot_count) }
  Invoke-PythonChecked -Arguments $validationArgs -Name "validation"
  $validation = Get-Content -LiteralPath $ValidationPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $PendingReviewNext = Join-Path $RunDir "pending_review_next.json"
  $PendingReviewReport = Join-Path $RunDir "pending_review_report.json"
  Invoke-PythonChecked -Arguments @(
    $PendingReviewScript,
    "apply",
    "--review-json", $PendingReviewJson,
    "--output-review", $PendingReviewNext,
    "--current-orders", $OfficialCurrentOrders,
    "--ocr-json", $OcrData,
    "--runs-root", $RunsRoot,
    "--extractor", $Extractor,
    "--tesseract", $Tesseract,
    "--tessdata", $Tessdata,
    "--report", $PendingReviewReport
  ) -Name "pending_review_apply"
  Invoke-PythonChecked -Arguments @(
    $ValidateScript,
    "--input", $OcrData,
    "--output", $ValidationPath,
    "--source-pattern", "^GOU[0-9A-Z-]+\.png$"
  ) -Name "post_review_validation"
  $validation = Get-Content -LiteralPath $ValidationPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $reviewReport = Get-Content -LiteralPath $PendingReviewReport -Raw -Encoding UTF8 | ConvertFrom-Json
  Complete-Stage -Key "validate" -Message "Validated $($validation.total_orders) orders; pending=$($validation.pending); retry_success=$($reviewReport.retry_success); excluded=$($reviewReport.excluded)"

  Assert-Continue -SafePoint "before comparison"
  Set-Stage -Key "compare" -Status "running" -Message "Comparing complete orders and detail rows" -StageProgress 30
  Invoke-PythonChecked -Arguments @(
    $CompareScript,
    "--input", $OcrData,
    "--history", $OfficialHistory,
    "--output", $ComparisonPath,
    "--source-pattern", "^GOU[0-9A-Z-]+\.png$"
  ) -Name "comparison"
  $comparison = Get-Content -LiteralPath $ComparisonPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($ExpectUnchanged -and ([int]$comparison.summary.new_count -ne 0 -or [int]$comparison.summary.changed_count -ne 0)) {
    throw "Same-day replay expectation failed: new=$($comparison.summary.new_count) changed=$($comparison.summary.changed_count)"
  }
  Complete-Stage -Key "compare" -Message "new=$($comparison.summary.new_count) unchanged=$($comparison.summary.unchanged_count) changed=$($comparison.summary.changed_count) pending=$($comparison.summary.pending_count)"

  Assert-Continue -SafePoint "before official Excel and history update"
  Invoke-PythonChecked -Arguments @(
    $PendingReviewScript,
    "check-lock",
    "--excel", $OfficialExcel
  ) -Name "excel_close_check"
  Set-Stage -Key "excel" -Status "running" -Message "Updating official history and 4001 Excel atomically" -StageProgress 20
  Invoke-PythonChecked -Arguments @(
    $IncrementalEngine,
    "--input", $OcrData,
    "--state-dir", $OfficialStateDir,
    "--batch-complete",
    "--source-image-pattern", "^GOU[0-9A-Z-]+\.png$",
    "--node", $Node,
    "--excel-builder", $ExcelBuilder,
    "--pending-review", $PendingReviewNext,
    "--preview", $PreviewPath
  ) -Name "official_update"
  if (-not (Test-Path -LiteralPath $OfficialExcel)) { throw "Official current_orders.xlsx is missing after update." }
  $latestOfficialBackup = Get-ChildItem -LiteralPath (Join-Path $OfficialStateDir "backups") -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  if ($latestOfficialBackup) { $RunState.official_backup_dir = $latestOfficialBackup.FullName }
  $RunState.excel_path = $OfficialExcel
  Complete-Stage -Key "excel" -Message "Official history and Excel updated"

  Set-Stage -Key "complete" -Status "running" -Message "Creating daily summary" -StageProgress 50
  Invoke-PythonChecked -Arguments @(
    $SummaryScript,
    "--current-orders", (Join-Path $OfficialStateDir "current_orders.json"),
    "--validation", $ValidationPath,
    "--output-dir", $RunDir,
    "--query-start", [string]$RunState.query_start,
    "--query-end", [string]$RunState.query_end,
    "--started-at", [string]$RunState.started_at,
    "--excel", $OfficialExcel,
    "--run-mode", [string]$RunState.run_mode
  ) -Name "summary"
  $RunState.summary_path = Join-Path $RunDir "daily_summary.json"
  Complete-Stage -Key "complete" -Message "V5 daily flow completed"
  $RunState.status = "completed"
  $RunState.progress = 100
  $RunState.finished_at = (Get-Date).ToString("o")
  $RunState.message = "V5 completed successfully"
  Save-RunState
  Write-RunLog "V5 completed successfully"
} catch {
  $reason = $_.Exception.Message
  if ($null -ne $RunState) {
    $currentKey = [string]$RunState.current_stage
    $stage = Get-Stage -Key $currentKey
    if ($reason.StartsWith("V5_STOPPED:")) {
      $RunState.status = "stopped"
      if ($stage) { Set-Stage -Key $currentKey -Status "stopped" -Message $reason -StageProgress ([int]$stage.progress) }
      $ExitCode = 2
    } else {
      $RunState.status = "failed"
      if ($stage) { Set-Stage -Key $currentKey -Status "failed" -Message $reason -StageProgress ([int]$stage.progress) }
      $ExitCode = 1
    }
    $RunState.finished_at = (Get-Date).ToString("o")
    $RunState.message = $reason
    Save-RunState
  }
  Write-RunLog "ERROR $reason"
} finally {
  if ($MutexOwned -and $Mutex) {
    try { $Mutex.ReleaseMutex() } catch { }
    $Mutex.Dispose()
  }
}

exit $ExitCode
