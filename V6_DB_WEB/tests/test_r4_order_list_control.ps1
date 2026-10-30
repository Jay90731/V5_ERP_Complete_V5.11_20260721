$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class R4OrderListSimulationNative {
  [StructLayout(LayoutKind.Sequential)]
  public struct POINT { public int X; public int Y; }

  [StructLayout(LayoutKind.Sequential)]
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

  [DllImport("user32.dll")]
  public static extern IntPtr WindowFromPoint(POINT point);

  [DllImport("user32.dll")]
  public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern int GetClassName(IntPtr hWnd, StringBuilder value, int count);

  [DllImport("user32.dll")]
  public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

  [DllImport("user32.dll")]
  public static extern bool AttachThreadInput(uint attach, uint attachTo, bool value);

  [DllImport("user32.dll")]
  public static extern IntPtr SetFocus(IntPtr hWnd);

  [DllImport("kernel32.dll")]
  public static extern uint GetCurrentThreadId();

  [DllImport("user32.dll")]
  public static extern bool PostMessage(IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam);

  public const uint WM_KEYDOWN = 0x0100;
  public const uint WM_KEYUP = 0x0101;
  public const int VK_DOWN = 0x28;
}
'@

$form = New-Object System.Windows.Forms.Form
$form.Text = "R4 order-list control simulation"
$form.StartPosition = "Manual"
$form.Location = New-Object System.Drawing.Point(100, 100)
$form.Size = New-Object System.Drawing.Size(500, 400)
$list = New-Object System.Windows.Forms.ListBox
$list.Location = New-Object System.Drawing.Point(20, 20)
$list.Size = New-Object System.Drawing.Size(260, 300)
[void]$list.Items.Add("GOU26070517")
[void]$list.Items.Add("GOU26070518")
[void]$list.Items.Add("GOU26070519")
$list.SelectedIndex = 0
$form.Controls.Add($list)

try {
  $form.Show()
  [System.Windows.Forms.Application]::DoEvents()
  Start-Sleep -Milliseconds 200

  $rect = New-Object R4OrderListSimulationNative+RECT
  [void][R4OrderListSimulationNative]::GetWindowRect($list.Handle, [ref]$rect)
  $point = New-Object R4OrderListSimulationNative+POINT
  $point.X = $rect.Left + 6
  $point.Y = $rect.Top + [Math]::Max(6, [int]($list.ItemHeight / 2))
  $control = [R4OrderListSimulationNative]::WindowFromPoint($point)
  if ($control -ne $list.Handle) {
    throw "Simulation failed: WindowFromPoint did not return the list control."
  }

  $processId = [uint32]0
  $targetThread = [R4OrderListSimulationNative]::GetWindowThreadProcessId($control, [ref]$processId)
  $currentThread = [R4OrderListSimulationNative]::GetCurrentThreadId()
  $attached = $false
  try {
    if ($currentThread -ne $targetThread) {
      $attached = [R4OrderListSimulationNative]::AttachThreadInput($currentThread, $targetThread, $true)
    }
    [void][R4OrderListSimulationNative]::SetFocus($control)
    [void][R4OrderListSimulationNative]::PostMessage(
      $control,
      [R4OrderListSimulationNative]::WM_KEYDOWN,
      [IntPtr][R4OrderListSimulationNative]::VK_DOWN,
      [IntPtr]::Zero
    )
    [void][R4OrderListSimulationNative]::PostMessage(
      $control,
      [R4OrderListSimulationNative]::WM_KEYUP,
      [IntPtr][R4OrderListSimulationNative]::VK_DOWN,
      [IntPtr]::Zero
    )
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 200
  } finally {
    if ($attached) {
      [void][R4OrderListSimulationNative]::AttachThreadInput($currentThread, $targetThread, $false)
    }
  }

  if ([string]$list.SelectedItem -ne "GOU26070518") {
    throw "Simulation failed: expected GOU26070518, actual $($list.SelectedItem)."
  }
  $classBuilder = New-Object System.Text.StringBuilder 512
  [void][R4OrderListSimulationNative]::GetClassName($control, $classBuilder, $classBuilder.Capacity)
  Write-Output "R4_ORDER_LIST_CONTROL_SIMULATION_OK previous=GOU26070517 current=$($list.SelectedItem) pid=$processId handle=$($control.ToInt64()) class=[$classBuilder]"
} finally {
  $form.Close()
  $list.Dispose()
  $form.Dispose()
}
