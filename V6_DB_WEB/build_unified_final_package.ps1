param([string]$Destination = "")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
if ([string]::IsNullOrWhiteSpace($Destination)) {
  $Destination = Join-Path $Root "dist\V6_DB_WEB_DUAL_R4_$Stamp"
}
$Destination = [IO.Path]::GetFullPath($Destination)
$DistRoot = [IO.Path]::GetFullPath((Join-Path $Root "dist"))
if (-not $Destination.StartsWith($DistRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "封裝目標必須位於 dist：$Destination"
}
$ZipPath = "$Destination.zip"
if ((Test-Path -LiteralPath $Destination) -or (Test-Path -LiteralPath $ZipPath)) {
  throw "封裝目標已存在：$Destination"
}

$RuntimeRoot = Join-Path $Root (
  "erp_returns\20260824_r6_full_return\" +
  "V6_HISTORY_2022_TO_NOW_R6_FULL_20260812_200955\runtime"
)
foreach ($required in @(
  (Join-Path $Root "data\orders.db"),
  (Join-Path $RuntimeRoot "python\python.exe"),
  (Join-Path $RuntimeRoot "tesseract\tesseract.exe")
)) {
  if (-not (Test-Path -LiteralPath $required)) { throw "缺少封裝來源：$required" }
}

function Copy-RootFile([string]$Relative) {
  $Source = Join-Path $Root $Relative
  if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
    throw "缺少必要檔案：$Relative"
  }
  $Target = Join-Path $Destination $Relative
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
  Copy-Item -LiteralPath $Source -Destination $Target
}

function Copy-RootDirectory([string]$Relative) {
  $Source = Join-Path $Root $Relative
  if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "缺少必要資料夾：$Relative"
  }
  $TargetParent = Split-Path -Parent (Join-Path $Destination $Relative)
  New-Item -ItemType Directory -Force -Path $TargetParent | Out-Null
  Copy-Item -LiteralPath $Source -Destination $TargetParent -Recurse
}

function Remove-PackageArtifacts([string]$PackageRoot) {
  $Resolved = [IO.Path]::GetFullPath($PackageRoot)
  if (-not $Resolved.StartsWith($DistRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "拒絕清理 dist 以外的路徑：$Resolved"
  }
  Get-ChildItem -LiteralPath $Resolved -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
  Get-ChildItem -LiteralPath $Resolved -Recurse -File |
    Where-Object {
      $_.Extension -in @(".pyc", ".tmp") -or
      $_.Name -match "\.db-(shm|wal|journal)$" -or
      $_.Name -like "*.write.lock"
    } |
    Remove-Item -Force
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
foreach ($relative in @(
  "v6.py", "pyproject.toml", "phase3_config.json", "phase3_labels.json",
  "phase3_launcher.ps1", "phase3_worker.ps1", "check_phase3_environment.ps1",
  "formal_database_modes.json", "formal_database_tools.ps1",
  "START_DAILY_AND_6_MONTHS.bat", "START_WEDDING_ORDERS_WEB.bat",
  "START_WEDDING_ORDERS_WEB_DEBUG.bat",
  "STOP_V6_PHASE3_NOW.bat", "BACKUP_FORMAL_DATABASE.bat",
  "RESTORE_FORMAL_DATABASE.bat", "VALIDATE_FORMAL_DATABASE.bat",
  "RUN_OFFLINE_SELF_TESTS.bat", "RUN_7_DAY_IMAGE_CLEANUP.bat",
  "INSTALL_7_DAY_IMAGE_CLEANUP.bat"
)) { Copy-RootFile $relative }
foreach ($relative in @("src", "migrations", "components")) {
  Copy-RootDirectory $relative
}
foreach ($relative in @(
  "tests\test_phase3_pipeline.py",
  "tests\test_ocr_ingest.py",
  "tests\test_r3_date_ocr.py",
  "tests\fixtures\daily_7_days_ocr.json"
)) { Copy-RootFile $relative }
Copy-RootDirectory "tests\fixtures\r4_date_ocr"

foreach ($relative in @(
  "app.py", "cleanup_success_images.py", "validate_database.py",
  "install_image_cleanup_task.ps1", "INSTALL_7_DAY_IMAGE_CLEANUP.bat",
  "RUN_7_DAY_IMAGE_CLEANUP.bat", "README.md"
)) { Copy-RootFile ("web_v1\" + $relative) }
foreach ($relative in @("web_v1\wedding_web", "web_v1\static", "web_v1\tests")) {
  Copy-RootDirectory $relative
}

foreach ($relative in @(
  "docs\UNIFIED_DATABASE_OPERATION_GUIDE.md",
  "docs\UNIFIED_DATABASE_BACKUP_RESTORE.md",
  "reports\LATEST_MANUAL_CONFIRMATION_DATABASE_VALIDATION.json",
  "reports\LATEST_MANUAL_CONFIRMATION_DATABASE_VALIDATION.md",
  "reports\LATEST_MANUAL_CONFIRMATION_REPACKAGE.md",
  "reports\UNIFIED_DATABASE_INTEGRATION_TEST.md",
  "reports\DUAL_LITE_DATABASE_VALIDATION_20260825.json",
  "reports\DUAL_LITE_DATABASE_VALIDATION_20260825.md",
  "reports\DUAL_MODE_LITE_ACCEPTANCE_20260825.md",
  "reports\DUAL_R2_DATABASE_VALIDATION_20260825.json",
  "reports\DUAL_R2_DATABASE_VALIDATION_20260825.md",
  "reports\QUERY_RESULT_DETECTION_FIX_20260825.md",
  "reports\DUAL_MODE_FIRST_CAPTURE_ACCEPTANCE_20260825.md",
  "reports\R3_QUERY_DATE_AND_PENDING_IMAGE_FIX_20260825.md",
  "reports\R3_ERP_RETURN_ACCEPTANCE_20260825.md",
  "reports\R3_RETURNED_DATABASE_BEFORE_REPAIR.json",
  "reports\R3_RETURNED_DATABASE_BEFORE_REPAIR.md",
  "reports\R3_FINAL_DATABASE_VALIDATION_20260825.json",
  "reports\R3_FINAL_DATABASE_VALIDATION_20260825.md",
  "reports\R4_DATE_RULE_FIX_20260826.md",
  "reports\R4_1735_REPLAY_ACCEPTANCE_20260826.md",
  "reports\R4_DATABASE_REPAIR_BEFORE_AFTER_20260826.md",
  "reports\R4_OFFLINE_ACCEPTANCE_20260826.md",
  "reports\R4_FINAL_DATABASE_VALIDATION_20260826.json",
  "reports\R4_FINAL_DATABASE_VALIDATION_20260826.md",
  "reports\R4_FULL_DATABASE_DATE_ANOMALIES_20260826.json",
  "reports\R4_FULL_DATABASE_DATE_ANOMALIES_20260826.csv",
  "reports\R4_DATE_EVIDENCE_REPROCESS_20260826.json",
  "reports\R4_1735_REPLAY_FIRST_20260826.json",
  "reports\R4_1735_REPLAY_SECOND_20260826.json",
  "reports\R4_FIVE_MANUAL_DATES_20260826.json",
  "reports\R4_BHQ_INTEGRATION_20260826.json",
  "reports\R4_BHQ_INTEGRATION_IDEMPOTENCY_20260826.json"
)) { Copy-RootFile $relative }

foreach ($directory in @("data", "backups\formal", "backups\manual_review", "runs", "control", "environment_reports", "logs")) {
  New-Item -ItemType Directory -Force -Path (Join-Path $Destination $directory) | Out-Null
}
Copy-Item -LiteralPath (Join-Path $Root "data\images") `
  -Destination (Join-Path $Destination "data") -Recurse

New-Item -ItemType Directory -Force -Path (Join-Path $Destination "runtime") | Out-Null
Copy-Item -LiteralPath (Join-Path $RuntimeRoot "python") `
  -Destination (Join-Path $Destination "runtime") -Recurse
Copy-Item -LiteralPath (Join-Path $RuntimeRoot "tesseract") `
  -Destination (Join-Path $Destination "runtime") -Recurse

$ComponentFiles = @(
  "erp_capture/capture_erp_v4_full_batch.ps1",
  "erp_capture/capture_guard_helpers.ps1",
  "ocr/extract_orders.py"
)
$ComponentHashes = [ordered]@{}
foreach ($relative in $ComponentFiles) {
  $component = Join-Path (Join-Path $Destination "components") $relative
  if (-not (Test-Path -LiteralPath $component -PathType Leaf)) {
    throw "封裝缺少元件：$relative"
  }
  $ComponentHashes[$relative] = (
    Get-FileHash -LiteralPath $component -Algorithm SHA256
  ).Hash
}
[ordered]@{
  source = "V6 final daily-7-days and monthly-6-months runtime"
  copied_without_modification = $false
  files = $ComponentHashes
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (
  Join-Path $Destination "components\COMPONENT_SOURCES.json"
) -Encoding UTF8

$SourcePython = Join-Path $RuntimeRoot "python\python.exe"
$SourceValidationJson = Join-Path $Root "reports\R4_PACKAGE_SOURCE_VALIDATION_$Stamp.json"
$SourceValidationMarkdown = Join-Path $Root "reports\R4_PACKAGE_SOURCE_VALIDATION_$Stamp.md"
& $SourcePython -X utf8 (Join-Path $Root "web_v1\validate_database.py") `
  --database (Join-Path $Root "data\orders.db") `
  --json-report $SourceValidationJson --markdown-report $SourceValidationMarkdown
if ($LASTEXITCODE -ne 0) { throw "封裝來源正式資料庫驗證失敗" }
$SourceDatabaseReport = Get-Content -LiteralPath $SourceValidationJson `
  -Raw -Encoding UTF8 | ConvertFrom-Json
& $SourcePython -X utf8 (Join-Path $Root "tools\snapshot_database.py") `
  --source (Join-Path $Root "data\orders.db") `
  --destination (Join-Path $Destination "data\orders.db")
if ($LASTEXITCODE -ne 0) { throw "正式資料庫快照失敗" }

Remove-PackageArtifacts $Destination
$Python = Join-Path $Destination "runtime\python\python.exe"
$ValidationJson = Join-Path $Destination "reports\PACKAGE_DATABASE_VALIDATION.json"
$ValidationMarkdown = Join-Path $Destination "reports\PACKAGE_DATABASE_VALIDATION.md"
& $Python -X utf8 (Join-Path $Destination "web_v1\validate_database.py") `
  --database (Join-Path $Destination "data\orders.db") `
  --json-report $ValidationJson --markdown-report $ValidationMarkdown
if ($LASTEXITCODE -ne 0) { throw "封裝後資料庫驗證失敗" }
$PackageDatabaseReport = Get-Content -LiteralPath $ValidationJson -Raw -Encoding UTF8 |
  ConvertFrom-Json
if (
  [int]$PackageDatabaseReport.counts.orders -ne [int]$SourceDatabaseReport.counts.orders -or
  [int]$PackageDatabaseReport.counts.order_versions -ne [int]$SourceDatabaseReport.counts.order_versions -or
  [int]$PackageDatabaseReport.counts.order_items -ne [int]$SourceDatabaseReport.counts.order_items -or
  [int]$PackageDatabaseReport.counts.formal_orders_sr -ne [int]$SourceDatabaseReport.counts.formal_orders_sr -or
  [int]$PackageDatabaseReport.counts.review_queue_pending -ne [int]$SourceDatabaseReport.counts.review_queue_pending -or
  [int]$PackageDatabaseReport.counts.GOU26080250 -ne 1 -or
  [int]$PackageDatabaseReport.counts.GOU26080219 -ne 1 -or
  [int]$PackageDatabaseReport.counts.GOU26070611 -ne 1
) {
  throw "正式資料基準不符，停止封裝。"
}

$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$(Join-Path $Destination 'src');$(Join-Path $Destination 'web_v1')"
& $Python -X utf8 -m unittest discover `
  -s (Join-Path $Destination "web_v1\tests") -v
$PackageTestExit = $LASTEXITCODE
if ($PackageTestExit -eq 0) {
  & $Python -X utf8 -m unittest discover `
    -s (Join-Path $Destination "tests") -p "test_*.py" -v
  $PackageTestExit = $LASTEXITCODE
}
$env:PYTHONPATH = $PreviousPythonPath
if ($PackageTestExit -ne 0) { throw "封裝後整合測試失敗" }

$WebStdout = Join-Path $Destination "logs\package_web_startup_stdout.log"
$WebStderr = Join-Path $Destination "logs\package_web_startup_stderr.log"
$WebArguments = @(
  "-X", "utf8",
  ('"' + (Join-Path $Destination "web_v1\app.py") + '"'),
  "--database", ('"' + (Join-Path $Destination "data\orders.db") + '"'),
  "--host", "127.0.0.1", "--port", "8765",
  "--startup-log", ('"' + (Join-Path $Destination "logs\web_startup_error.log") + '"')
)
$WebProcess = Start-Process -FilePath $Python -ArgumentList $WebArguments `
  -PassThru -WindowStyle Hidden -RedirectStandardOutput $WebStdout `
  -RedirectStandardError $WebStderr
$WebReady = $false
$WebHttpStatus = 0
$WebHealth = $null
try {
  for ($attempt = 1; $attempt -le 80; $attempt++) {
    Start-Sleep -Milliseconds 250
    if ($WebProcess.HasExited) { break }
    try {
      $Response = Invoke-WebRequest -UseBasicParsing `
        -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 2
      $WebHttpStatus = [int]$Response.StatusCode
      $WebHealth = $Response.Content | ConvertFrom-Json
      if ($WebHttpStatus -eq 200 -and $WebHealth.status -eq "ok") {
        $WebReady = $true
        break
      }
    } catch { }
  }
} finally {
  if (-not $WebProcess.HasExited) {
    Stop-Process -Id $WebProcess.Id -Force
    $WebProcess.WaitForExit()
  }
}
if (-not $WebReady) {
  $stdout = if (Test-Path -LiteralPath $WebStdout) {
    Get-Content -LiteralPath $WebStdout -Raw -Encoding UTF8
  } else { "" }
  $stderr = if (Test-Path -LiteralPath $WebStderr) {
    Get-Content -LiteralPath $WebStderr -Raw -Encoding UTF8
  } else { "" }
  throw "封裝網站 HTTP 200 測試失敗。stdout=$stdout stderr=$stderr"
}
$WebTestReport = [ordered]@{
  status = "passed"
  tested_at = (Get-Date).ToString("o")
  url = "http://127.0.0.1:8765/api/health"
  http_status = $WebHttpStatus
  database_name = [string]$WebHealth.database_name
  journal_mode = [string]$WebHealth.journal_mode
  formal_orders = [int]$WebHealth.formal_orders
  pending = [int]$WebHealth.pending
  launcher_uses_direct_python = $true
}
$WebTestReport | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (
  Join-Path $Destination "reports\PACKAGE_WEB_STARTUP_HTTP_TEST.json"
) -Encoding UTF8

Remove-PackageArtifacts $Destination
$Forbidden = Get-ChildItem -LiteralPath $Destination -Recurse -File |
  Where-Object { $_.Name -ieq "orders_web.db" }
if ($Forbidden) { throw "封裝內仍存在 orders_web.db" }

$ForbiddenHistoryFiles = @(
  "START_HISTORY_2022_TO_NOW.bat",
  "START_2022_01_HISTORY_TEST.bat",
  "STOP_HISTORY_SCAN.bat",
  "history_full_launcher.ps1",
  "history_full_worker.ps1",
  "history_scan_config.json",
  "history_scan.py",
  "history_validation.py"
)
$HistoryFiles = @(
  Get-ChildItem -LiteralPath $Destination -Recurse -File |
    Where-Object { $ForbiddenHistoryFiles -contains $_.Name }
)
if ($HistoryFiles.Count -gt 0) {
  throw "精簡封裝仍含歷史執行檔：$($HistoryFiles.FullName -join ', ')"
}
$OperationalFiles = @(
  (Join-Path $Destination "src\v6_db_web\phase3_pipeline.py"),
  (Join-Path $Destination "src\v6_db_web\cli.py"),
  (Join-Path $Destination "components\erp_capture\capture_erp_v4_full_batch.ps1"),
  (Join-Path $Destination "components\erp_capture\capture_guard_helpers.ps1"),
  (Join-Path $Destination "phase3_launcher.ps1"),
  (Join-Path $Destination "phase3_worker.ps1")
)
foreach ($file in $OperationalFiles) {
  $text = Get-Content -LiteralPath $file -Raw -Encoding UTF8
  if ($text -match "history_monthly|formal_history|Get-ExpectedMonthFirstOrder|GOUYYMM0001|month_first_order") {
    throw "精簡封裝仍含歷史執行邏輯：$file"
  }
}

$manifestFiles = @(Get-ChildItem -LiteralPath $Destination -Recurse -File)
$manifest = [ordered]@{
  package = "V6_DB_WEB_DUAL_R4"
  built_at = (Get-Date).ToString("o")
  supported_modes = @("daily_7_days", "monthly_6_months")
  history_execution_enabled = $false
  standalone_python = $true
  internet_required = $false
  formal_database = "data/orders.db"
  writable_databases = @("data/orders.db")
  pending_images = @(Get-ChildItem -LiteralPath (
    Join-Path $Destination "data\images\pending_review"
  ) -File).Count
  files = @($manifestFiles | ForEach-Object {
    [ordered]@{
      relative_path = $_.FullName.Substring($Destination.Length + 1).Replace("\", "/")
      bytes = $_.Length
      sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
  })
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (
  Join-Path $Destination "package_manifest.json"
) -Encoding UTF8

Compress-Archive -LiteralPath $Destination -DestinationPath $ZipPath -CompressionLevel Optimal
$ZipHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
Set-Content -LiteralPath "$ZipPath.sha256.txt" `
  -Value "$ZipHash  $(Split-Path -Leaf $ZipPath)" -Encoding ASCII

$ExtractRoot = Join-Path $Root "verification\unified_extract_$Stamp"
New-Item -ItemType Directory -Force -Path $ExtractRoot | Out-Null
Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractRoot
$Extracted = Join-Path $ExtractRoot (Split-Path -Leaf $Destination)
$ExtractedPython = Join-Path $Extracted "runtime\python\python.exe"
& $ExtractedPython -X utf8 (Join-Path $Extracted "web_v1\validate_database.py") `
  --database (Join-Path $Extracted "data\orders.db") `
  --json-report (Join-Path $Extracted "reports\EXTRACTED_DATABASE_VALIDATION.json") `
  --markdown-report (Join-Path $Extracted "reports\EXTRACTED_DATABASE_VALIDATION.md")
if ($LASTEXITCODE -ne 0) { throw "ZIP 解壓後資料庫驗證失敗" }

$ExtractedForbidden = Get-ChildItem -LiteralPath $Extracted -Recurse -File |
  Where-Object { $_.Name -ieq "orders_web.db" }
if ($ExtractedForbidden) { throw "ZIP 解壓後仍存在 orders_web.db" }

Write-Output "PACKAGE=$Destination"
Write-Output "ZIP=$ZipPath"
Write-Output "SHA256=$ZipHash"
Write-Output "EXTRACTED_VERIFICATION=$Extracted"
