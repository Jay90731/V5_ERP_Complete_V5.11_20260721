$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class R3WindowSimulationNative {
  [DllImport("user32.dll")]
  public static extern IntPtr GetParent(IntPtr hWnd);

  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern int GetClassName(IntPtr hWnd, StringBuilder value, int count);

  [DllImport("user32.dll", EntryPoint = "SendMessageW")]
  public static extern IntPtr SendMessagePtr(IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam);

  [DllImport("user32.dll")]
  public static extern bool IsZoomed(IntPtr hWnd);

  public const uint WM_MDIMAXIMIZE = 0x0225;
}
'@

function Get-SimulatedWindowClass {
  param([IntPtr]$Handle)
  $builder = New-Object System.Text.StringBuilder 512
  [void][R3WindowSimulationNative]::GetClassName($Handle, $builder, $builder.Capacity)
  return $builder.ToString()
}

$parent = New-Object System.Windows.Forms.Form
$parent.Text = "R3 MDI simulation"
$parent.IsMdiContainer = $true
$parent.Size = New-Object System.Drawing.Size(1200, 800)
$child = New-Object System.Windows.Forms.Form
$child.Text = "External gift order entry simulation"
$child.Size = New-Object System.Drawing.Size(520, 360)
$child.MdiParent = $parent

try {
  $parent.Show()
  $child.Show()
  [System.Windows.Forms.Application]::DoEvents()
  Start-Sleep -Milliseconds 200

  $current = $child.Handle
  $mdiClient = [IntPtr]::Zero
  $classes = New-Object System.Collections.Generic.List[string]
  for ($level = 0; $level -lt 16 -and $current -ne [IntPtr]::Zero; $level++) {
    $className = Get-SimulatedWindowClass -Handle $current
    $classes.Add($className)
    if ($className -match '(?i)MDICLIENT') {
      $mdiClient = $current
      break
    }
    $current = [R3WindowSimulationNative]::GetParent($current)
  }
  if ($mdiClient -eq [IntPtr]::Zero) {
    throw "Simulation failed: MDIClient was not found in parent hierarchy: $($classes -join ' > ')"
  }

  [void][R3WindowSimulationNative]::SendMessagePtr(
    $mdiClient,
    [R3WindowSimulationNative]::WM_MDIMAXIMIZE,
    $child.Handle,
    [IntPtr]::Zero
  )
  [System.Windows.Forms.Application]::DoEvents()
  Start-Sleep -Milliseconds 250
  if (-not [R3WindowSimulationNative]::IsZoomed($child.Handle)) {
    throw "Simulation failed: WM_MDIMAXIMIZE did not maximize the MDI child."
  }
  Write-Output "R3_WINDOW_HIERARCHY_SIMULATION_OK classes=$($classes -join '>') child=$($child.Width)x$($child.Height)"
} finally {
  $child.Close()
  $parent.Close()
  $child.Dispose()
  $parent.Dispose()
}
