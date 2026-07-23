param([switch]$SmokeTest, [switch]$ForceFullSixMonths)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Labels = Get-Content -LiteralPath (Join-Path $Root "v5_labels.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$Worker = Join-Path $Root "v5_worker.ps1"
$StopFlag = Join-Path $Root "STOP_V5_NOW.flag"
$LoginFlag = Join-Path $Root "LOGIN_CONTINUE.flag"
$LatestRunFile = Join-Path $Root "latest_run_path.txt"
$script:WorkerProcess = $null
$script:CurrentState = $null
$script:CurrentRunDir = ""
$script:SelectedQueryMode = if ($ForceFullSixMonths) { "six_months" } else { "seven_days" }

function Start-Worker {
  param([switch]$Resume)
  if ($script:WorkerProcess -and -not $script:WorkerProcess.HasExited) {
    [System.Windows.Forms.MessageBox]::Show("V5 is already running.", $Labels.title) | Out-Null
    return
  }
  Remove-Item -LiteralPath $StopFlag -Force -ErrorAction SilentlyContinue
  $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $Worker + '"'))
  if ($Resume) {
    $arguments += "-ResumeLatest"
  } elseif ($script:SelectedQueryMode -eq "six_months") {
    $arguments += "-ForceFullSixMonths"
  } else {
    # Make the interface choice authoritative even when today is the first day
    # of a month, when the worker would otherwise select the monthly mode.
    $arguments += "-ForceDailyIncremental"
  }
  $script:WorkerProcess = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -PassThru -WindowStyle Hidden
  $selectedRange = if ($script:SelectedQueryMode -eq "six_months") { $Labels.rangeSixMonths } else { $Labels.rangeSevenDays }
  $StatusLabel.Text = "$($Labels.running)  $($Labels.queryRange): $selectedRange"
  $StartButton.Enabled = $false
  $ResumeButton.Enabled = $false
  $ModePanel.Enabled = $false
}

function Load-LatestState {
  # A transferred package can contain a stale absolute path from another PC.
  # Prefer the newest state physically stored under this package's runs folder.
  $localRuns = Join-Path $Root "runs"
  $latestLocalState = if (Test-Path -LiteralPath $localRuns) {
    Get-ChildItem -LiteralPath $localRuns -Recurse -Filter "run_state.json" -File -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending |
      Select-Object -First 1
  } else {
    $null
  }

  $statePath = if ($null -ne $latestLocalState) { $latestLocalState.FullName } else { "" }
  if ([string]::IsNullOrWhiteSpace($statePath) -and (Test-Path -LiteralPath $LatestRunFile)) {
    $runDir = (Get-Content -LiteralPath $LatestRunFile -Raw -Encoding UTF8).Trim()
    if (-not [string]::IsNullOrWhiteSpace($runDir)) {
      $candidate = Join-Path $runDir "run_state.json"
      if (Test-Path -LiteralPath $candidate) { $statePath = $candidate }
    }
  }
  if ([string]::IsNullOrWhiteSpace($statePath)) { return $null }
  try {
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $script:CurrentRunDir = Split-Path -Parent $statePath
    return $state
  } catch {
    return $null
  }
}

function Test-V5WorkerActive {
  if ($script:WorkerProcess -and -not $script:WorkerProcess.HasExited) {
    return $true
  }

  $createdNew = $false
  $probe = $null
  try {
    $probe = New-Object System.Threading.Mutex($true, "Local\WeddingOrderV5DailyFullFlow", [ref]$createdNew)
    if ($createdNew) {
      $probe.ReleaseMutex()
      return $false
    }
    return $true
  } catch {
    return $false
  } finally {
    if ($null -ne $probe) { $probe.Dispose() }
  }
}

function Update-ModeAppearance {
  $selectedBack = [System.Drawing.Color]::FromArgb(0, 102, 184)
  $selectedFore = [System.Drawing.Color]::White
  $normalBack = [System.Drawing.Color]::White
  $normalFore = [System.Drawing.Color]::FromArgb(30, 30, 30)
  $normalBorder = [System.Drawing.Color]::FromArgb(110, 118, 128)

  $selectedButton = if ($script:SelectedQueryMode -eq "six_months") { $SixMonthMode } else { $SevenDayMode }
  foreach ($button in @($SevenDayMode, $SixMonthMode)) {
    if ([object]::ReferenceEquals($button, $selectedButton)) {
      $button.BackColor = $selectedBack
      $button.ForeColor = $selectedFore
      $button.FlatAppearance.BorderColor = $selectedBack
      $button.FlatAppearance.BorderSize = 2
    } else {
      $button.BackColor = $normalBack
      $button.ForeColor = $normalFore
      $button.FlatAppearance.BorderColor = $normalBorder
      $button.FlatAppearance.BorderSize = 1
    }
  }
}

function Refresh-Display {
  $state = Load-LatestState
  $workerActive = Test-V5WorkerActive
  if ($null -eq $state) {
    $StartButton.Enabled = -not $workerActive
    $ResumeButton.Enabled = $false
    $StopButton.Enabled = $workerActive
    $ContinueButton.Enabled = $false
    $ModePanel.Enabled = -not $workerActive
    return
  }
  $script:CurrentState = $state
  $ProgressBar.Value = [Math]::Min(100, [Math]::Max(0, [int]$state.progress))
  $stateWasInterrupted = $state.status -eq "running" -and -not $workerActive
  $StatusLabel.Text = if ($stateWasInterrupted) { $Labels.interrupted } else { "$($state.status)  $($state.message)" }
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
    [void]$StageList.Items.Add(("{0} {1,-12} {2}" -f $marker, $stage.label, $stage.message))
  }
  $StartButton.Enabled = -not $workerActive
  $ResumeButton.Enabled = -not $workerActive -and ($state.status -in @("stopped", "failed", "running"))
  $StopButton.Enabled = $workerActive -and $state.current_stage -notin @("excel", "complete")
  $ContinueButton.Enabled = $workerActive -and $state.status -eq "running" -and $state.current_stage -eq "erp_prepare"
  $ModePanel.Enabled = -not $workerActive
  $OpenExcelButton.Enabled = -not [string]::IsNullOrWhiteSpace([string]$state.excel_path) -and (Test-Path -LiteralPath ([string]$state.excel_path))
  $OpenLogsButton.Enabled = Test-Path -LiteralPath $script:CurrentRunDir
}

$Form = New-Object System.Windows.Forms.Form
$Form.Text = $Labels.title
$Form.StartPosition = "CenterScreen"
$Form.Size = New-Object System.Drawing.Size(900, 570)
$Form.MinimumSize = New-Object System.Drawing.Size(820, 520)
$Form.Font = New-Object System.Drawing.Font("Microsoft JhengHei UI", 10)
$Form.BackColor = [System.Drawing.Color]::FromArgb(246, 247, 249)

$Header = New-Object System.Windows.Forms.Label
$Header.Text = $Labels.title
$Header.Font = New-Object System.Drawing.Font("Microsoft JhengHei UI", 19, [System.Drawing.FontStyle]::Bold)
$Header.Location = New-Object System.Drawing.Point(28, 22)
$Header.AutoSize = $true
$Form.Controls.Add($Header)

$ModeLabel = New-Object System.Windows.Forms.Label
$ModeLabel.Text = $Labels.queryRange
$ModeLabel.Location = New-Object System.Drawing.Point(32, 68)
$ModeLabel.Size = New-Object System.Drawing.Size(82, 28)
$ModeLabel.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
$Form.Controls.Add($ModeLabel)

$ModePanel = New-Object System.Windows.Forms.Panel
$ModePanel.Location = New-Object System.Drawing.Point(118, 62)
$ModePanel.Size = New-Object System.Drawing.Size(252, 36)
$Form.Controls.Add($ModePanel)

$SevenDayMode = New-Object System.Windows.Forms.Button
$SevenDayMode.Text = $Labels.rangeSevenDays
$SevenDayMode.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$SevenDayMode.UseVisualStyleBackColor = $false
$SevenDayMode.Cursor = [System.Windows.Forms.Cursors]::Hand
$SevenDayMode.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
$SevenDayMode.Location = New-Object System.Drawing.Point(0, 0)
$SevenDayMode.Size = New-Object System.Drawing.Size(116, 34)
$ModePanel.Controls.Add($SevenDayMode)

$SixMonthMode = New-Object System.Windows.Forms.Button
$SixMonthMode.Text = $Labels.rangeSixMonths
$SixMonthMode.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$SixMonthMode.UseVisualStyleBackColor = $false
$SixMonthMode.Cursor = [System.Windows.Forms.Cursors]::Hand
$SixMonthMode.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
$SixMonthMode.Location = New-Object System.Drawing.Point(116, 0)
$SixMonthMode.Size = New-Object System.Drawing.Size(136, 34)
$ModePanel.Controls.Add($SixMonthMode)

$StatusLabel = New-Object System.Windows.Forms.Label
$StatusLabel.Text = $Labels.idle
$StatusLabel.Location = New-Object System.Drawing.Point(31, 105)
$StatusLabel.Size = New-Object System.Drawing.Size(820, 28)
$Form.Controls.Add($StatusLabel)

$SevenDayMode.Add_Click({
    if (-not (Test-V5WorkerActive)) {
      $script:SelectedQueryMode = "seven_days"
      Update-ModeAppearance
      $StatusLabel.Text = "$($Labels.selectedRange): $($Labels.rangeSevenDays)"
    }
})
$SixMonthMode.Add_Click({
    if (-not (Test-V5WorkerActive)) {
      $script:SelectedQueryMode = "six_months"
      Update-ModeAppearance
      $StatusLabel.Text = "$($Labels.selectedRange): $($Labels.rangeSixMonths)"
    }
})
Update-ModeAppearance

$ProgressBar = New-Object System.Windows.Forms.ProgressBar
$ProgressBar.Location = New-Object System.Drawing.Point(32, 137)
$ProgressBar.Size = New-Object System.Drawing.Size(820, 20)
$ProgressBar.Style = "Continuous"
$Form.Controls.Add($ProgressBar)

$StageList = New-Object System.Windows.Forms.ListBox
$StageList.Location = New-Object System.Drawing.Point(32, 173)
$StageList.Size = New-Object System.Drawing.Size(820, 235)
$StageList.Font = New-Object System.Drawing.Font("Consolas", 10)
$StageList.BackColor = [System.Drawing.Color]::White
$StageList.BorderStyle = "FixedSingle"
$Form.Controls.Add($StageList)

$StartButton = New-Object System.Windows.Forms.Button
$StartButton.Text = $Labels.start
$StartButton.Location = New-Object System.Drawing.Point(32, 430)
$StartButton.Size = New-Object System.Drawing.Size(145, 40)
$StartButton.Add_Click({ Start-Worker })
$Form.Controls.Add($StartButton)

$ContinueButton = New-Object System.Windows.Forms.Button
$ContinueButton.Text = $Labels.continue
$ContinueButton.Location = New-Object System.Drawing.Point(187, 430)
$ContinueButton.Size = New-Object System.Drawing.Size(160, 40)
$ContinueButton.Enabled = $false
$ContinueButton.Add_Click({
    Set-Content -LiteralPath $LoginFlag -Value (Get-Date).ToString("o") -Encoding ASCII
    $ContinueButton.Enabled = $false
    $StatusLabel.Text = $Labels.continueSent
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 250
    $Form.WindowState = [System.Windows.Forms.FormWindowState]::Minimized
})
$Form.Controls.Add($ContinueButton)

$StopButton = New-Object System.Windows.Forms.Button
$StopButton.Text = $Labels.stop
$StopButton.Location = New-Object System.Drawing.Point(357, 430)
$StopButton.Size = New-Object System.Drawing.Size(90, 40)
$StopButton.Enabled = $false
$StopButton.Add_Click({ Set-Content -LiteralPath $StopFlag -Value (Get-Date).ToString("o") -Encoding ASCII })
$Form.Controls.Add($StopButton)

$ResumeButton = New-Object System.Windows.Forms.Button
$ResumeButton.Text = $Labels.resume
$ResumeButton.Location = New-Object System.Drawing.Point(457, 430)
$ResumeButton.Size = New-Object System.Drawing.Size(155, 40)
$ResumeButton.Enabled = $false
$ResumeButton.Add_Click({ Start-Worker -Resume })
$Form.Controls.Add($ResumeButton)

$OpenExcelButton = New-Object System.Windows.Forms.Button
$OpenExcelButton.Text = $Labels.openExcel
$OpenExcelButton.Location = New-Object System.Drawing.Point(32, 480)
$OpenExcelButton.Size = New-Object System.Drawing.Size(145, 35)
$OpenExcelButton.Enabled = $false
$OpenExcelButton.Add_Click({ if ($script:CurrentState.excel_path) { Invoke-Item -LiteralPath ([string]$script:CurrentState.excel_path) } })
$Form.Controls.Add($OpenExcelButton)

$OpenLogsButton = New-Object System.Windows.Forms.Button
$OpenLogsButton.Text = $Labels.openLogs
$OpenLogsButton.Location = New-Object System.Drawing.Point(187, 480)
$OpenLogsButton.Size = New-Object System.Drawing.Size(145, 35)
$OpenLogsButton.Enabled = $false
$OpenLogsButton.Add_Click({ if ($script:CurrentRunDir) { Invoke-Item -LiteralPath $script:CurrentRunDir } })
$Form.Controls.Add($OpenLogsButton)

$CloseButton = New-Object System.Windows.Forms.Button
$CloseButton.Text = $Labels.close
$CloseButton.Location = New-Object System.Drawing.Point(762, 480)
$CloseButton.Size = New-Object System.Drawing.Size(90, 35)
$CloseButton.Add_Click({ $Form.Close() })
$Form.Controls.Add($CloseButton)

$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = 800
$Timer.Add_Tick({ Refresh-Display })
$Timer.Start()
Refresh-Display
if ($SmokeTest) {
  $Form.Show()
  [System.Windows.Forms.Application]::DoEvents()
  $initialSeven = $script:SelectedQueryMode -eq "seven_days"
  $SixMonthMode.PerformClick()
  [System.Windows.Forms.Application]::DoEvents()
  $sixMonthClick = $script:SelectedQueryMode -eq "six_months"
  $SevenDayMode.PerformClick()
  [System.Windows.Forms.Application]::DoEvents()
  $sevenDayClick = $script:SelectedQueryMode -eq "seven_days"
  "V5_LAUNCHER_SMOKE_OK controls=$($Form.Controls.Count) stages=$($StageList.Items.Count) mode_enabled=$($ModePanel.Enabled) continue_enabled=$($ContinueButton.Enabled) initial_seven=$initialSeven click_six_month=$sixMonthClick click_seven_day=$sevenDayClick"
  $Form.Hide()
  $Timer.Stop()
  $Form.Dispose()
  exit 0
}
[void]$Form.ShowDialog()
$Timer.Stop()
