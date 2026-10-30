param(
  [string]$ErpExePath = "E:\MIS\GI_System.exe",
  [int]$LoginWaitTimeoutSeconds = 180,
  [int]$OrderScreenTimeoutSeconds = 60,
  [switch]$EnableFixedPositionFallback,
  [int]$QueryDialogTimeoutSeconds = 20,
  [switch]$FullSixMonths,
  [int]$ShortRangeDays = 7,
  [int]$MinimumWaitSeconds = 2,
  [int]$MaximumWaitSeconds = 5,
  [int]$MaximumOrders = 2000,
  [switch]$ResumeLatest,
  [string]$BatchesRootOverride = "",
  [string]$StopFlagPathOverride = "",
  [string]$LoginContinueFlagPath = "",
  [string]$ManualMaximizeRequestPath = "",
  [string]$ManualMaximizeContinueFlagPath = "",
  [int]$ManualMaximizeTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$BuildVersion = "V6-PHASE3-R4.1-DUAL-R2-20260825-first-capture-fallback"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root "capture_guard_helpers.ps1")
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BatchesRoot = if ([string]::IsNullOrWhiteSpace($BatchesRootOverride)) {
  Join-Path $Root "batches"
} else {
  [System.IO.Path]::GetFullPath($BatchesRootOverride)
}
$StopFlagPath = if ([string]::IsNullOrWhiteSpace($StopFlagPathOverride)) {
  Join-Path $Root "STOP_V4_NOW.flag"
} else {
  [System.IO.Path]::GetFullPath($StopFlagPathOverride)
}
$ManualMaximizeRequestPath = if ([string]::IsNullOrWhiteSpace($ManualMaximizeRequestPath)) {
  Join-Path $Root "MANUAL_MAXIMIZE_REQUIRED.flag"
} else {
  [System.IO.Path]::GetFullPath($ManualMaximizeRequestPath)
}
$ManualMaximizeContinueFlagPath = if ([string]::IsNullOrWhiteSpace($ManualMaximizeContinueFlagPath)) {
  Join-Path $Root "MANUAL_MAXIMIZE_CONTINUE.flag"
} else {
  [System.IO.Path]::GetFullPath($ManualMaximizeContinueFlagPath)
}
if ($ManualMaximizeTimeoutSeconds -lt 30) { $ManualMaximizeTimeoutSeconds = 30 }
New-Item -ItemType Directory -Force -Path $BatchesRoot | Out-Null

if ($MinimumWaitSeconds -lt 2) { $MinimumWaitSeconds = 2 }
if ($MaximumWaitSeconds -lt $MinimumWaitSeconds) { $MaximumWaitSeconds = $MinimumWaitSeconds }
if ($MaximumWaitSeconds -gt 5) { $MaximumWaitSeconds = 5 }
if ($MaximumOrders -lt 1) { $MaximumOrders = 1 }
if ($MaximumOrders -gt 2000) { $MaximumOrders = 2000 }
if ($ShortRangeDays -lt 1) { $ShortRangeDays = 1 }

$resumeState = $null
$IsResume = $false
$BatchStatePath = $null
$LegacyStatePath = $null
if ($ResumeLatest) {
  $latestBatch = Get-ChildItem -LiteralPath $BatchesRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object {
      (Test-Path -LiteralPath (Join-Path $_.FullName "batch_state.json")) -or
      (Test-Path -LiteralPath (Join-Path $_.FullName "state.json"))
    } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  if ($null -eq $latestBatch) { throw "No resumable V4 batch was found." }
  $BatchDir = $latestBatch.FullName
  $BatchStatePath = Join-Path $BatchDir "batch_state.json"
  $LegacyStatePath = Join-Path $BatchDir "state.json"
  $StatePath = if (Test-Path -LiteralPath $BatchStatePath) { $BatchStatePath } else { $LegacyStatePath }
  $resumeState = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
  $DateStart = [string]$resumeState.date_start
  $DateEnd = [string]$resumeState.date_end
  $RunMode = [string]$resumeState.run_mode
  $IsResume = $true
} else {
  $today = Get-Date
  if ($FullSixMonths) {
    $startDate = $today.AddMonths(-6)
    $RunMode = "six_months"
  } else {
    $startDate = $today.AddDays(-1 * ($ShortRangeDays - 1))
    $RunMode = "short_${ShortRangeDays}_days"
  }
  $DateStart = $startDate.ToString("yyyy/MM/dd")
  $DateEnd = $today.ToString("yyyy/MM/dd")
  $BatchDir = Join-Path $BatchesRoot ("batch_{0}_{1}" -f $Stamp, $RunMode)
  New-Item -ItemType Directory -Path $BatchDir -Force | Out-Null
  $BatchStatePath = Join-Path $BatchDir "batch_state.json"
  $LegacyStatePath = Join-Path $BatchDir "state.json"
  $StatePath = $BatchStatePath
}

$DateStartInput = $DateStart -replace '[^0-9]', ''
$DateEndInput = $DateEnd -replace '[^0-9]', ''
$OrderPrefix = "GOU"
$CaptureDir = Join-Path $BatchDir "captures"
$LogDir = Join-Path $BatchDir "logs"
New-Item -ItemType Directory -Force -Path $CaptureDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$LogPath = Join-Path $LogDir "v4_run_$Stamp.log"
$StepPath = Join-Path $LogDir "v4_steps_$Stamp.csv"
$SummaryPath = Join-Path $BatchDir "summary.txt"
$ErrorPath = Join-Path $LogDir "v4_error_$Stamp.txt"
$OrderStatusPath = Join-Path $BatchDir "order_status.csv"
$StepRecords = New-Object System.Collections.Generic.List[object]

if ($IsResume) {
  if ($resumeState.order_prefix -ne $OrderPrefix) {
    throw "Resume query condition mismatch: expected order prefix $OrderPrefix, found $($resumeState.order_prefix)."
  }
  if ([string]::IsNullOrWhiteSpace([string]$resumeState.date_start) -or [string]$resumeState.date_start -notmatch '^\d{4}/\d{2}/\d{2}$') {
    throw "Resume batch has an invalid start date condition."
  }
  if ([string]::IsNullOrWhiteSpace([string]$resumeState.date_end) -or [string]$resumeState.date_end -notmatch '^\d{4}/\d{2}/\d{2}$') {
    throw "Resume batch has an invalid end date condition."
  }
  if ([string]$resumeState.status -in @("completed", "completed_limit")) {
    throw "The latest V4 batch is already complete and does not need to resume."
  }
}

function New-Chars {
  param([int[]]$Codes)
  return [string]::Concat(($Codes | ForEach-Object { [char]$_ }))
}

$SystemTitleKeyword = New-Chars @(0x9032,0x92B7,0x5B58,0x7CFB,0x7D71)
$OrderTitleKeyword = New-Chars @(0x5916,0x5546,0x79AE,0x76D2,0x8A02,0x55AE,0x767B,0x9304)
$SystemMenuText = New-Chars @(0x7CFB,0x7D71,0x8A2D,0x5B9A)
$InventorySystemText = New-Chars @(0x9032,0x92B7,0x5B58,0x7CFB,0x7D71)
$ForeignMenuText = New-Chars @(0x5916,0x5546,0x7BA1,0x7406)
$OrderEntryText = $OrderTitleKeyword
$QueryText = New-Chars @(0x67E5,0x8A62)
$OkText = New-Chars @(0x78BA,0x5B9A)
$script:ErpProcessId = [uint32]0

function Write-Log {
  param([string]$Message)
  $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
  $written = $false
  for ($attempt = 1; $attempt -le 10; $attempt++) {
    try {
      Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
      $written = $true
      break
    } catch {
      Start-Sleep -Milliseconds (40 * $attempt)
    }
  }
  if (-not $written) { Write-Warning "Log file was temporarily locked; message was not persisted: $line" }
  Write-Host $line
}

function Add-StepRecord {
  param(
    [string]$Step,
    [string]$Status,
    [datetime]$StartedAt,
    [datetime]$FinishedAt,
    [string]$Reason = ""
  )

  $record = [pscustomobject]@{
    step = $Step
    status = $Status
    started_at = $StartedAt.ToString("yyyy-MM-dd HH:mm:ss.fff")
    finished_at = $FinishedAt.ToString("yyyy-MM-dd HH:mm:ss.fff")
    duration_seconds = [Math]::Round(($FinishedAt - $StartedAt).TotalSeconds, 2)
    reason = $Reason
  }
  $script:StepRecords.Add($record)
  $script:StepRecords | Export-Csv -LiteralPath $StepPath -NoTypeInformation -Encoding UTF8
}

function Invoke-Step {
  param(
    [string]$Name,
    [scriptblock]$Action
  )

  $started = Get-Date
  Write-Log "STEP START: $Name"
  try {
    $result = & $Action
    $finished = Get-Date
    Add-StepRecord -Step $Name -Status "success" -StartedAt $started -FinishedAt $finished -Reason ([string]$result)
    Write-Log "STEP OK: $Name $result"
    return $result
  } catch {
    $finished = Get-Date
    $reason = $_.Exception.Message
    Add-StepRecord -Step $Name -Status "failed" -StartedAt $started -FinishedAt $finished -Reason $reason
    Write-Log "STEP FAIL: $Name $reason"
    throw
  }
}

function Stop-Test {
  param([string]$Reason, [int]$Code = 1)
  Write-Log "STOP: $Reason"
  Set-Content -LiteralPath $ErrorPath -Value $Reason -Encoding UTF8
  Set-Content -LiteralPath $SummaryPath -Value @(
    "status=failed"
    "reason=$Reason"
    "steps_csv=$(Get-PortableBatchPath -Path $StepPath)"
    "log=$(Get-PortableBatchPath -Path $LogPath)"
    "time=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
  ) -Encoding UTF8
  exit $Code
}

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;

public class ErpV35Native {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

  [DllImport("user32.dll")]
  public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

  [DllImport("user32.dll")]
  public static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowsProc lpEnumFunc, IntPtr lParam);

  [DllImport("user32.dll")]
  public static extern bool IsWindowVisible(IntPtr hWnd);

  [DllImport("user32.dll")]
  public static extern bool IsWindow(IntPtr hWnd);

  [DllImport("user32.dll")]
  public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

  [DllImport("user32.dll")]
  public static extern int GetWindowTextLength(IntPtr hWnd);

  [DllImport("user32.dll")]
  public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr hWnd);

  [DllImport("user32.dll")]
  public static extern bool BringWindowToTop(IntPtr hWnd);

  [DllImport("user32.dll")]
  public static extern IntPtr GetForegroundWindow();

  [DllImport("user32.dll")]
  public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

  [DllImport("user32.dll")]
  public static extern IntPtr GetParent(IntPtr hWnd);

  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

  [DllImport("user32.dll")]
  public static extern bool IsZoomed(IntPtr hWnd);

  [DllImport("user32.dll")]
  public static extern int GetSystemMetrics(int nIndex);

  [DllImport("user32.dll", EntryPoint = "SendMessageW")]
  public static extern IntPtr SendMessagePtr(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

  [DllImport("user32.dll")]
  public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

  [DllImport("user32.dll")]
  public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

  [DllImport("user32.dll")]
  public static extern IntPtr SetFocus(IntPtr hWnd);

  [DllImport("user32.dll")]
  public static extern IntPtr GetFocus();

  [DllImport("kernel32.dll")]
  public static extern uint GetCurrentThreadId();

  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  public static extern bool MoveFileEx(string existingFileName, string newFileName, uint flags);

  [DllImport("user32.dll")]
  public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint nFlags);

  [DllImport("user32.dll")]
  public static extern IntPtr GetWindowDC(IntPtr hWnd);

  [DllImport("user32.dll")]
  public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);

  [DllImport("user32.dll")]
  public static extern bool SetCursorPos(int X, int Y);

  [DllImport("user32.dll")]
  public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);

  [DllImport("user32.dll")]
  public static extern IntPtr WindowFromPoint(POINT point);

  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, string lParam);

  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern IntPtr LoadKeyboardLayout(string pwszKLID, uint Flags);

  [DllImport("user32.dll")]
  public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

  [DllImport("imm32.dll")]
  public static extern IntPtr ImmGetContext(IntPtr hWnd);

  [DllImport("imm32.dll")]
  public static extern bool ImmSetOpenStatus(IntPtr hIMC, bool fOpen);

  [DllImport("imm32.dll")]
  public static extern bool ImmReleaseContext(IntPtr hWnd, IntPtr hIMC);

  [DllImport("gdi32.dll")]
  public static extern bool BitBlt(IntPtr hdcDest, int xDest, int yDest, int wDest, int hDest, IntPtr hdcSrc, int xSrc, int ySrc, int rop);

  public const int SW_MAXIMIZE = 3;
  public const int SW_RESTORE = 9;
  public const int SRCCOPY = 0x00CC0020;
  public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
  public const uint MOUSEEVENTF_LEFTUP = 0x0004;
  public const uint WM_SETTEXT = 0x000C;
  public const uint WM_INPUTLANGCHANGEREQUEST = 0x0050;
  public const uint WM_SYSCOMMAND = 0x0112;
  public const uint WM_MDIMAXIMIZE = 0x0225;
  public const uint WM_KEYDOWN = 0x0100;
  public const uint WM_KEYUP = 0x0101;
  public const uint SC_MAXIMIZE = 0xF030;
  public const uint SC_RESTORE = 0xF120;
  public const uint KLF_ACTIVATE = 0x00000001;
  public const int SM_CXSIZE = 30;
  public const int SM_CYSIZE = 31;
  public const int VK_DOWN = 0x28;
  public const int VK_HOME = 0x24;

  [StructLayout(LayoutKind.Sequential)]
  public struct POINT {
    public int X;
    public int Y;
  }

  [StructLayout(LayoutKind.Sequential)]
  public struct RECT {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
  }
}
'@

function Get-WindowTitle {
  param([IntPtr]$Handle)
  $length = [ErpV35Native]::GetWindowTextLength($Handle)
  if ($length -le 0) { return "" }
  $builder = New-Object System.Text.StringBuilder ($length + 1)
  [void][ErpV35Native]::GetWindowText($Handle, $builder, $builder.Capacity)
  return $builder.ToString()
}

function Get-WindowProcessId {
  param([IntPtr]$Handle)
  $processId = [uint32]0
  [void][ErpV35Native]::GetWindowThreadProcessId($Handle, [ref]$processId)
  return $processId
}

function Get-WindowThreadId {
  param([IntPtr]$Handle)
  $processId = [uint32]0
  return [ErpV35Native]::GetWindowThreadProcessId($Handle, [ref]$processId)
}

function Write-ErpProcessDiagnostic {
  param([object]$Window)
  try {
    $process = Get-Process -Id ([int]$Window.ProcessId) -ErrorAction Stop
    Write-Log (
      "ERP PROCESS pid={0} name=[{1}] main_handle={2} observed_handle={3} title=[{4}] class=[{5}]" -f
      $process.Id, $process.ProcessName, $process.MainWindowHandle,
      $Window.Handle.ToInt64(), $Window.Title,
      (Get-WindowClassName -Handle $Window.Handle)
    )
  } catch {
    Write-Log "ERP process diagnostic warning: pid=$($Window.ProcessId) handle=$($Window.Handle) title=[$($Window.Title)] error=$($_.Exception.Message)"
  }
}

function Get-WindowClassName {
  param([IntPtr]$Handle)
  $builder = New-Object System.Text.StringBuilder 512
  [void][ErpV35Native]::GetClassName($Handle, $builder, $builder.Capacity)
  return $builder.ToString()
}

function Get-WindowDescriptor {
  param([IntPtr]$Handle, [int]$Level)
  $rect = New-Object ErpV35Native+RECT
  [void][ErpV35Native]::GetWindowRect($Handle, [ref]$rect)
  return [pscustomobject]@{
    Level = $Level
    Handle = $Handle
    HandleValue = $Handle.ToInt64()
    Parent = [ErpV35Native]::GetParent($Handle)
    ProcessId = Get-WindowProcessId -Handle $Handle
    ClassName = Get-WindowClassName -Handle $Handle
    Title = Get-WindowTitle -Handle $Handle
    Visible = [ErpV35Native]::IsWindowVisible($Handle)
    Zoomed = [ErpV35Native]::IsZoomed($Handle)
    Left = $rect.Left
    Top = $rect.Top
    Width = $rect.Right - $rect.Left
    Height = $rect.Bottom - $rect.Top
  }
}

function Get-WindowParentHierarchy {
  param([IntPtr]$Handle)
  $items = New-Object System.Collections.Generic.List[object]
  $seen = @{}
  $current = $Handle
  for ($level = 0; $level -lt 32 -and $current -ne [IntPtr]::Zero; $level++) {
    $key = $current.ToInt64().ToString()
    if ($seen.ContainsKey($key)) { break }
    $seen[$key] = $true
    $item = Get-WindowDescriptor -Handle $current -Level $level
    $items.Add($item)
    $current = $item.Parent
  }
  return $items
}

function Write-WindowParentHierarchy {
  param([IntPtr]$Handle)
  $hierarchy = @(Get-WindowParentHierarchy -Handle $Handle)
  foreach ($item in $hierarchy) {
    Write-Log (
      "PARENT level={0} handle={1} parent={2} pid={3} class=[{4}] title=[{5}] " +
      "visible={6} zoomed={7} rect={8}x{9} at {10},{11}" -f
      $item.Level, $item.HandleValue, $item.Parent.ToInt64(), $item.ProcessId,
      $item.ClassName, $item.Title, $item.Visible, $item.Zoomed,
      $item.Width, $item.Height, $item.Left, $item.Top
    )
  }
  return $hierarchy
}

function Find-MdiClientAncestor {
  param([object[]]$Hierarchy)
  return @($Hierarchy | Where-Object { $_.ClassName -match '(?i)MDICLIENT' } |
    Sort-Object Level | Select-Object -First 1)[0]
}

function Get-VisibleWindows {
  $items = New-Object System.Collections.Generic.List[object]
  $callback = [ErpV35Native+EnumWindowsProc]{
    param([IntPtr]$hWnd, [IntPtr]$lParam)
    if (-not [ErpV35Native]::IsWindowVisible($hWnd)) { return $true }
    $title = Get-WindowTitle -Handle $hWnd
    if ([string]::IsNullOrWhiteSpace($title)) { return $true }
    if ($title -match "Chrome|Google Chrome|Remote Desktop|Chrome Remote Desktop") { return $true }
    $rect = New-Object ErpV35Native+RECT
    [void][ErpV35Native]::GetWindowRect($hWnd, [ref]$rect)
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    if ($width -lt 250 -or $height -lt 120) { return $true }
    $items.Add([pscustomobject]@{
      Handle = $hWnd
      ProcessId = Get-WindowProcessId -Handle $hWnd
      Title = $title
      Left = $rect.Left
      Top = $rect.Top
      Width = $width
      Height = $height
      Area = $width * $height
      IsSystemWindow = ($title -like "*$SystemTitleKeyword*")
      IsOrderWindow = ($title -like "*$OrderTitleKeyword*")
    })
    return $true
  }
  [void][ErpV35Native]::EnumWindows($callback, [IntPtr]::Zero)
  return $items
}

function Get-VisibleChildWindows {
  param([IntPtr]$ParentHandle)

  $items = New-Object System.Collections.Generic.List[object]
  $callback = [ErpV35Native+EnumWindowsProc]{
    param([IntPtr]$hWnd, [IntPtr]$lParam)
    if (-not [ErpV35Native]::IsWindowVisible($hWnd)) { return $true }
    $title = Get-WindowTitle -Handle $hWnd
    if ([string]::IsNullOrWhiteSpace($title)) { return $true }
    $rect = New-Object ErpV35Native+RECT
    [void][ErpV35Native]::GetWindowRect($hWnd, [ref]$rect)
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    if ($width -lt 400 -or $height -lt 250) { return $true }
    $items.Add([pscustomobject]@{
      Handle = $hWnd
      ProcessId = Get-WindowProcessId -Handle $hWnd
      Title = $title
      Left = $rect.Left
      Top = $rect.Top
      Width = $width
      Height = $height
      Area = $width * $height
      ZOrder = $items.Count
      IsOrderWindow = ($title -like "*$OrderTitleKeyword*")
    })
    return $true
  }
  [void][ErpV35Native]::EnumChildWindows($ParentHandle, $callback, [IntPtr]::Zero)
  return $items
}

function Wait-ForOrderChildWindow {
  param(
    [IntPtr]$ParentHandle,
    [int]$TimeoutSeconds
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $children = @(Get-VisibleChildWindows -ParentHandle $ParentHandle)
    # EnumChildWindows returns children in Z order. The first matching child is
    # the visible order screen, even if an older MDI child remains behind it.
    $order = @($children | Where-Object { $_.IsOrderWindow } | Sort-Object ZOrder)
    if ($order.Count -gt 0) {
      Write-Log "Order child window: [$($order[0].Title)] $($order[0].Width)x$($order[0].Height) at $($order[0].Left),$($order[0].Top)"
      return $order[0]
    }
    Start-Sleep -Milliseconds 300
  }
  return $null
}

function Find-ErpWindow {
  $windows = @(Get-VisibleWindows)
  foreach ($window in $windows) {
    Write-Log "Window: [$($window.Title)] $($window.Width)x$($window.Height) at $($window.Left),$($window.Top)"
  }
  $order = @($windows | Where-Object { $_.IsOrderWindow } | Sort-Object Area -Descending)
  if ($order.Count -gt 0) { return $order[0] }
  $system = @($windows | Where-Object { $_.IsSystemWindow } | Sort-Object Area -Descending)
  if ($system.Count -gt 0) { return $system[0] }
  return $null
}

function Activate-Window {
  param([IntPtr]$Handle)
  $targetProcessId = Get-WindowProcessId -Handle $Handle
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    [void][ErpV35Native]::ShowWindow($Handle, [ErpV35Native]::SW_MAXIMIZE)
    [void][ErpV35Native]::BringWindowToTop($Handle)

    # Sending a harmless ALT key lets Windows transfer foreground ownership
    # after the operator presses Continue in the launcher.
    try { [System.Windows.Forms.SendKeys]::SendWait("%") } catch {}
    [void][ErpV35Native]::SetForegroundWindow($Handle)
    Start-Sleep -Milliseconds (400 + ($attempt * 300))

    $foregroundHandle = [ErpV35Native]::GetForegroundWindow()
    if ($foregroundHandle -eq $Handle) {
      Write-Log "Activation succeeded on attempt $attempt."
      return $true
    }

    $foregroundProcessId = Get-WindowProcessId -Handle $foregroundHandle
    if ($targetProcessId -ne 0 -and $targetProcessId -eq $foregroundProcessId) {
      Write-Log "Activation accepted on attempt ${attempt}: foreground window belongs to ERP process $targetProcessId."
      return $true
    }

    $foregroundTitle = Get-WindowTitle -Handle $foregroundHandle
    Write-Log "Activation retry ${attempt}: target_pid=$targetProcessId foreground_pid=$foregroundProcessId foreground_title=[$foregroundTitle]"
  }

  $foregroundHandle = [ErpV35Native]::GetForegroundWindow()
  $foregroundProcessId = Get-WindowProcessId -Handle $foregroundHandle
  $foregroundTitle = Get-WindowTitle -Handle $foregroundHandle
  Write-Log "Activation failed: target_pid=$targetProcessId foreground_pid=$foregroundProcessId foreground_title=[$foregroundTitle]"
  return $false
}

function Save-WindowPng {
  param(
    [IntPtr]$Handle,
    [string]$Path
  )

  $rect = New-Object ErpV35Native+RECT
  [void][ErpV35Native]::GetWindowRect($Handle, [ref]$rect)
  $width = $rect.Right - $rect.Left
  $height = $rect.Bottom - $rect.Top
  if ($width -lt 900 -or $height -lt 500) {
    throw "Selected ERP window is too small for a complete order screenshot. Size=${width}x${height}."
  }

  $bitmap = New-Object System.Drawing.Bitmap $width, $height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  $hdc = $graphics.GetHdc()
  $printed = $false
  try {
    $printed = [ErpV35Native]::PrintWindow($Handle, $hdc, 2)
  } finally {
    $graphics.ReleaseHdc($hdc)
    $graphics.Dispose()
  }

  if (-not $printed) {
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $destHdc = $graphics.GetHdc()
    $srcHdc = [ErpV35Native]::GetWindowDC($Handle)
    try {
      [void][ErpV35Native]::BitBlt($destHdc, 0, 0, $width, $height, $srcHdc, 0, 0, [ErpV35Native]::SRCCOPY)
    } finally {
      [void][ErpV35Native]::ReleaseDC($Handle, $srcHdc)
      $graphics.ReleaseHdc($destHdc)
      $graphics.Dispose()
    }
  }

  $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
  $bitmap.Dispose()
  return "${width}x${height}"
}

function Get-AutomationRoot {
  param([IntPtr]$Handle)
  return [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
}

function Find-ElementByName {
  param(
    [System.Windows.Automation.AutomationElement]$RootElement,
    [string]$Name,
    [int]$TimeoutSeconds = 8
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  $condition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty,
    $Name
  )
  while ((Get-Date) -lt $deadline) {
    $found = $RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
    if ($null -ne $found) { return $found }
    Start-Sleep -Milliseconds 300
  }
  return $null
}

function Invoke-UiaElement {
  param(
    [System.Windows.Automation.AutomationElement]$Element,
    [string]$Name
  )

  if ($null -eq $Element) { throw "Target not found: $Name" }
  try {
    $pattern = $Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $pattern.Invoke()
    Start-Sleep -Milliseconds 800
    return "uia_invoke:$Name"
  } catch {
    try {
      $point = $Element.GetClickablePoint()
      [ErpV35Native]::SetCursorPos([int]$point.X, [int]$point.Y) | Out-Null
      Start-Sleep -Milliseconds 100
      [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
      [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 800
      return "uia_click:$Name"
    } catch {
      throw "Target exists but cannot be invoked: $Name"
    }
  }
}

function Test-UnknownPopup {
  $foreground = [ErpV35Native]::GetForegroundWindow()
  if ($foreground -eq [IntPtr]::Zero) { return }
  $title = Get-WindowTitle -Handle $foreground
  if ([string]::IsNullOrWhiteSpace($title)) { return }
  if ($title -like "*$SystemTitleKeyword*" -or $title -like "*$OrderTitleKeyword*") { return }
  if ($title -like "*$QueryText*") { return }
  if ($title -match "Windows PowerShell|Command Prompt|cmd.exe") { return }

  $foregroundProcessId = Get-WindowProcessId -Handle $foreground
  if ($script:ErpProcessId -gt 0 -and $foregroundProcessId -ne $script:ErpProcessId) {
    Write-Log "Ignoring unrelated foreground window: [$title] pid=$foregroundProcessId"
    return
  }
  throw "Unknown foreground window or popup detected: [$title]"
}

function Assert-ConfirmedDesktopLayout {
  $screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  if ($screen.Width -ne 1920 -or $screen.Height -ne 1080) {
    throw "Confirmed ERP navigation requires 1920x1080 display. Current=$($screen.Width)x$($screen.Height)."
  }
  return "1920x1080"
}

function Invoke-ScreenClick {
  param(
    [int]$X,
    [int]$Y,
    [string]$Name,
    [int]$WaitMilliseconds = 800
  )

  Write-Log "Click target=$Name screen=$X,$Y"
  [void][ErpV35Native]::SetCursorPos($X, $Y)
  Start-Sleep -Milliseconds 150
  [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
  [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds $WaitMilliseconds
}

function Invoke-RelativeClick {
  param(
    [object]$Window,
    [int]$X,
    [int]$Y,
    [string]$Name
  )

  $screenX = [int]($Window.Left + $X)
  $screenY = [int]($Window.Top + $Y)
  Write-Log "Click target=$Name relative=$X,$Y screen=$screenX,$screenY"
  [void][ErpV35Native]::SetCursorPos($screenX, $screenY)
  Start-Sleep -Milliseconds 150
  [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
  [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 800
}

function Invoke-ConfirmedTopMenuPath {
  param([object]$MainWindow)

  [void](Assert-ConfirmedDesktopLayout)
  if (-not (Activate-Window -Handle $MainWindow.Handle)) {
    throw "ERP main window could not be activated before confirmed top-menu navigation."
  }

  $steps = @(
    @{ Name = "system settings menu"; X = 39; Y = 34; Wait = 350 },
    @{ Name = "inventory system item"; X = 75; Y = 107; Wait = 1600 },
    @{ Name = "foreign management menu"; X = 278; Y = 34; Wait = 350 },
    @{ Name = "external gift order entry item"; X = 350; Y = 57; Wait = 1800 }
  )

  foreach ($step in $steps) {
    Invoke-ScreenClick -X $step.X -Y $step.Y -Name $step.Name -WaitMilliseconds $step.Wait
    Test-UnknownPopup
  }
  return "system_settings>inventory_system>foreign_management>external_gift_order_entry"
}

function Wait-ForWindowClosed {
  param(
    [IntPtr]$Handle,
    [int]$TimeoutSeconds
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (-not [ErpV35Native]::IsWindow($Handle)) { return $true }
    $title = Get-WindowTitle -Handle $Handle
    if ([string]::IsNullOrWhiteSpace($title)) { return $true }
    Start-Sleep -Milliseconds 300
  }
  return $false
}

function Maximize-OrderChildWindow {
  param([object]$OrderChild)

  $minimumWidth = 1700
  $minimumHeight = 800
  $hierarchy = @(Write-WindowParentHierarchy -Handle $OrderChild.Handle)
  $mdiClient = Find-MdiClientAncestor -Hierarchy $hierarchy
  if ($null -eq $mdiClient) {
    Write-Log "No MDIClient class was found in the order child parent hierarchy."
  } else {
    Write-Log "Confirmed MDIClient ancestor: handle=$($mdiClient.HandleValue) level=$($mdiClient.Level) rect=$($mdiClient.Width)x$($mdiClient.Height)."
  }

  function Test-MaximizedSize {
    $rect = New-Object ErpV35Native+RECT
    [void][ErpV35Native]::GetWindowRect($OrderChild.Handle, [ref]$rect)
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    return [pscustomobject]@{
      Passed = ($width -ge $minimumWidth -and $height -ge $minimumHeight)
      Width = $width
      Height = $height
      Left = $rect.Left
      Top = $rect.Top
      Right = $rect.Right
      Bottom = $rect.Bottom
      Zoomed = [ErpV35Native]::IsZoomed($OrderChild.Handle)
    }
  }

  function Confirm-Attempt {
    param([int]$Attempt, [string]$Method)
    Start-Sleep -Milliseconds 1200
    $size = Test-MaximizedSize
    Write-Log "Order child maximize attempt ${Attempt} method=$Method result=$($size.Width)x$($size.Height) at $($size.Left),$($size.Top) zoomed=$($size.Zoomed)"
    return $size
  }

  Write-Log "Maximize order child attempt 1: ShowWindow(SW_MAXIMIZE)."
  [void][ErpV35Native]::ShowWindow($OrderChild.Handle, [ErpV35Native]::SW_MAXIMIZE)
  $size = Confirm-Attempt -Attempt 1 -Method "ShowWindow"
  if ($size.Passed) { return "title=$($OrderChild.Title) size=$($size.Width)x$($size.Height) attempt=1" }

  if ($null -ne $mdiClient) {
    Write-Log "Maximize order child attempt 2: WM_MDIMAXIMIZE to confirmed MDIClient."
    [void][ErpV35Native]::SendMessagePtr(
      $mdiClient.Handle,
      [ErpV35Native]::WM_MDIMAXIMIZE,
      $OrderChild.Handle,
      [IntPtr]::Zero
    )
    $size = Confirm-Attempt -Attempt 2 -Method "WM_MDIMAXIMIZE"
    if ($size.Passed) { return "title=$($OrderChild.Title) size=$($size.Width)x$($size.Height) attempt=2" }
  } else {
    Write-Log "Maximize order child attempt 2 skipped: confirmed MDIClient not found."
  }

  Write-Log "Maximize order child attempt 3: WM_SYSCOMMAND + SC_MAXIMIZE."
  [void][ErpV35Native]::SendMessagePtr(
    $OrderChild.Handle,
    [ErpV35Native]::WM_SYSCOMMAND,
    [IntPtr][ErpV35Native]::SC_MAXIMIZE,
    [IntPtr]::Zero
  )
  $size = Confirm-Attempt -Attempt 3 -Method "WM_SYSCOMMAND_SC_MAXIMIZE"
  if ($size.Passed) { return "title=$($OrderChild.Title) size=$($size.Width)x$($size.Height) attempt=3" }

  if ($size.Zoomed) {
    Write-Log "Order child reports zoomed but is below the required size; restoring before title-bar double click."
    [void][ErpV35Native]::SendMessagePtr(
      $OrderChild.Handle,
      [ErpV35Native]::WM_SYSCOMMAND,
      [IntPtr][ErpV35Native]::SC_RESTORE,
      [IntPtr]::Zero
    )
    Start-Sleep -Milliseconds 800
    $size = Test-MaximizedSize
  }
  $titleX = [int](($size.Left + $size.Right) / 2)
  $buttonHeight = [Math]::Max(18, [ErpV35Native]::GetSystemMetrics([ErpV35Native]::SM_CYSIZE))
  $titleY = [int]($size.Top + [Math]::Round($buttonHeight / 2))
  Write-Log "Maximize order child attempt 4: double-click title bar center at $titleX,$titleY."
  [void][ErpV35Native]::SetCursorPos($titleX, $titleY)
  Start-Sleep -Milliseconds 150
  for ($click = 1; $click -le 2; $click++) {
    [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
    [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 120
  }
  $size = Confirm-Attempt -Attempt 4 -Method "TITLE_BAR_DOUBLE_CLICK"
  if ($size.Passed) { return "title=$($OrderChild.Title) size=$($size.Width)x$($size.Height) attempt=4" }

  if ($size.Zoomed) {
    Write-Log "Order child still reports zoomed below the required size; restoring before maximize-button click."
    [void][ErpV35Native]::ShowWindow($OrderChild.Handle, [ErpV35Native]::SW_RESTORE)
    Start-Sleep -Milliseconds 800
    $size = Test-MaximizedSize
  }
  $buttonWidth = [Math]::Max(18, [ErpV35Native]::GetSystemMetrics([ErpV35Native]::SM_CXSIZE))
  $buttonHeight = [Math]::Max(18, [ErpV35Native]::GetSystemMetrics([ErpV35Native]::SM_CYSIZE))
  $clickX = [int]($size.Right - [Math]::Round($buttonWidth * 1.5))
  $clickY = [int]($size.Top + [Math]::Round($buttonHeight / 2))
  Write-Log "Maximize order child attempt 5: calculated maximize button at $clickX,$clickY button=${buttonWidth}x${buttonHeight} rect=$($size.Width)x$($size.Height)."
  [void][ErpV35Native]::SetCursorPos($clickX, $clickY)
  Start-Sleep -Milliseconds 150
  [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
  [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
  $size = Confirm-Attempt -Attempt 5 -Method "CALCULATED_MAXIMIZE_BUTTON"
  if ($size.Passed) { return "title=$($OrderChild.Title) size=$($size.Width)x$($size.Height) attempt=5" }

  $message = "Please manually maximize the ERP order child window."
  Write-Log ("MANUAL ACTION REQUIRED: {0}. Current={1}x{2}; required={3}x{4}." -f
    $message, $size.Width, $size.Height, $minimumWidth, $minimumHeight)
  New-Item -ItemType Directory -Force -Path (Split-Path $ManualMaximizeRequestPath) | Out-Null
  Set-Content -LiteralPath $ManualMaximizeRequestPath -Value $message -Encoding UTF8
  Remove-Item -LiteralPath $ManualMaximizeContinueFlagPath -Force -ErrorAction SilentlyContinue
  $deadline = (Get-Date).AddSeconds($ManualMaximizeTimeoutSeconds)
  while (-not (Test-Path -LiteralPath $ManualMaximizeContinueFlagPath)) {
    if (Test-StopRequested) { throw "Stop requested while waiting for manual ERP order-window maximize." }
    if ((Get-Date) -ge $deadline) {
      throw "Timed out waiting for manual ERP order-window maximize after $ManualMaximizeTimeoutSeconds seconds."
    }
    Start-Sleep -Milliseconds 400
  }
  Remove-Item -LiteralPath $ManualMaximizeContinueFlagPath -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $ManualMaximizeRequestPath -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 600
  $size = Test-MaximizedSize
  Write-Log "Manual maximize verification: $($size.Width)x$($size.Height) at $($size.Left),$($size.Top) zoomed=$($size.Zoomed)."
  if (-not $size.Passed) {
    throw "ERP order child is still below the required size after manual confirmation. Size=$($size.Width)x$($size.Height); required=${minimumWidth}x${minimumHeight}."
  }
  return "title=$($OrderChild.Title) size=$($size.Width)x$($size.Height) method=manual_verified"
}

function Get-ForegroundWindowInfo {
  $handle = [ErpV35Native]::GetForegroundWindow()
  if ($handle -eq [IntPtr]::Zero) { return $null }
  $title = Get-WindowTitle -Handle $handle
  $rect = New-Object ErpV35Native+RECT
  [void][ErpV35Native]::GetWindowRect($handle, [ref]$rect)
  return [pscustomobject]@{
    Handle = $handle
    ProcessId = Get-WindowProcessId -Handle $handle
    Title = $title
    Left = $rect.Left
    Top = $rect.Top
    Width = $rect.Right - $rect.Left
    Height = $rect.Bottom - $rect.Top
  }
}

function Wait-ForQueryDialog {
  param(
    [IntPtr]$OrderHandle,
    [int]$TimeoutSeconds
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $foreground = Get-ForegroundWindowInfo
    if (
      $null -ne $foreground -and
      $foreground.Handle -ne $OrderHandle -and
      $foreground.ProcessId -eq $script:ErpProcessId -and
      $foreground.Title -like "*$QueryText*" -and
      $foreground.Width -gt 250 -and
      $foreground.Height -gt 120
    ) {
      Write-Log "Foreground after search: [$($foreground.Title)] $($foreground.Width)x$($foreground.Height)"
      return $foreground
    }

    $windows = @(Get-VisibleWindows)
    $dialog = @($windows | Where-Object {
      $_.Handle -ne $OrderHandle -and
      $_.ProcessId -eq $script:ErpProcessId -and
      $_.Title -like "*$QueryText*"
    } | Sort-Object Area)[0]
    if ($null -ne $dialog) { return $dialog }

    Start-Sleep -Milliseconds 300
  }
  return $null
}

function Get-DescendantElementsByControlType {
  param(
    [System.Windows.Automation.AutomationElement]$RootElement,
    [System.Windows.Automation.ControlType]$ControlType
  )

  $condition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    $ControlType
  )
  return @($RootElement.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition))
}

function Set-UiaValue {
  param(
    [System.Windows.Automation.AutomationElement]$Element,
    [string]$Value,
    [string]$Name
  )

  try {
    $pattern = $Element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    $pattern.SetValue($Value)
    Write-Log "Set UIA value $Name=$Value"
    return $true
  } catch {
    Write-Log "Set UIA value failed for $Name`: $($_.Exception.Message)"
    return $false
  }
}

function Try-FillQueryDialogUia {
  param([object]$Dialog)

  $rootElement = Get-AutomationRoot -Handle $Dialog.Handle
  $edits = @(Get-DescendantElementsByControlType -RootElement $rootElement -ControlType ([System.Windows.Automation.ControlType]::Edit))
  Write-Log "Query dialog UIA edit count: $($edits.Count)"
  if ($edits.Count -lt 3) { return $false }

  $setCount = 0
  if (Set-UiaValue -Element $edits[0] -Value $DateStart -Name "start_date") { $setCount++ }
  if (Set-UiaValue -Element $edits[1] -Value $DateEnd -Name "end_date") { $setCount++ }
  if (Set-UiaValue -Element $edits[2] -Value $OrderPrefix -Name "order_prefix") { $setCount++ }
  if ($setCount -lt 3) { return $false }

  $buttonNames = @($OkText, $QueryText, "OK")
  foreach ($buttonName in $buttonNames) {
    $button = Find-ElementByName -RootElement $rootElement -Name $buttonName -TimeoutSeconds 2
    if ($null -ne $button) {
      $result = Invoke-UiaElement -Element $button -Name $buttonName
      Write-Log "Query confirm: $result"
      return $true
    }
  }

  [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
  Write-Log "Query confirm: enter_key_after_uia_values"
  return $true
}

function Fill-QueryDialogByKeyboard {
  param([object]$Dialog)

  [void][ErpV35Native]::SetForegroundWindow($Dialog.Handle)
  Start-Sleep -Milliseconds 400
  Write-Log "Keyboard query fallback: start=$DateStart end=$DateEnd prefix=$OrderPrefix"

  [System.Windows.Forms.SendKeys]::SendWait("^a")
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait($DateStart)
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait("{TAB}")
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait("^a")
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait($DateEnd)
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait("{TAB}")
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait("^a")
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait($OrderPrefix)
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
  Start-Sleep -Milliseconds 800
  return "keyboard_tab_sequence"
}

function Set-ClipboardTextWithRetry {
  param([string]$Text)

  $lastError = $null
  for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
      [System.Windows.Forms.Clipboard]::SetText($Text)
      return
    } catch {
      $lastError = $_.Exception.Message
      Start-Sleep -Milliseconds 200
    }
  }
  throw "Could not prepare exact query text on the clipboard: $lastError"
}

function Get-ControlAtRelativePoint {
  param(
    [object]$Window,
    [int]$X,
    [int]$Y
  )

  $point = New-Object ErpV35Native+POINT
  $point.X = [int]($Window.Left + $X)
  $point.Y = [int]($Window.Top + $Y)
  return [ErpV35Native]::WindowFromPoint($point)
}

function Set-EnglishInputForControl {
  param([IntPtr]$ControlHandle)

  if ($ControlHandle -eq [IntPtr]::Zero) {
    throw "Date input control was not found at the confirmed position."
  }

  $englishLayout = [ErpV35Native]::LoadKeyboardLayout("00000409", [ErpV35Native]::KLF_ACTIVATE)
  if ($englishLayout -eq [IntPtr]::Zero) {
    throw "US English keyboard layout could not be loaded for the date field."
  }
  [void][ErpV35Native]::PostMessage(
    $ControlHandle,
    [ErpV35Native]::WM_INPUTLANGCHANGEREQUEST,
    [IntPtr]::Zero,
    $englishLayout
  )

  $inputContext = [ErpV35Native]::ImmGetContext($ControlHandle)
  if ($inputContext -ne [IntPtr]::Zero) {
    try { [void][ErpV35Native]::ImmSetOpenStatus($inputContext, $false) }
    finally { [void][ErpV35Native]::ImmReleaseContext($ControlHandle, $inputContext) }
  }
  Start-Sleep -Milliseconds 250
}

function Set-DateFieldByEnglishTyping {
  param(
    [object]$Dialog,
    [int]$X,
    [int]$Y,
    [string]$Value,
    [string]$Name
  )

  Invoke-RelativeClick -Window $Dialog -X $X -Y $Y -Name $Name
  $controlHandle = Get-ControlAtRelativePoint -Window $Dialog -X $X -Y $Y
  Set-EnglishInputForControl -ControlHandle $controlHandle
  [System.Windows.Forms.SendKeys]::SendWait("^a")
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait("{BACKSPACE}")
  Start-Sleep -Milliseconds 100
  foreach ($character in $Value.ToCharArray()) {
    [System.Windows.Forms.SendKeys]::SendWait([string]$character)
    Start-Sleep -Milliseconds 60
  }
  Start-Sleep -Milliseconds 200

  $actual = Get-WindowTitle -Handle $controlHandle
  $actualDigits = $actual -replace '[^0-9]', ''
  Write-Log "Date field readback after English typing: $Name expected=$Value actual=[$actual] digits=[$actualDigits]"
  if ([string]::IsNullOrWhiteSpace($actual)) {
    Write-Log "Legacy date control does not expose its text. English typing completed; continuing without readback verification."
    return
  }
  if ($actualDigits -ne $Value) {
    throw "Date field verification failed for $Name. Expected=$Value actual=[$actual]"
  }
}

function Set-QueryFieldByPosition {
  param(
    [object]$Dialog,
    [int]$X,
    [int]$Y,
    [string]$Value,
    [string]$Name
  )

  if ($Name -like "*_date") {
    Set-DateFieldByEnglishTyping -Dialog $Dialog -X $X -Y $Y -Value $Value -Name $Name
    return
  }

  Invoke-RelativeClick -Window $Dialog -X $X -Y $Y -Name $Name
  [System.Windows.Forms.SendKeys]::SendWait("^a")
  Start-Sleep -Milliseconds 100
  Set-ClipboardTextWithRetry -Text $Value
  [System.Windows.Forms.SendKeys]::SendWait("^v")
  Start-Sleep -Milliseconds 200
  Write-Log "Set query field by exact clipboard paste: $Name=$Value"
}

function Clear-QueryFieldByPosition {
  param(
    [object]$Dialog,
    [int]$X,
    [int]$Y,
    [string]$Name
  )

  Invoke-RelativeClick -Window $Dialog -X $X -Y $Y -Name $Name
  [System.Windows.Forms.SendKeys]::SendWait("^a")
  Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait("{BACKSPACE}")
  Start-Sleep -Milliseconds 150
  Write-Log "Cleared query field: $Name"
}

function Fill-QueryDialogByPosition {
  param([object]$Dialog)

  if ($Dialog.Width -lt 480 -or $Dialog.Height -lt 320) {
    throw "Query dialog size does not match the confirmed layout. Size=$($Dialog.Width)x$($Dialog.Height)."
  }

  [void][ErpV35Native]::SetForegroundWindow($Dialog.Handle)
  Start-Sleep -Milliseconds 300

  $originalClipboard = $null
  try {
    try { $originalClipboard = [System.Windows.Forms.Clipboard]::GetDataObject() } catch { }
    Set-QueryFieldByPosition -Dialog $Dialog -X 285 -Y 80 -Value $DateStartInput -Name "start_date"
    Set-QueryFieldByPosition -Dialog $Dialog -X 285 -Y 114 -Value $DateEndInput -Name "end_date"
    Set-QueryFieldByPosition -Dialog $Dialog -X 300 -Y 148 -Value $OrderPrefix -Name "order_prefix"
    Clear-QueryFieldByPosition -Dialog $Dialog -X 333 -Y 181 -Name "customer_name"
    Clear-QueryFieldByPosition -Dialog $Dialog -X 285 -Y 215 -Name "planned_ship_start"
    Clear-QueryFieldByPosition -Dialog $Dialog -X 285 -Y 249 -Name "planned_ship_end"
    Invoke-RelativeClick -Window $Dialog -X 389 -Y 311 -Name "query_confirm"
  } finally {
    if ($null -ne $originalClipboard) {
      try { [System.Windows.Forms.Clipboard]::SetDataObject($originalClipboard, $true) } catch { }
    }
  }

  if (-not (Wait-ForWindowClosed -Handle $Dialog.Handle -TimeoutSeconds $OrderScreenTimeoutSeconds)) {
    throw "Query dialog did not close after clicking confirm."
  }
  return "confirmed_position_fields"
}

function Invoke-OrderQuery {
  param(
    [object]$ParentWindow,
    [object]$OrderChild
  )

  $ok = Activate-Window -Handle $ParentWindow.Handle
  if (-not $ok) { throw "Order entry window could not be activated before query." }

  $title = Get-WindowTitle -Handle $OrderChild.Handle
  if ($title -notlike "*$OrderTitleKeyword*") {
    throw "Current child window is not the order entry screen before query. Title=[$title]"
  }
  Invoke-RelativeClick -Window $OrderChild -X 111 -Y 46 -Name "toolbar_search_magnifier"

  $dialog = Wait-ForQueryDialog -OrderHandle $ParentWindow.Handle -TimeoutSeconds $QueryDialogTimeoutSeconds
  if ($null -eq $dialog) {
    throw "Query dialog did not appear after clicking the magnifier."
  }

  $filledBy = Fill-QueryDialogByPosition -Dialog $dialog

  Start-Sleep -Seconds 2
  $orderAfterQuery = Wait-ForOrderWindow -TimeoutSeconds $OrderScreenTimeoutSeconds
  if ($null -eq $orderAfterQuery) {
    throw "Order entry window was not found after confirming query."
  }

  return "query_submitted_by=$filledBy start=$DateStart end=$DateEnd prefix=$OrderPrefix"
}

function Invoke-MenuPathUia {
  param(
    [IntPtr]$MainHandle,
    [string[]]$MenuNames
  )

  foreach ($name in $MenuNames) {
    $rootElement = Get-AutomationRoot -Handle $MainHandle
    $element = Find-ElementByName -RootElement $rootElement -Name $name -TimeoutSeconds 8
    if ($null -eq $element) { throw "Menu target not found before click: $name" }
    $result = Invoke-UiaElement -Element $element -Name $name
    Write-Log "Menu action: $result"
    Test-UnknownPopup
  }
}

function Invoke-FixedFallbackPath {
  param([object]$MainWindow)

  $screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  if ($screen.Width -ne 1920 -or $screen.Height -ne 1080) {
    throw "Fixed-position fallback requires 1920x1080 display. Current=$($screen.Width)x$($screen.Height)."
  }

  if (-not $EnableFixedPositionFallback) {
    throw "UI Automation failed. Fixed-position fallback is disabled for this run."
  }

  if (-not (Activate-Window -Handle $MainWindow.Handle)) {
    throw "ERP window could not be activated before fixed-position menu navigation."
  }

  Write-Log "Fixed-position fallback enabled. Using 1920x1080 / 100 percent layout assumptions."
  $steps = @(
    @{ Name = "top menu foreign management"; X = 268; Y = 34 },
    @{ Name = "external gift order entry"; X = 300; Y = 78 }
  )

  foreach ($step in $steps) {
    Write-Log "Fallback click target=$($step.Name) x=$($step.X) y=$($step.Y)"
    [void][ErpV35Native]::SetCursorPos([int]$step.X, [int]$step.Y)
    Start-Sleep -Milliseconds 200
    [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
    [ErpV35Native]::mouse_event([ErpV35Native]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 1200
    Test-UnknownPopup
  }
}

function Wait-ForErpWindow {
  param([int]$TimeoutSeconds)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $window = Find-ErpWindow
    if ($null -ne $window) { return $window }
    Start-Sleep -Seconds 1
  }
  return $null
}

function Wait-ForOrderWindow {
  param([int]$TimeoutSeconds)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $windows = @(Get-VisibleWindows)
    $order = @($windows | Where-Object { $_.IsOrderWindow } | Sort-Object Area -Descending)
    if ($order.Count -gt 0) { return $order[0] }
    Start-Sleep -Seconds 1
  }
  return $null
}

function Get-PortableBatchPath {
  param([string]$Path)

  if ([string]::IsNullOrWhiteSpace($Path)) { return "" }
  try {
    $batchRoot = [System.IO.Path]::GetFullPath($BatchDir)
    while ($batchRoot.EndsWith("\") -or $batchRoot.EndsWith("/")) {
      $batchRoot = $batchRoot.Substring(0, $batchRoot.Length - 1)
    }
    $batchRoot = "$batchRoot\"
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath.StartsWith($batchRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
      return $fullPath.Substring($batchRoot.Length).Replace("\", "/")
    }
  } catch { }
  return $Path
}

function Add-OrderStatus {
  param(
    [int]$Sequence,
    [string]$OrderNumber,
    [string]$Status,
    [datetime]$StartedAt,
    [datetime]$FinishedAt,
    [string]$ImagePath,
    [string]$Reason
  )

  $record = [pscustomobject]@{
    sequence = $Sequence
    order_number = $OrderNumber
    status = $Status
    started_at = $StartedAt.ToString("yyyy-MM-dd HH:mm:ss.fff")
    finished_at = $FinishedAt.ToString("yyyy-MM-dd HH:mm:ss.fff")
    duration_seconds = [Math]::Round(($FinishedAt - $StartedAt).TotalSeconds, 2)
    image = Get-PortableBatchPath -Path $ImagePath
    reason = $Reason
  }
  if (Test-Path -LiteralPath $OrderStatusPath) {
    $record | Export-Csv -LiteralPath $OrderStatusPath -NoTypeInformation -Encoding UTF8 -Append
  } else {
    $record | Export-Csv -LiteralPath $OrderStatusPath -NoTypeInformation -Encoding UTF8
  }
}

function Get-BatchSummaryCounts {
  $records = @()
  if (Test-Path -LiteralPath $OrderStatusPath) {
    $records = @(Import-Csv -LiteralPath $OrderStatusPath)
  }
  $imageCount = @(Get-ChildItem -LiteralPath $CaptureDir -File -Filter "*.png" -ErrorAction SilentlyContinue).Count
  return [pscustomobject]@{
    TotalRecords = $records.Count
    Success = @($records | Where-Object { $_.status -eq "success" }).Count
    Failed = @($records | Where-Object { $_.status -eq "failed" }).Count
    Skipped = @($records | Where-Object { $_.status -eq "skipped" }).Count
    Retry = @($records | Where-Object { $_.status -eq "retry" }).Count
    ImageCount = $imageCount
  }
}

function Get-NextStatusSequence {
  if (-not (Test-Path -LiteralPath $OrderStatusPath)) { return 1 }
  return (@(Import-Csv -LiteralPath $OrderStatusPath).Count + 1)
}

function Get-ExistingOrderSet {
  $seen = @{}
  if (Test-Path -LiteralPath $OrderStatusPath) {
    foreach ($record in @(Import-Csv -LiteralPath $OrderStatusPath)) {
      if ($record.status -eq "success" -and -not [string]::IsNullOrWhiteSpace($record.order_number)) {
        $seen[$record.order_number.ToUpperInvariant()] = $true
      }
    }
  }
  foreach ($file in @(Get-ChildItem -LiteralPath $CaptureDir -File -Filter "*.png" -ErrorAction SilentlyContinue)) {
    $seen[$file.BaseName.ToUpperInvariant()] = $true
  }
  return $seen
}

function Save-BatchState {
  param(
    [string]$Status,
    [string]$CurrentOrder,
    [int]$CapturedCount,
    [string]$StopReason
  )

  $stoppedAt = ""
  if (-not [string]::IsNullOrWhiteSpace($StopReason)) {
    $stoppedAt = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  }
  $state = [ordered]@{
    version = "V4"
    build = $BuildVersion
    status = $Status
    run_mode = $RunMode
    date_start = $DateStart
    date_end = $DateEnd
    date_format = "yyyy/MM/dd"
    order_prefix = $OrderPrefix
    customer_name = ""
    planned_ship_date_start = ""
    planned_ship_date_end = ""
    current_order = $CurrentOrder
    captured_count = $CapturedCount
    completed_order_numbers = @($seenOrders.Keys | Sort-Object)
    maximum_orders = $MaximumOrders
    minimum_wait_seconds = $MinimumWaitSeconds
    maximum_wait_seconds = $MaximumWaitSeconds
     batch_dir = "."
     batch_name = (Split-Path -Leaf $BatchDir)
    stop_reason = $StopReason
    stopped_at = $stoppedAt
    updated_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  }
  $stateJson = $state | ConvertTo-Json -Depth 5
  $stateTargets = @($StatePath, $BatchStatePath, $LegacyStatePath) |
    Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
    Select-Object -Unique
  $mutexNameBytes = [System.Text.Encoding]::UTF8.GetBytes(
    ([System.IO.Path]::GetFullPath($BatchDir)).ToUpperInvariant()
  )
  $sha256 = [System.Security.Cryptography.SHA256]::Create()
  try {
    $mutexToken = ([System.BitConverter]::ToString(
      $sha256.ComputeHash($mutexNameBytes)
    ) -replace '-', '').Substring(0, 24)
  } finally {
    $sha256.Dispose()
  }
  $stateMutex = New-Object System.Threading.Mutex(
    $false,
    "Local\WeddingOrderV6BatchState_$mutexToken"
  )
  $lockAcquired = $false
  try {
    try {
      $lockAcquired = $stateMutex.WaitOne(30000)
    } catch [System.Threading.AbandonedMutexException] {
      $lockAcquired = $true
    }
    if (-not $lockAcquired) {
      throw "Timed out waiting for the batch-state write lock."
    }

    foreach ($target in $stateTargets) {
      $targetDirectory = Split-Path -Parent $target
      New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
      $temporaryState = Join-Path $targetDirectory (
        ".{0}.{1}.{2}.tmp" -f
        (Split-Path -Leaf $target), $PID, ([guid]::NewGuid().ToString("N"))
      )
      [System.IO.File]::WriteAllText(
        $temporaryState,
        $stateJson,
        (New-Object System.Text.UTF8Encoding($true))
      )
      $replaced = $false
      try {
        for ($attempt = 1; $attempt -le 12; $attempt++) {
          if ([ErpV35Native]::MoveFileEx($temporaryState, $target, 0x9)) {
            $replaced = $true
            break
          }
          if ($attempt -lt 12) {
            Start-Sleep -Milliseconds ([Math]::Min(800, 40 * [Math]::Pow(2, $attempt - 1)))
          }
        }
        if (-not $replaced) {
          $errorCode = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
          throw "Atomic batch-state replace failed after 12 attempts: target=$target win32=$errorCode"
        }
      } finally {
        if (-not $replaced) {
          Remove-Item -LiteralPath $temporaryState -Force -ErrorAction SilentlyContinue
        }
      }
    }
  } finally {
    if ($lockAcquired) {
      try { $stateMutex.ReleaseMutex() } catch { }
    }
    $stateMutex.Dispose()
  }
}

function Write-BatchSummary {
  param(
    [string]$Status,
    [int]$CapturedCount,
    [string]$CurrentOrder,
    [string]$StopReason,
    [datetime]$StartedAt
  )

  $summaryStoppedAt = ""
  if (-not [string]::IsNullOrWhiteSpace($StopReason)) {
    $summaryStoppedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  }
  $counts = Get-BatchSummaryCounts
  Set-Content -LiteralPath $SummaryPath -Value @(
    "version=V4"
    "build=$BuildVersion"
    "status=$Status"
    "run_mode=$RunMode"
    "date_start=$DateStart"
    "date_end=$DateEnd"
    "date_format=yyyy/MM/dd"
    "order_prefix=$OrderPrefix"
    "customer_name="
    "planned_ship_date_start="
    "planned_ship_date_end="
    "total_orders=$CapturedCount"
    "captured_count=$CapturedCount"
    "success_count=$($counts.Success)"
    "failed_count=$($counts.Failed)"
    "skipped_count=$($counts.Skipped)"
    "retry_count=$($counts.Retry)"
    "image_count=$($counts.ImageCount)"
    "image_count_matches_success=$($counts.ImageCount -eq $counts.Success)"
    "current_order=$CurrentOrder"
    "stop_reason=$StopReason"
    "stopped_at=$summaryStoppedAt"
     "batch_dir=."
     "batch_name=$(Split-Path -Leaf $BatchDir)"
     "captures=captures"
     "order_status_csv=order_status.csv"
     "latest_run_log=logs/$(Split-Path -Leaf $LogPath)"
    "duration_seconds=$([Math]::Round(((Get-Date) - $StartedAt).TotalSeconds, 2))"
    "time=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
  ) -Encoding UTF8
}

function Test-StopRequested {
  return (Test-Path -LiteralPath $StopFlagPath)
}

function Clear-StopFlag {
  for ($attempt = 1; $attempt -le 12; $attempt++) {
    Remove-Item -LiteralPath $StopFlagPath -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $StopFlagPath)) { return }
    if ($attempt -lt 12) {
      Start-Sleep -Milliseconds ([Math]::Min(800, 40 * [Math]::Pow(2, $attempt - 1)))
    }
  }
  throw "The stale STOP flag could not be cleared: $StopFlagPath"
}

function Get-CurrentOrderNumber {
  param(
    [object]$OrderChild,
    [string]$PreviousOrderNumber = "",
    [switch]$HeaderOnly
  )

  try {
    $rootElement = [System.Windows.Automation.AutomationElement]::FromHandle($OrderChild.Handle)
    if ($null -eq $rootElement) { return "UNKNOWN" }
    $elements = $rootElement.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition
    )
    $candidates = New-Object System.Collections.Generic.List[object]

    foreach ($element in $elements) {
      $values = New-Object System.Collections.Generic.List[string]
      try {
        $name = $element.Current.Name
        if (-not [string]::IsNullOrWhiteSpace($name)) { $values.Add($name) }
      } catch { }
      try {
        $valuePattern = $null
        if ($element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)) {
          $value = $valuePattern.Current.Value
          if (-not [string]::IsNullOrWhiteSpace($value)) { $values.Add($value) }
        }
      } catch { }

      $isSelected = $false
      try {
        $selectionPattern = $null
        if ($element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$selectionPattern)) {
          $isSelected = $selectionPattern.Current.IsSelected
        }
      } catch { }

      $relativeCenterX = -1.0
      $relativeCenterY = -1.0
      try {
        $bounds = $element.Current.BoundingRectangle
        if (-not $bounds.IsEmpty) {
          $relativeCenterX = ($bounds.X + ($bounds.Width / 2.0)) - $OrderChild.Left
          $relativeCenterY = ($bounds.Y + ($bounds.Height / 2.0)) - $OrderChild.Top
        }
      } catch { }
      $isHeaderOrderField = (
        $relativeCenterX -ge 210 -and $relativeCenterX -le 520 -and
        $relativeCenterY -ge 45 -and $relativeCenterY -le 125
      )

      foreach ($value in $values) {
        foreach ($match in [regex]::Matches($value, "(?i)\bGOU[0-9A-Z-]{6,20}\b")) {
          if ($HeaderOnly -and -not $isHeaderOrderField) { continue }
          $score = 0
          if ($isHeaderOrderField) { $score += 1000 }
          if ($isSelected) { $score += 500 }
          $candidates.Add([pscustomobject]@{
            Value = $match.Value.ToUpperInvariant()
            IsHeaderOrderField = $isHeaderOrderField
            IsSelected = $isSelected
            Score = $score
            RelativeX = [Math]::Round($relativeCenterX, 1)
            RelativeY = [Math]::Round($relativeCenterY, 1)
          })
        }
      }
    }

    if ($candidates.Count -gt 0) {
      $ranked = @($candidates |
        Group-Object Value |
        ForEach-Object {
          $maxScore = ($_.Group | Measure-Object Score -Maximum).Maximum
          [pscustomobject]@{
            Value = $_.Name
            Score = $maxScore + $_.Count
            Count = $_.Count
            HasHeader = (@($_.Group | Where-Object { $_.IsHeaderOrderField }).Count -gt 0)
            HasSelected = (@($_.Group | Where-Object { $_.IsSelected }).Count -gt 0)
          }
        } |
        Sort-Object Score -Descending)
      $best = $ranked[0]
      if (-not [string]::IsNullOrWhiteSpace($PreviousOrderNumber)) {
        $changedCandidate = @($ranked | Where-Object {
          $_.Value -ne $PreviousOrderNumber -and ($_.HasSelected -or $_.HasHeader)
        } | Sort-Object Score -Descending | Select-Object -First 1)
        if ($changedCandidate.Count -gt 0) {
          Write-Log "Order-number change accepted from selected/header control: previous=$PreviousOrderNumber current=$($changedCandidate[0].Value)"
          return [string]$changedCandidate[0].Value
        }
      }
      if ($ranked.Count -gt 1 -and $best.Score -eq $ranked[1].Score) {
        Write-Log "Order number read is ambiguous in active child: $($ranked[0].Value), $($ranked[1].Value)"
        return "UNKNOWN"
      }
      return [string]$best.Value
    }
  } catch {
    Write-Log "Order number read warning: $($_.Exception.Message)"
  }
  return "UNKNOWN"
}

function Read-VisibleOrderNumberWithOcr {
  param(
    [object]$OrderChild,
    [string]$PreviousOrderNumber
  )

  $v5Root = Split-Path -Parent (Split-Path -Parent $Root)
  $tesseract = Join-Path $v5Root "runtime\tesseract\tesseract.exe"
  $tessdata = Join-Path $v5Root "runtime\tesseract\tessdata"
  if (-not (Test-Path -LiteralPath $tesseract)) { return "UNKNOWN" }

  $probeId = "order_number_{0}_{1}" -f $PID, ([guid]::NewGuid().ToString("N"))
  $fullPath = Join-Path $LogDir "$probeId.png"
  $cropPath = Join-Path $LogDir "${probeId}_crop.png"
  $source = $null
  $scaled = $null
  $graphics = $null
  try {
    [void](Save-WindowPng -Handle $OrderChild.Handle -Path $fullPath)
    $source = [System.Drawing.Bitmap]::FromFile($fullPath)
    # The order-number edit box is stable relative to the maximized MDI child.
    # Keep this crop tight so the nearby date field cannot win OCR recognition.
    $cropX = [Math]::Min(235, [Math]::Max(0, $source.Width - 1))
    $cropY = [Math]::Min(45, [Math]::Max(0, $source.Height - 1))
    $cropWidth = [Math]::Min(250, $source.Width - $cropX)
    $cropHeight = [Math]::Min(75, $source.Height - $cropY)
    if ($cropWidth -lt 40 -or $cropHeight -lt 20) { return "UNKNOWN" }

    $scaled = New-Object System.Drawing.Bitmap -ArgumentList @(($cropWidth * 5), ($cropHeight * 5))
    $graphics = [System.Drawing.Graphics]::FromImage($scaled)
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
    $graphics.DrawImage(
      $source,
      (New-Object System.Drawing.Rectangle -ArgumentList @(0, 0, $scaled.Width, $scaled.Height)),
      (New-Object System.Drawing.Rectangle -ArgumentList @($cropX, $cropY, $cropWidth, $cropHeight)),
      [System.Drawing.GraphicsUnit]::Pixel
    )
    $scaled.Save($cropPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose(); $graphics = $null
    $scaled.Dispose(); $scaled = $null
    $source.Dispose(); $source = $null

    $ocrLines = New-Object System.Collections.Generic.List[string]
    foreach ($psm in @(6, 11)) {
      $ocrOutput = & $tesseract $cropPath stdout --tessdata-dir $tessdata -l eng --psm $psm 2>$null
      $ocrLines.Add(($ocrOutput -join " "))
    }
    $text = ($ocrLines -join " ").ToUpperInvariant()
    $matches = @(Get-GouOrderNumbersFromOcrLines -Lines $ocrLines)
    $changed = @($matches | Where-Object { $_ -ne $PreviousOrderNumber } | Select-Object -Unique)
    if ($changed.Count -eq 1) {
      Write-Log "Visible order-number OCR fallback succeeded: previous=$PreviousOrderNumber current=$($changed[0])"
      return [string]$changed[0]
    }
    Write-Log "Visible order-number OCR fallback was inconclusive: previous=$PreviousOrderNumber text=[$text]"
  } catch {
    Write-Log "Visible order-number OCR fallback warning: $($_.Exception.Message)"
  } finally {
    if ($null -ne $graphics) { $graphics.Dispose() }
    if ($null -ne $scaled) { $scaled.Dispose() }
    if ($null -ne $source) { $source.Dispose() }
    Remove-Item -LiteralPath $fullPath, $cropPath -Force -ErrorAction SilentlyContinue
  }
  return "UNKNOWN"
}

function Find-SelectedOrderRowY {
  param([object]$OrderChild)

  $probePath = Join-Path $LogDir ("selected_row_probe_{0}_{1}.png" -f $PID, ([guid]::NewGuid().ToString("N")))
  $bitmap = $null
  try {
    [void](Save-WindowPng -Handle $OrderChild.Handle -Path $probePath)
    $bitmap = [System.Drawing.Bitmap]::FromFile($probePath)
    $startY = 90
    $endY = [Math]::Min($bitmap.Height - 25, 980)
    $startX = 15
    $endX = [Math]::Min($bitmap.Width - 15, 180)
    $bestY = -1
    $bestCount = 0

    for ($y = $startY; $y -le $endY; $y++) {
      $blueCount = 0
      for ($x = $startX; $x -le $endX; $x += 2) {
        $color = $bitmap.GetPixel($x, $y)
        if (
          $color.B -ge 130 -and
          $color.B -ge ($color.R + 45) -and
          $color.G -ge ($color.R + 35)
        ) {
          $blueCount++
        }
      }
      if ($blueCount -gt $bestCount) {
        $bestCount = $blueCount
        $bestY = $y
      }
    }

    if ($bestCount -lt 18) {
      Write-Log "Selected-row color scan did not find enough blue pixels: best_count=$bestCount"
      return -1
    }

    $threshold = [Math]::Max(12, [int][Math]::Floor($bestCount * 0.55))
    $top = $bestY
    $bottom = $bestY
    for ($y = $bestY - 1; $y -ge $startY; $y--) {
      $count = 0
      for ($x = $startX; $x -le $endX; $x += 2) {
        $color = $bitmap.GetPixel($x, $y)
        if ($color.B -ge 130 -and $color.B -ge ($color.R + 45) -and $color.G -ge ($color.R + 35)) { $count++ }
      }
      if ($count -lt $threshold) { break }
      $top = $y
    }
    for ($y = $bestY + 1; $y -le $endY; $y++) {
      $count = 0
      for ($x = $startX; $x -le $endX; $x += 2) {
        $color = $bitmap.GetPixel($x, $y)
        if ($color.B -ge 130 -and $color.B -ge ($color.R + 45) -and $color.G -ge ($color.R + 35)) { $count++ }
      }
      if ($count -lt $threshold) { break }
      $bottom = $y
    }
    $rowY = [int][Math]::Round(($top + $bottom) / 2.0)
    Write-Log "Selected row found by color: y=$rowY range=$top-$bottom blue_count=$bestCount"
    return $rowY
  } catch {
    Write-Log "Selected-row color scan warning: $($_.Exception.Message)"
    return -1
  } finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
  }
}

function Get-VisibleOrdersBelowSelection {
  param([object]$OrderChild)

  $rowY = Find-SelectedOrderRowY -OrderChild $OrderChild
  if ($rowY -lt 0) {
    return [pscustomobject]@{ Reliable = $false; Orders = @(); Reason = "selected_row_not_found" }
  }

  $v5Root = Split-Path -Parent (Split-Path -Parent $Root)
  $tesseract = Join-Path $v5Root "runtime\tesseract\tesseract.exe"
  $tessdata = Join-Path $v5Root "runtime\tesseract\tessdata"
  if (-not (Test-Path -LiteralPath $tesseract)) {
    return [pscustomobject]@{ Reliable = $false; Orders = @(); Reason = "tesseract_not_found" }
  }

  $probeId = "orders_below_{0}_{1}" -f $PID, ([guid]::NewGuid().ToString("N"))
  $fullPath = Join-Path $LogDir "$probeId.png"
  $cropPath = Join-Path $LogDir "${probeId}_crop.png"
  $source = $null
  $scaled = $null
  $graphics = $null
  try {
    [void](Save-WindowPng -Handle $OrderChild.Handle -Path $fullPath)
    $source = [System.Drawing.Bitmap]::FromFile($fullPath)
    $cropX = 5
    $cropY = [Math]::Min($source.Height - 1, $rowY + 12)
    $cropWidth = [Math]::Min(175, $source.Width - $cropX)
    $cropHeight = [Math]::Min(850, $source.Height - $cropY)
    if ($cropWidth -lt 80 -or $cropHeight -lt 20) {
      return [pscustomobject]@{ Reliable = $false; Orders = @(); Reason = "below_selection_crop_too_small" }
    }

    $scaled = New-Object System.Drawing.Bitmap -ArgumentList @(($cropWidth * 3), ($cropHeight * 3))
    $graphics = [System.Drawing.Graphics]::FromImage($scaled)
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
    $graphics.DrawImage(
      $source,
      (New-Object System.Drawing.Rectangle -ArgumentList @(0, 0, $scaled.Width, $scaled.Height)),
      (New-Object System.Drawing.Rectangle -ArgumentList @($cropX, $cropY, $cropWidth, $cropHeight)),
      [System.Drawing.GraphicsUnit]::Pixel
    )
    $scaled.Save($cropPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose(); $graphics = $null
    $scaled.Dispose(); $scaled = $null
    $source.Dispose(); $source = $null

    $ocrOutput = & $tesseract $cropPath stdout --tessdata-dir $tessdata -l eng --psm 6 2>$null
    if ($LASTEXITCODE -ne 0) {
      return [pscustomobject]@{ Reliable = $false; Orders = @(); Reason = "tesseract_exit_$LASTEXITCODE" }
    }
    $orders = @(Get-GouOrderNumbersFromOcrLines -Lines @($ocrOutput))
    Write-Log "Orders visible below selected row: row_y=$rowY count=$($orders.Count) orders=[$($orders -join ',')]"
    return [pscustomobject]@{ Reliable = $true; Orders = $orders; Reason = "ocr_checked" }
  } catch {
    Write-Log "Orders-below-selection check warning: $($_.Exception.Message)"
    return [pscustomobject]@{ Reliable = $false; Orders = @(); Reason = $_.Exception.Message }
  } finally {
    if ($null -ne $graphics) { $graphics.Dispose() }
    if ($null -ne $scaled) { $scaled.Dispose() }
    if ($null -ne $source) { $source.Dispose() }
    Remove-Item -LiteralPath $fullPath, $cropPath -Force -ErrorAction SilentlyContinue
  }
}

function Get-OrderListControl {
  param(
    [object]$OrderChild,
    [int]$RowY,
    [string]$Purpose
  )

  $arrowX = 18
  if ($RowY -lt 100 -or $RowY -gt ($OrderChild.Height - 20)) {
    throw "Order-list row is outside the active child: y=$RowY height=$($OrderChild.Height) purpose=$Purpose"
  }
  $screenX = [int]($OrderChild.Left + $arrowX)
  $screenY = [int]($OrderChild.Top + $RowY)
  $point = New-Object ErpV35Native+POINT
  $point.X = $screenX
  $point.Y = $screenY
  $handle = [ErpV35Native]::WindowFromPoint($point)
  if ($handle -eq [IntPtr]::Zero) {
    throw "WindowFromPoint returned no control for the order-list arrow area at $screenX,$screenY."
  }
  $descriptor = Get-WindowDescriptor -Handle $handle -Level 0
  Write-Log (
    "ORDER LIST CONTROL purpose={0} point={1},{2} relative={3},{4} handle={5} pid={6} " +
    "thread={7} class=[{8}] title=[{9}] rect={10}x{11} at {12},{13}" -f
    $Purpose, $screenX, $screenY, $arrowX, $RowY, $descriptor.HandleValue,
    $descriptor.ProcessId, (Get-WindowThreadId -Handle $handle),
    $descriptor.ClassName, $descriptor.Title, $descriptor.Width,
    $descriptor.Height, $descriptor.Left, $descriptor.Top
  )
  if ($descriptor.ProcessId -ne [uint32]$OrderChild.ProcessId) {
    throw "WindowFromPoint order-list control belongs to pid=$($descriptor.ProcessId), expected ERP pid=$($OrderChild.ProcessId)."
  }
  return [pscustomobject]@{
    Handle = $handle
    HandleValue = $descriptor.HandleValue
    ProcessId = $descriptor.ProcessId
    ThreadId = Get-WindowThreadId -Handle $handle
    ClassName = $descriptor.ClassName
    Title = $descriptor.Title
    ArrowX = $arrowX
    RowY = $RowY
    ScreenX = $screenX
    ScreenY = $screenY
  }
}

function Set-OrderListControlFocus {
  param([object]$Control)

  $currentThread = [ErpV35Native]::GetCurrentThreadId()
  $targetThread = [uint32]$Control.ThreadId
  $foregroundHandle = [ErpV35Native]::GetForegroundWindow()
  $foregroundThread = if ($foregroundHandle -ne [IntPtr]::Zero) {
    Get-WindowThreadId -Handle $foregroundHandle
  } else { [uint32]0 }
  $attachedTarget = $false
  $attachedForeground = $false
  try {
    if ($targetThread -ne 0 -and $targetThread -ne $currentThread) {
      $attachedTarget = [ErpV35Native]::AttachThreadInput($currentThread, $targetThread, $true)
    }
    if ($foregroundThread -ne 0 -and $foregroundThread -ne $currentThread -and $foregroundThread -ne $targetThread) {
      $attachedForeground = [ErpV35Native]::AttachThreadInput($currentThread, $foregroundThread, $true)
    }
    [void][ErpV35Native]::SetFocus($Control.Handle)
    Start-Sleep -Milliseconds 120
    $focused = [ErpV35Native]::GetFocus()
    $matched = ($focused -eq $Control.Handle)
    Write-Log "ORDER LIST FOCUS target=$($Control.HandleValue) focused=$($focused.ToInt64()) current_thread=$currentThread target_thread=$targetThread foreground_thread=$foregroundThread attached_target=$attachedTarget attached_foreground=$attachedForeground matched=$matched"
    return $matched
  } finally {
    if ($attachedForeground) {
      [void][ErpV35Native]::AttachThreadInput($currentThread, $foregroundThread, $false)
    }
    if ($attachedTarget) {
      [void][ErpV35Native]::AttachThreadInput($currentThread, $targetThread, $false)
    }
  }
}

function Post-KeyToOrderListControl {
  param(
    [object]$Control,
    [int]$VirtualKey,
    [string]$Name
  )

  try {
    $focusMatched = Set-OrderListControlFocus -Control $Control
    Write-Log "POST KEY name=$Name vk=$VirtualKey handle=$($Control.HandleValue) class=[$($Control.ClassName)] focus_matched=$focusMatched"
    [void][ErpV35Native]::PostMessage(
      $Control.Handle,
      [ErpV35Native]::WM_KEYDOWN,
      [IntPtr]$VirtualKey,
      [IntPtr]::Zero
    )
    Start-Sleep -Milliseconds 80
    [void][ErpV35Native]::PostMessage(
      $Control.Handle,
      [ErpV35Native]::WM_KEYUP,
      [IntPtr]$VirtualKey,
      [IntPtr]::Zero
    )
    return $true
  } catch {
    Write-Log "Direct order-list key warning: name=$Name error=$($_.Exception.Message)"
    return $false
  }
}

function Get-TrackedOrderRowY {
  param([object]$OrderChild, [int]$VisualRowIndex)
  $rowY = Find-SelectedOrderRowY -OrderChild $OrderChild
  if ($rowY -ge 0) { return $rowY }
  $visibleRowIndex = [Math]::Min([Math]::Max($VisualRowIndex, 0), 34)
  $rowY = 138 + (24 * $visibleRowIndex)
  Write-Log "Using tracked order row: visual_index=$VisualRowIndex relative_y=$rowY"
  return $rowY
}

function Invoke-OrderListDown {
  param([object]$OrderChild, [string]$CurrentOrder, [int]$VisualRowIndex)
  $rowY = Get-TrackedOrderRowY -OrderChild $OrderChild -VisualRowIndex $VisualRowIndex
  $control = Get-OrderListControl -OrderChild $OrderChild -RowY $rowY -Purpose "down_$CurrentOrder"
  Invoke-RelativeClick -Window $OrderChild -X $control.ArrowX -Y $rowY -Name "order_list_arrow_current_$CurrentOrder"
  if (-not (Post-KeyToOrderListControl -Control $control -VirtualKey ([ErpV35Native]::VK_DOWN) -Name "VK_DOWN")) {
    throw "The actual ERP order-list control could not receive VK_DOWN: handle=$($control.HandleValue)."
  }
  return $control
}

function Click-NextOrderRowFallback {
  param([object]$OrderChild, [string]$CurrentOrder, [int]$VisualRowIndex)
  $rowY = Get-TrackedOrderRowY -OrderChild $OrderChild -VisualRowIndex $VisualRowIndex
  $nextRowY = $rowY + 24
  if ($nextRowY -gt ($OrderChild.Height - 20)) {
    Write-Log "Next-row fallback is outside the active order child: current_y=$rowY next_y=$nextRowY height=$($OrderChild.Height)"
    return $false
  }
  $control = Get-OrderListControl -OrderChild $OrderChild -RowY $nextRowY -Purpose "next_row_fallback_$CurrentOrder"
  Write-Log "NEXT ROW FALLBACK previous=$CurrentOrder current_y=$rowY next_y=$nextRowY handle=$($control.HandleValue)"
  Invoke-RelativeClick -Window $OrderChild -X $control.ArrowX -Y $nextRowY -Name "order_list_arrow_next_after_$CurrentOrder"
  return $true
}

function Read-QueryResultOrdersWithOcr {
  param([object]$OrderChild)

  $v5Root = Split-Path -Parent (Split-Path -Parent $Root)
  $tesseract = Join-Path $v5Root "runtime\tesseract\tesseract.exe"
  $tessdata = Join-Path $v5Root "runtime\tesseract\tessdata"
  if (-not (Test-Path -LiteralPath $tesseract)) {
    return [pscustomobject]@{ Reliable = $false; Orders = @(); Reason = "tesseract_not_found" }
  }

  $probeId = "query_results_{0}_{1}" -f $PID, ([guid]::NewGuid().ToString("N"))
  $fullPath = Join-Path $LogDir "$probeId.png"
  $cropPath = Join-Path $LogDir "${probeId}_crop.png"
  $source = $null
  $scaled = $null
  $graphics = $null
  try {
    [void](Save-WindowPng -Handle $OrderChild.Handle -Path $fullPath)
    $source = [System.Drawing.Bitmap]::FromFile($fullPath)
    $cropX = 5
    $cropY = 100
    $cropWidth = [Math]::Min(180, $source.Width - $cropX)
    $cropHeight = [Math]::Min(880, $source.Height - $cropY)
    if ($cropWidth -lt 80 -or $cropHeight -lt 40) {
      return [pscustomobject]@{ Reliable = $false; Orders = @(); Reason = "query_list_crop_too_small" }
    }
    $scaled = New-Object System.Drawing.Bitmap -ArgumentList @(($cropWidth * 3), ($cropHeight * 3))
    $graphics = [System.Drawing.Graphics]::FromImage($scaled)
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
    $graphics.DrawImage(
      $source,
      (New-Object System.Drawing.Rectangle -ArgumentList @(0, 0, $scaled.Width, $scaled.Height)),
      (New-Object System.Drawing.Rectangle -ArgumentList @($cropX, $cropY, $cropWidth, $cropHeight)),
      [System.Drawing.GraphicsUnit]::Pixel
    )
    $scaled.Save($cropPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose(); $graphics = $null
    $scaled.Dispose(); $scaled = $null
    $source.Dispose(); $source = $null

    $successfulRuns = 0
    $allLines = New-Object System.Collections.Generic.List[string]
    foreach ($psm in @(6, 11, 12)) {
      $ocrOutput = @(& $tesseract $cropPath stdout --tessdata-dir $tessdata -l eng --psm $psm 2>$null)
      if ($LASTEXITCODE -eq 0) {
        $successfulRuns++
        foreach ($line in $ocrOutput) { $allLines.Add([string]$line) }
      }
    }
    if ($successfulRuns -lt 2) {
      return [pscustomobject]@{ Reliable = $false; Orders = @(); Reason = "query_list_ocr_failed" }
    }
    $orders = @(Get-GouOrderNumbersFromOcrLines -Lines @($allLines))
    Write-Log "QUERY RESULT OCR reliable=true count=$($orders.Count) orders=[$($orders -join ',')]"
    return [pscustomobject]@{ Reliable = $true; Orders = $orders; Reason = "query_list_ocr_checked" }
  } catch {
    Write-Log "Query-result OCR warning: $($_.Exception.Message)"
    return [pscustomobject]@{ Reliable = $false; Orders = @(); Reason = $_.Exception.Message }
  } finally {
    if ($null -ne $graphics) { $graphics.Dispose() }
    if ($null -ne $scaled) { $scaled.Dispose() }
    if ($null -ne $source) { $source.Dispose() }
    Remove-Item -LiteralPath $fullPath, $cropPath -Force -ErrorAction SilentlyContinue
  }
}

function Select-FirstQueryResult {
  param(
    [object]$ParentWindow,
    [object]$OrderChild
  )

  if (-not (Activate-Window -Handle $ParentWindow.Handle)) {
    throw "ERP window could not be activated before selecting the first order."
  }
  $detected = Read-QueryResultOrdersWithOcr -OrderChild $OrderChild
  if ($detected.Reliable -and @($detected.Orders).Count -eq 0) {
    $emptyDecision = Get-QueryFirstOrderDecision `
      -DetectedOrders @() -SelectedOrder "" -DetectionReliable $true
    return [pscustomobject]@{
      HasResults = $false
      OrderNumber = ""
      Reason = $emptyDecision.Reason
      OrderNumberKnown = $false
    }
  }
  $firstRowY = 138
  $control = Get-OrderListControl -OrderChild $OrderChild -RowY $firstRowY -Purpose "select_first_query_result"
  Invoke-RelativeClick -Window $OrderChild -X $control.ArrowX -Y $firstRowY -Name "first_query_result_arrow_row"
  $focusMatched = Set-OrderListControlFocus -Control $control
  $selectionDecision = Get-QueryFirstOrderDecision `
    -DetectedOrders @($detected.Orders) `
    -SelectedOrder "" `
    -DetectionReliable ([bool]$detected.Reliable) `
    -ClickSucceeded $true `
    -FocusMatched ([bool]$focusMatched)
  if ($selectionDecision.Action -eq "fail") {
    throw $selectionDecision.Reason
  }
  Write-Log "Selected the first query result by its row-selector arrow area; focus_matched=$focusMatched list_ocr_reliable=$($detected.Reliable) list_ocr_count=$(@($detected.Orders).Count)."
  $selectionStarted = Get-Date
  Start-Sleep -Seconds $MinimumWaitSeconds

  $deadline = $selectionStarted.AddSeconds($MaximumWaitSeconds)
  $headerAttempts = 0
  do {
    $headerAttempts++
    $orderNumber = Get-CurrentOrderNumber -OrderChild $OrderChild -HeaderOnly
    if ($orderNumber -eq "UNKNOWN" -and ($headerAttempts -eq 1 -or $headerAttempts -ge 3)) {
      $orderNumber = Read-VisibleOrderNumberWithOcr -OrderChild $OrderChild -PreviousOrderNumber ""
    }
    if ($orderNumber -match '^GOU[0-9A-Z-]{6,20}$') {
      $decision = Get-QueryFirstOrderDecision `
        -DetectedOrders @($detected.Orders) `
        -SelectedOrder $orderNumber `
        -DetectionReliable ([bool]$detected.Reliable) `
        -ClickSucceeded $true `
        -FocusMatched $true
      if ($decision.Action -eq "fail") { throw $decision.Reason }
      if ($detected.Reliable -and @($detected.Orders).Count -gt 0 -and $orderNumber -ne [string]$detected.Orders[0]) {
        Write-Log "QUERY LIST OCR AUXILIARY MISMATCH ignored: list_first=$($detected.Orders[0]) header=$orderNumber"
      }
      return [pscustomobject]@{
        HasResults = $true
        OrderNumber = $decision.OrderNumber
        Reason = $decision.Reason
        OrderNumberKnown = $true
      }
    }
    if (Test-StopRequested) { throw "Stop requested while waiting for the first order." }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  $fallback = Get-QueryFirstOrderDecision `
    -DetectedOrders @($detected.Orders) `
    -SelectedOrder "" `
    -DetectionReliable ([bool]$detected.Reliable) `
    -ClickSucceeded $true `
    -FocusMatched $true `
    -ProvisionalOrder "seq_0001"
  Write-Log "HEADER ORDER NUMBER unreadable after $headerAttempts attempts; capturing first order as $($fallback.OrderNumber) for normal OCR and review handling."
  return [pscustomobject]@{
    HasResults = $true
    OrderNumber = $fallback.OrderNumber
    Reason = $fallback.Reason
    OrderNumberKnown = $false
  }
}

function Wait-ForOrderNumberChange {
  param(
    [object]$OrderChild,
    [string]$PreviousOrderNumber
  )

  $started = Get-Date
  Start-Sleep -Seconds $MinimumWaitSeconds
  while ($true) {
    if (Test-StopRequested) {
      return [pscustomobject]@{ Changed = $false; StopRequested = $true; OrderNumber = $PreviousOrderNumber; WaitSeconds = [Math]::Round(((Get-Date) - $started).TotalSeconds, 2) }
    }
    $current = Get-CurrentOrderNumber -OrderChild $OrderChild -PreviousOrderNumber $PreviousOrderNumber
    if ($current -ne "UNKNOWN" -and $current -ne $PreviousOrderNumber) {
      return [pscustomobject]@{ Changed = $true; StopRequested = $false; OrderNumber = $current; WaitSeconds = [Math]::Round(((Get-Date) - $started).TotalSeconds, 2) }
    }
    if (((Get-Date) - $started).TotalSeconds -ge $MaximumWaitSeconds) { break }
    Start-Sleep -Milliseconds 500
  }
  $ocrOrder = Read-VisibleOrderNumberWithOcr -OrderChild $OrderChild -PreviousOrderNumber $PreviousOrderNumber
  if ($ocrOrder -ne "UNKNOWN" -and $ocrOrder -ne $PreviousOrderNumber) {
    return [pscustomobject]@{ Changed = $true; StopRequested = $false; OrderNumber = $ocrOrder; WaitSeconds = [Math]::Round(((Get-Date) - $started).TotalSeconds, 2) }
  }
  return [pscustomobject]@{ Changed = $false; StopRequested = $false; OrderNumber = $PreviousOrderNumber; WaitSeconds = [Math]::Round(((Get-Date) - $started).TotalSeconds, 2) }
}

function Get-OrderImagePath {
  param([string]$OrderNumber)
  if ($OrderNumber -match '^(?i)seq_[0-9]{4}$') {
    return Join-Path $CaptureDir "$($OrderNumber.ToLowerInvariant()).png"
  }
  $safeOrder = [regex]::Replace($OrderNumber.ToUpperInvariant(), "[^A-Z0-9_-]", "_")
  return Join-Path $CaptureDir "$safeOrder.png"
}

function Capture-OrderImage {
  param(
    [object]$OrderWindow,
    [string]$OrderNumber
  )

  $path = Get-OrderImagePath -OrderNumber $OrderNumber
  if (Test-Path -LiteralPath $path) {
    return [pscustomobject]@{ Saved = $false; Path = $path; Reason = "image_already_exists" }
  }
  $size = Save-WindowPng -Handle $OrderWindow.Handle -Path $path
  $file = Get-Item -LiteralPath $path
  if ($file.Length -lt 10000) {
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    throw "Screenshot for $OrderNumber is too small and may be blank."
  }
  $verificationImage = $null
  try {
    $verificationImage = [System.Drawing.Bitmap]::FromFile($path)
    if ($verificationImage.Width -lt 1000 -or $verificationImage.Height -lt 600) {
      throw "Screenshot dimensions are invalid: $($verificationImage.Width)x$($verificationImage.Height)."
    }
  } catch {
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    throw "Screenshot for $OrderNumber is damaged or unreadable: $($_.Exception.Message)"
  } finally {
    if ($null -ne $verificationImage) { $verificationImage.Dispose() }
  }
  return [pscustomobject]@{ Saved = $true; Path = $path; Reason = "captured size=$size bytes=$($file.Length)" }
}

function Get-OrderScreenSignature {
  param([object]$OrderWindow)

  $probePath = Join-Path $LogDir ("screen_probe_{0}_{1}.png" -f $PID, ([guid]::NewGuid().ToString("N")))
  try {
    [void](Save-WindowPng -Handle $OrderWindow.Handle -Path $probePath)
    return (Get-FileHash -LiteralPath $probePath -Algorithm SHA256).Hash
  } finally {
    Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
  }
}

$runStarted = Get-Date
$currentOrder = ""
$capturedCount = 0
$finalStatus = "running"
$stopReason = ""
$statusSequence = Get-NextStatusSequence
$seenOrders = Get-ExistingOrderSet
$capturedCount = $seenOrders.Count

try {
  Clear-StopFlag
  Save-BatchState -Status $finalStatus -CurrentOrder $currentOrder -CapturedCount $capturedCount -StopReason $stopReason

  Write-Log "ERP V4 full batch capture started."
  Write-Log "Build: $BuildVersion"
  Write-Log "Safety: query, navigation, and screenshot only; no add, edit, delete, save, print, or submit operation is used."
  Write-Log "ERP path: $ErpExePath"
  Write-Log "Batch: $BatchDir"
  Write-Log "Mode: $RunMode resume=$IsResume"
  Write-Log "Query condition: start=$DateStart end=$DateEnd prefix=$OrderPrefix"
  Write-Log "Limits: wait=$MinimumWaitSeconds-$MaximumWaitSeconds seconds maximum_orders=$MaximumOrders"
  if ($IsResume) {
    $resumeOrder = [string]$resumeState.current_order
    $resumeNow = Get-Date
    Add-OrderStatus -Sequence $statusSequence -OrderNumber $resumeOrder -Status "resumed" -StartedAt $resumeNow -FinishedAt $resumeNow -ImagePath "" -Reason "resume_query_validated_date=$DateStart..$DateEnd prefix=$OrderPrefix"
    $statusSequence++
    Write-Log "RESUMED: batch=$BatchDir date=$DateStart..$DateEnd prefix=$OrderPrefix completed_orders=$($seenOrders.Count)"
  }

  Invoke-Step -Name "check_or_start_erp" -Action {
    $running = @(Get-Process -Name "GI_System" -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) { return "already_running pid=$($running[0].Id)" }
    if (-not (Test-Path -LiteralPath $ErpExePath)) {
      throw "ERP executable not found: $ErpExePath"
    }
    $process = Start-Process -FilePath $ErpExePath -PassThru
    return "started pid=$($process.Id)"
  } | Out-Null

  $mainWindow = Invoke-Step -Name "wait_for_login_or_main_window" -Action {
    $window = Wait-ForErpWindow -TimeoutSeconds 45
    if ($null -eq $window) {
      return "ERP process started; login window may be waiting but main title is not visible yet"
    }
    return "window=$($window.Title)"
  }

  Write-Host ""
  Write-Host "ERP should now be open on this ERP computer."
  Write-Host "Please enter the account and password manually."
  Write-Host "After login reaches the main ERP screen, return here and press Enter to continue."
  Write-Host "The tool does not save or type any account/password."
  if ([string]::IsNullOrWhiteSpace($LoginContinueFlagPath)) {
    [void](Read-Host "Press Enter after ERP login is complete")
  } else {
    Write-Host "Waiting for the V5 Continue button."
    while (-not (Test-Path -LiteralPath $LoginContinueFlagPath)) {
      if (Test-StopRequested) { throw "Stop requested while waiting for manual ERP login." }
      Start-Sleep -Milliseconds 500
    }
    Remove-Item -LiteralPath $LoginContinueFlagPath -Force -ErrorAction SilentlyContinue
  }

  $mainWindow = Invoke-Step -Name "find_main_erp_window_after_login" -Action {
    $window = Wait-ForErpWindow -TimeoutSeconds $LoginWaitTimeoutSeconds
    if ($null -eq $window) { throw "Main ERP window was not found after login." }
    $script:ErpProcessId = [uint32]$window.ProcessId
    Write-Log "ERP window process id: $script:ErpProcessId"
    Write-ErpProcessDiagnostic -Window $window
    return $window
  }

  Invoke-Step -Name "maximize_main_erp_window" -Action {
    $ok = Activate-Window -Handle $mainWindow.Handle
    if (-not $ok) { throw "Main ERP window could not be activated." }
    return "title=$($mainWindow.Title)"
  } | Out-Null

  Invoke-Step -Name "enter_order_screen" -Action {
    Invoke-ConfirmedTopMenuPath -MainWindow $mainWindow
  } | Out-Null

  $orderWindow = Invoke-Step -Name "confirm_order_window" -Action {
    $window = Wait-ForOrderWindow -TimeoutSeconds $OrderScreenTimeoutSeconds
    if ($null -eq $window) { throw "Order entry window title was not found." }
    return $window
  }

  $orderChild = Invoke-Step -Name "confirm_order_child_window" -Action {
    $child = Wait-ForOrderChildWindow -ParentHandle $orderWindow.Handle -TimeoutSeconds $OrderScreenTimeoutSeconds
    if ($null -eq $child) { throw "Order entry child window was not found after the confirmed top-menu path." }
    return $child
  }

  Invoke-Step -Name "query_gou_orders" -Action {
    Invoke-OrderQuery -ParentWindow $orderWindow -OrderChild $orderChild
  } | Out-Null

  $orderWindow = Invoke-Step -Name "confirm_order_window_after_query" -Action {
    $window = Wait-ForOrderWindow -TimeoutSeconds $OrderScreenTimeoutSeconds
    if ($null -eq $window) { throw "Order entry window was not found after query." }
    return $window
  }

  $orderChild = Invoke-Step -Name "confirm_order_child_after_query" -Action {
    $child = Wait-ForOrderChildWindow -ParentHandle $orderWindow.Handle -TimeoutSeconds $OrderScreenTimeoutSeconds
    if ($null -eq $child) { throw "Order entry child window was not found after query." }
    return $child
  }

  Invoke-Step -Name "maximize_order_window" -Action {
    Maximize-OrderChildWindow -OrderChild $orderChild
  } | Out-Null

  $orderChild = Invoke-Step -Name "refresh_active_order_child_after_maximize" -Action {
    $child = Wait-ForOrderChildWindow -ParentHandle $orderWindow.Handle -TimeoutSeconds $OrderScreenTimeoutSeconds
    if ($null -eq $child) { throw "Active order child window was not found after maximize." }
    return $child
  }
  Write-ErpProcessDiagnostic -Window $orderChild

  $querySelection = Invoke-Step -Name "select_first_query_result" -Action {
    Select-FirstQueryResult -ParentWindow $orderWindow -OrderChild $orderChild
  }
  if (-not $querySelection.HasResults) {
    $finalStatus = "completed"
    $stopReason = "query_returned_zero_orders"
    Save-BatchState -Status $finalStatus -CurrentOrder "" -CapturedCount 0 -StopReason $stopReason
    Write-BatchSummary -Status $finalStatus -CapturedCount 0 -CurrentOrder "" -StopReason $stopReason -StartedAt $runStarted
    if ($StepRecords.Count -gt 0) { $StepRecords | Export-Csv -LiteralPath $StepPath -NoTypeInformation -Encoding UTF8 }
    Write-Progress -Activity "ERP V4 batch screenshot" -Completed
    Write-Host "Query completed with zero GOU orders. No screenshot was created."
    Write-Host "Batch: $BatchDir"
    return
  }
  $currentOrder = [string]$querySelection.OrderNumber
  if ($currentOrder -notmatch '^(?i)(GOU[0-9A-Z-]{6,20}|seq_[0-9]{4})$') {
    throw "The selected order identifier is invalid: $currentOrder"
  }

  $unchangedAttempts = 0
  $visualRowIndex = 0
  while ($true) {
    if (Test-StopRequested) {
      $finalStatus = "stopped_by_user"
      $stopReason = "STOP_V4_NOW.flag detected before processing the current order"
      $now = Get-Date
      Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "stopped" -StartedAt $now -FinishedAt $now -ImagePath "" -Reason $stopReason
      $statusSequence++
      break
    }

    if ($currentOrder -notmatch '^(?i)(GOU[0-9A-Z-]{6,20}|seq_[0-9]{4})$') {
      throw "The current order identifier is invalid: $currentOrder"
    }

    $isProvisionalOrder = $currentOrder -match '^(?i)seq_[0-9]{4}$'
    if (-not $isProvisionalOrder) {
      $verifiedOrder = Get-CurrentOrderNumber -OrderChild $orderChild
      if ($verifiedOrder -ne $currentOrder) {
        $visibleOrder = Read-VisibleOrderNumberWithOcr -OrderChild $orderChild -PreviousOrderNumber ""
        if ($visibleOrder -eq $currentOrder) {
          Write-Log "Pre-capture verification accepted visible OCR because UI Automation was stale: expected=$currentOrder ui_automation=$verifiedOrder"
          $verifiedOrder = $visibleOrder
        } elseif ($verifiedOrder -eq "UNKNOWN") {
          throw "The active order child did not expose a reliable order number before capture; visible_ocr=$visibleOrder expected=$currentOrder."
        } else {
          throw "Order number changed unexpectedly before capture: expected=$currentOrder ui_automation=$verifiedOrder visible_ocr=$visibleOrder"
        }
      }
    } else {
      Write-Log "Pre-capture order-number verification deferred to normal OCR for provisional image $currentOrder."
    }

    $itemStarted = Get-Date
    if ($seenOrders.ContainsKey($currentOrder)) {
      $itemFinished = Get-Date
      Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "skipped" -StartedAt $itemStarted -FinishedAt $itemFinished -ImagePath (Get-OrderImagePath -OrderNumber $currentOrder) -Reason "already_captured_resume_scan"
      $statusSequence++
      Write-Log "SKIP: $currentOrder was already captured in this batch."
    } else {
      $capture = Capture-OrderImage -OrderWindow $orderWindow -OrderNumber $currentOrder
      $itemFinished = Get-Date
      if (-not $capture.Saved) {
        Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "skipped" -StartedAt $itemStarted -FinishedAt $itemFinished -ImagePath $capture.Path -Reason $capture.Reason
      } else {
        $seenOrders[$currentOrder] = $true
        $capturedCount = $seenOrders.Count
        Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "success" -StartedAt $itemStarted -FinishedAt $itemFinished -ImagePath $capture.Path -Reason $capture.Reason
        Write-Log "CAPTURED: order=$currentOrder count=$capturedCount image=$($capture.Path)"
      }
      $statusSequence++
    }

    Save-BatchState -Status $finalStatus -CurrentOrder $currentOrder -CapturedCount $capturedCount -StopReason $stopReason

    # A stop request is honored after the current order is recorded and before
    # any next-order key is sent.
    if (Test-StopRequested) {
      $finalStatus = "stopped_by_user"
      $stopReason = "STOP_V4_NOW.flag detected after completing current order; no next-order key sent"
      $now = Get-Date
      Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "stopped" -StartedAt $now -FinishedAt $now -ImagePath "" -Reason $stopReason
      $statusSequence++
      Save-BatchState -Status $finalStatus -CurrentOrder $currentOrder -CapturedCount $capturedCount -StopReason $stopReason
      break
    }

    $elapsed = [Math]::Round(((Get-Date) - $runStarted).TotalSeconds, 1)
    $percent = [Math]::Min(100, [Math]::Round(($capturedCount / [double]$MaximumOrders) * 100, 1))
    Write-Host ("Current order: {0} | completed: {1} | elapsed: {2}s" -f $currentOrder, $capturedCount, $elapsed)
    Write-Progress -Activity "ERP V4 batch screenshot" -Status ("{0} | completed {1} | {2}s" -f $currentOrder, $capturedCount, $elapsed) -PercentComplete $percent

    if ($capturedCount -ge $MaximumOrders) {
      $finalStatus = "completed_limit"
      $stopReason = "maximum order limit reached: $MaximumOrders"
      $now = Get-Date
      Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "stopped" -StartedAt $now -FinishedAt $now -ImagePath "" -Reason $stopReason
      $statusSequence++
      break
    }

    [void](Invoke-OrderListDown -OrderChild $orderChild -CurrentOrder $currentOrder -VisualRowIndex $visualRowIndex)
    $change = Wait-ForOrderNumberChange -OrderChild $orderChild -PreviousOrderNumber $currentOrder

    if ($change.StopRequested) {
      $finalStatus = "stopped_by_user"
      $stopReason = "STOP_V4_NOW.flag detected while waiting for the next order"
      $now = Get-Date
      Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "stopped" -StartedAt $now -FinishedAt $now -ImagePath "" -Reason $stopReason
      $statusSequence++
      break
    }

    if ($change.Changed) {
      Write-Log "ORDER TRANSITION VERIFIED method=CONTROL_VK_DOWN previous=$currentOrder current=$($change.OrderNumber)"
      $currentOrder = $change.OrderNumber
      $unchangedAttempts = 0
      $visualRowIndex++
      continue
    }

    Write-Log "CONTROL VK_DOWN did not change the visible order number; using next-row arrow click fallback. previous=$currentOrder"
    if (Click-NextOrderRowFallback -OrderChild $orderChild -CurrentOrder $currentOrder -VisualRowIndex $visualRowIndex) {
      $fallbackChange = Wait-ForOrderNumberChange -OrderChild $orderChild -PreviousOrderNumber $currentOrder
      if ($fallbackChange.StopRequested) {
        $finalStatus = "stopped_by_user"
        $stopReason = "STOP_V4_NOW.flag detected while waiting after next-row fallback"
        $now = Get-Date
        Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "stopped" -StartedAt $now -FinishedAt $now -ImagePath "" -Reason $stopReason
        $statusSequence++
        break
      }
      if ($fallbackChange.Changed) {
        Write-Log "ORDER TRANSITION VERIFIED method=NEXT_ROW_ARROW_CLICK previous=$currentOrder current=$($fallbackChange.OrderNumber)"
        $currentOrder = $fallbackChange.OrderNumber
        $unchangedAttempts = 0
        $visualRowIndex++
        continue
      }
    }

    $unchangedAttempts++
    $now = Get-Date
    Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "skipped" -StartedAt $now -FinishedAt $now -ImagePath "" -Reason "order_number_unchanged_attempt_$unchangedAttempts"
    $statusSequence++
    Write-Log "UNCHANGED: order=$currentOrder attempt=$unchangedAttempts/3 wait=$($change.WaitSeconds)s"
    if ($unchangedAttempts -ge 3) {
      $belowSelection = Get-VisibleOrdersBelowSelection -OrderChild $orderChild
      $endDecision = Get-EndOfListDecision -UnchangedAttempts $unchangedAttempts -CheckReliable $belowSelection.Reliable -OrdersBelow @($belowSelection.Orders)
      if ($endDecision -eq "fail_unverified") {
        throw "The order number stayed unchanged three times, but the end of the ERP list could not be verified: $($belowSelection.Reason)"
      }
      if ($endDecision -eq "fail_orders_below") {
        throw "The order number stayed unchanged three times while more orders were still visible below the selected row: $($belowSelection.Orders -join ','). The Down Arrow did not reach the ERP order list."
      }
      if ($endDecision -ne "complete") { throw "Unexpected end-of-list decision: $endDecision" }
      $finalStatus = "completed"
      $stopReason = "end_of_list_after_three_unchanged_attempts"
      $now = Get-Date
      Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "completed" -StartedAt $now -FinishedAt $now -ImagePath "" -Reason $stopReason
      $statusSequence++
      break
    }
  }

  Save-BatchState -Status $finalStatus -CurrentOrder $currentOrder -CapturedCount $capturedCount -StopReason $stopReason
  Write-BatchSummary -Status $finalStatus -CapturedCount $capturedCount -CurrentOrder $currentOrder -StopReason $stopReason -StartedAt $runStarted
  if ($StepRecords.Count -gt 0) { $StepRecords | Export-Csv -LiteralPath $StepPath -NoTypeInformation -Encoding UTF8 }
  Write-Progress -Activity "ERP V4 batch screenshot" -Completed

  Write-Host ""
  Write-Host "V4 batch finished."
  Write-Host "Status: $finalStatus"
  Write-Host "Captured: $capturedCount"
  Write-Host "Reason: $stopReason"
  Write-Host "Batch: $BatchDir"
} catch {
  $reason = $_.Exception.Message
  try {
    Write-Log "ERROR: $reason"
    $finalStatus = "failed"
    $stopReason = $reason
    $now = Get-Date
    Add-OrderStatus -Sequence $statusSequence -OrderNumber $currentOrder -Status "failed" -StartedAt $now -FinishedAt $now -ImagePath "" -Reason $reason
    Save-BatchState -Status $finalStatus -CurrentOrder $currentOrder -CapturedCount $capturedCount -StopReason $stopReason
    Write-BatchSummary -Status $finalStatus -CapturedCount $capturedCount -CurrentOrder $currentOrder -StopReason $stopReason -StartedAt $runStarted
    if ($StepRecords.Count -gt 0) { $StepRecords | Export-Csv -LiteralPath $StepPath -NoTypeInformation -Encoding UTF8 }
    Set-Content -LiteralPath $ErrorPath -Value $reason -Encoding UTF8
    Write-Progress -Activity "ERP V4 batch screenshot" -Completed
  } catch {}
  throw
}
