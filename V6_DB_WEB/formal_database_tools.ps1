param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("Initialize", "Import", "Validate", "Backup", "Restore", "Migrate")]
  [string]$Action,
  [string]$BackupPath = ""
)

$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Database = Join-Path $Root "data\orders.db"
$Source = Join-Path (Split-Path -Parent $Root) "V5_ERP_Complete_V5.10_20260721"
$ReportStamp = Get-Date -Format "yyyyMMdd_HHmmss"

function Resolve-Python {
  foreach ($candidate in @(
    (Join-Path $Root "runtime\python\python.exe"),
    "python.exe",
    "python"
  )) {
    if ([System.IO.Path]::IsPathRooted($candidate)) {
      if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    } else {
      $command = Get-Command $candidate -ErrorAction SilentlyContinue
      if ($command) { return $command.Source }
    }
  }
  throw "找不到 Python。"
}

function Invoke-V6 {
  param([string[]]$Arguments)
  & $script:Python -X utf8 (Join-Path $Root "v6.py") @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "V6 指令執行失敗，exit=$LASTEXITCODE"
  }
}

$script:Python = Resolve-Python

switch ($Action) {
  "Initialize" {
    Write-Host "即將全新建立正式資料庫：$Database" -ForegroundColor Yellow
    Write-Host "不會複製或改名 orders_test.db。"
    $answer = Read-Host "確認請輸入：建立正式資料庫"
    if ($answer -cne "建立正式資料庫") { throw "使用者取消初始化。" }
    Invoke-V6 @(
      "initialize-formal", "--db", $Database,
      "--confirm", "CREATE-FORMAL-ORDERS-DB"
    )
  }
  "Import" {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
      throw "找不到 V5.10 來源：$Source"
    }
    Invoke-V6 @(
      "import-formal-v5", "--db", $Database,
      "--source", $Source,
      "--report", (Join-Path $Root "reports\PHASE4_FORMAL_IMPORT_$ReportStamp.json")
    )
  }
  "Validate" {
    $JsonReport = Join-Path $Root "reports\FORMAL_RUNTIME_VALIDATION_$ReportStamp.json"
    $MarkdownReport = Join-Path $Root "reports\FORMAL_RUNTIME_VALIDATION_$ReportStamp.md"
    & $script:Python -X utf8 (Join-Path $Root "web_v1\validate_database.py") `
      --database $Database --json-report $JsonReport `
      --markdown-report $MarkdownReport
    if ($LASTEXITCODE -ne 0) {
      throw "正式資料庫動態驗證未通過，請查看：$MarkdownReport"
    }
    Write-Host "正式資料庫動態驗證通過：$MarkdownReport" -ForegroundColor Green
  }
  "Backup" {
    Invoke-V6 @(
      "backup-formal", "--db", $Database,
      "--notes", "Manual backup from BACKUP_FORMAL_DATABASE.bat"
    )
  }
  "Migrate" {
    Invoke-V6 @(
      "apply-formal-migrations", "--db", $Database
    )
  }
  "Restore" {
    if ([string]::IsNullOrWhiteSpace($BackupPath)) {
      Write-Host "可用備份：" -ForegroundColor Cyan
      Get-ChildItem -LiteralPath (Join-Path $Root "backups\formal") `
        -File -Filter "orders_*.db" | Sort-Object LastWriteTime -Descending |
        Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
      $BackupPath = Read-Host "請輸入要還原的完整備份路徑"
    }
    $answer = Read-Host "確認還原請輸入：還原正式資料庫"
    if ($answer -cne "還原正式資料庫") { throw "使用者取消還原。" }
    Invoke-V6 @(
      "restore-formal", "--db", $Database,
      "--backup", $BackupPath,
      "--confirm", "RESTORE-FORMAL-ORDERS-DB"
    )
  }
}

Write-Host "完成。" -ForegroundColor Green
