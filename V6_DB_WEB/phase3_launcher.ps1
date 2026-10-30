param([switch]$SmokeTest)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Labels = Get-Content -LiteralPath (Join-Path $Root "phase3_labels.json") `
  -Raw -Encoding UTF8 | ConvertFrom-Json
$Worker = Join-Path $Root "phase3_worker.ps1"
$StopFlag = Join-Path $Root "control\STOP_V6_NOW.flag"
$LoginFlag = Join-Path $Root "control\LOGIN_CONTINUE.flag"
$ManualMaximizeRequestFlag = Join-Path $Root "control\MANUAL_MAXIMIZE_REQUIRED.flag"
$ManualMaximizeContinueFlag = Join-Path $Root "control\MANUAL_MAXIMIZE_CONTINUE.flag"
$LatestRunFile = Join-Path $Root "latest_run_path.txt"
$script:WorkerProcess = $null
$script:CurrentState = $null
$script:CurrentRunDir = ""

function Start-Worker {
  param(
    [ValidateSet("daily_7_days", "monthly_6_months")]
    [string]$Mode = "daily_7_days",
    [switch]$Resume
  )
  if ($script:WorkerProcess -and -not $script:WorkerProcess.HasExited) {
    [System.Windows.Forms.MessageBox]::Show(
      "V6 每日流程已經在執行。",
      $Labels.title
    ) | Out-Null
    return
  }
  for ($attempt = 1; $attempt -le 12; $attempt++) {
    Remove-Item -LiteralPath $StopFlag -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $StopFlag)) { break }
    Start-Sleep -Milliseconds ([Math]::Min(800, 40 * [Math]::Pow(2, $attempt - 1)))
  }
  if (Test-Path -LiteralPath $StopFlag) {
    [System.Windows.Forms.MessageBox]::Show(
      "舊的停止旗標無法清除，請查看 control 資料夾。",
      $Labels.title
    ) | Out-Null
    return
  }
  $arguments = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    ('"' + $Worker + '"'),
    "-Mode",
    $Mode
  )
  if ($Resume) { $arguments += "-ResumeLatest" }
  $script:WorkerProcess = Start-Process -FilePath "powershell.exe" `
    -ArgumentList $arguments -PassThru -WindowStyle Hidden
  $StartDailyButton.Enabled = $false
  $StartSixMonthsButton.Enabled = $false
  $ResumeButton.Enabled = $false
}

function Load-LatestState {
  if (-not (Test-Path -LiteralPath $LatestRunFile)) { return $null }
  $runDir = (
    Get-Content -LiteralPath $LatestRunFile -Raw -Encoding UTF8
  ).Trim()
  if ([string]::IsNullOrWhiteSpace($runDir)) { return $null }
  $statePath = Join-Path $runDir "run_state.json"
  if (-not (Test-Path -LiteralPath $statePath)) { return $null }
  try {
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 |
      ConvertFrom-Json
    $script:CurrentRunDir = $runDir
    return $state
  } catch {
    return $null
  }
}

function Refresh-Display {
  $state = Load-LatestState
  if ($null -eq $state) { return }
  $script:CurrentState = $state
  $ProgressBar.Value = [Math]::Min(
    100,
    [Math]::Max(0, [int]$state.progress)
  )
  $manualMaximizeRequired = Test-Path -LiteralPath $ManualMaximizeRequestFlag
  $StatusLabel.Text = (
    "模式：{0}｜{1}｜{2}｜目前訂單：{3}｜完成：{4}｜待確認：{5}" -f
    $state.run_mode,
    $state.status,
    $state.message,
    $(if ($state.current_order) { $state.current_order } else { "-" }),
    $state.completed_count,
    $state.pending_count
  )
  if ($manualMaximizeRequired) {
    $StatusLabel.Text = $Labels.manualMaximizeMessage
  }
  $StageList.Items.Clear()
  foreach ($stage in $state.stages) {
    $marker = switch ([string]$stage.status) {
      "completed" { "[OK]" }
      "running" { "[>>]" }
      "failed" { "[X ]" }
      "stopped" { "[!!]" }
      "skipped" { "[--]" }
      default { "[  ]" }
    }
    [void]$StageList.Items.Add(
      ("{0} {1,-18} {2}" -f $marker, $stage.label, $stage.message)
    )
  }
  $isRunning = $state.status -eq "running"
  $StartDailyButton.Enabled = -not $isRunning
  $StartSixMonthsButton.Enabled = -not $isRunning
  $ResumeButton.Enabled = $state.status -in @("stopped", "failed")
  $StopButton.Enabled = $isRunning -and
    $state.current_stage -notin @("sqlite_write", "complete")
  $ContinueButton.Enabled = $isRunning -and (
    $state.current_stage -eq "erp_prepare" -or $manualMaximizeRequired
  )
  $ContinueButton.Text = if ($manualMaximizeRequired) {
    $Labels.manualMaximizeContinue
  } else {
    $Labels.continue
  }
  $OpenRunButton.Enabled = Test-Path -LiteralPath $script:CurrentRunDir
}

$Form = New-Object System.Windows.Forms.Form
$Form.Text = $Labels.title
$Form.StartPosition = "CenterScreen"
$Form.Size = New-Object System.Drawing.Size(940, 590)
$Form.MinimumSize = New-Object System.Drawing.Size(860, 540)
$Form.Font = New-Object System.Drawing.Font("Microsoft JhengHei UI", 10)
$Form.BackColor = [System.Drawing.Color]::FromArgb(246, 247, 249)

$Header = New-Object System.Windows.Forms.Label
$Header.Text = $Labels.title
$Header.Font = New-Object System.Drawing.Font(
  "Microsoft JhengHei UI",
  18,
  [System.Drawing.FontStyle]::Bold
)
$Header.Location = New-Object System.Drawing.Point(28, 22)
$Header.AutoSize = $true
$Form.Controls.Add($Header)

$StatusLabel = New-Object System.Windows.Forms.Label
$StatusLabel.Text = $Labels.idle
$StatusLabel.Location = New-Object System.Drawing.Point(31, 70)
$StatusLabel.Size = New-Object System.Drawing.Size(870, 28)
$Form.Controls.Add($StatusLabel)

$ProgressBar = New-Object System.Windows.Forms.ProgressBar
$ProgressBar.Location = New-Object System.Drawing.Point(32, 103)
$ProgressBar.Size = New-Object System.Drawing.Size(870, 20)
$ProgressBar.Style = "Continuous"
$Form.Controls.Add($ProgressBar)

$StageList = New-Object System.Windows.Forms.ListBox
$StageList.Location = New-Object System.Drawing.Point(32, 143)
$StageList.Size = New-Object System.Drawing.Size(870, 285)
$StageList.Font = New-Object System.Drawing.Font("Consolas", 10)
$StageList.BackColor = [System.Drawing.Color]::White
$StageList.BorderStyle = "FixedSingle"
$Form.Controls.Add($StageList)

$StartDailyButton = New-Object System.Windows.Forms.Button
$StartDailyButton.Text = "開始每日 7 天流程"
$StartDailyButton.Location = New-Object System.Drawing.Point(32, 450)
$StartDailyButton.Size = New-Object System.Drawing.Size(180, 40)
$StartDailyButton.Add_Click({ Start-Worker -Mode "daily_7_days" })
$Form.Controls.Add($StartDailyButton)

$StartSixMonthsButton = New-Object System.Windows.Forms.Button
$StartSixMonthsButton.Text = "開始最近 6 個月流程"
$StartSixMonthsButton.Location = New-Object System.Drawing.Point(222, 450)
$StartSixMonthsButton.Size = New-Object System.Drawing.Size(200, 40)
$StartSixMonthsButton.Add_Click({ Start-Worker -Mode "monthly_6_months" })
$Form.Controls.Add($StartSixMonthsButton)

$ContinueButton = New-Object System.Windows.Forms.Button
$ContinueButton.Text = $Labels.continue
$ContinueButton.Location = New-Object System.Drawing.Point(432, 450)
$ContinueButton.Size = New-Object System.Drawing.Size(165, 40)
$ContinueButton.Enabled = $false
$ContinueButton.Add_Click({
  $Form.WindowState = [System.Windows.Forms.FormWindowState]::Minimized
  [System.Windows.Forms.Application]::DoEvents()
  Start-Sleep -Milliseconds 400
  New-Item -ItemType Directory -Force -Path (Split-Path $LoginFlag) |
    Out-Null
  $continueFlag = if (Test-Path -LiteralPath $ManualMaximizeRequestFlag) {
    $ManualMaximizeContinueFlag
  } else {
    $LoginFlag
  }
  Set-Content -LiteralPath $continueFlag `
    -Value (Get-Date).ToString("o") -Encoding ASCII
})
$Form.Controls.Add($ContinueButton)

$StopButton = New-Object System.Windows.Forms.Button
$StopButton.Text = $Labels.stop
$StopButton.Location = New-Object System.Drawing.Point(607, 450)
$StopButton.Size = New-Object System.Drawing.Size(95, 40)
$StopButton.Enabled = $false
$StopButton.Add_Click({
  New-Item -ItemType Directory -Force -Path (Split-Path $StopFlag) |
    Out-Null
  Set-Content -LiteralPath $StopFlag `
    -Value (Get-Date).ToString("o") -Encoding ASCII
})
$Form.Controls.Add($StopButton)

$ResumeButton = New-Object System.Windows.Forms.Button
$ResumeButton.Text = $Labels.resume
$ResumeButton.Location = New-Object System.Drawing.Point(712, 450)
$ResumeButton.Size = New-Object System.Drawing.Size(190, 40)
$ResumeButton.Enabled = $false
$ResumeButton.Add_Click({
  $resumeMode = if (
    $script:CurrentState -and
    $script:CurrentState.run_mode -eq "monthly_6_months"
  ) { "monthly_6_months" } else { "daily_7_days" }
  Start-Worker -Mode $resumeMode -Resume
})
$Form.Controls.Add($ResumeButton)

$OpenRunButton = New-Object System.Windows.Forms.Button
$OpenRunButton.Text = $Labels.openRun
$OpenRunButton.Location = New-Object System.Drawing.Point(32, 505)
$OpenRunButton.Size = New-Object System.Drawing.Size(155, 35)
$OpenRunButton.Enabled = $false
$OpenRunButton.Add_Click({
  if ($script:CurrentRunDir) {
    Invoke-Item -LiteralPath $script:CurrentRunDir
  }
})
$Form.Controls.Add($OpenRunButton)

$CloseButton = New-Object System.Windows.Forms.Button
$CloseButton.Text = $Labels.close
$CloseButton.Location = New-Object System.Drawing.Point(807, 505)
$CloseButton.Size = New-Object System.Drawing.Size(95, 35)
$CloseButton.Add_Click({ $Form.Close() })
$Form.Controls.Add($CloseButton)

$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = 800
$Timer.Add_Tick({ Refresh-Display })
$Timer.Start()
Refresh-Display
if ($SmokeTest) {
  "V6_PHASE3_LAUNCHER_SMOKE_OK controls=$($Form.Controls.Count)"
  $Timer.Stop()
  $Form.Dispose()
  exit 0
}
[void]$Form.ShowDialog()
$Timer.Stop()
