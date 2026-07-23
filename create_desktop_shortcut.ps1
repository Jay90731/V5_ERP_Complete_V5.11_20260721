$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath("Desktop")
$Target = Join-Path $Root "START_V5.bat"
$ShortcutPath = Join-Path $Desktop "Wedding_Order_V5.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "Wedding Order V5 Daily Flow"
$Shortcut.Save()
Write-Host "Desktop shortcut created: $ShortcutPath"
