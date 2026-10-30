param([string]$Destination = "")

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Builder = Join-Path $ProjectRoot "build_unified_final_package.ps1"
if (-not (Test-Path -LiteralPath $Builder -PathType Leaf)) {
  throw "找不到統一正式包封裝器：$Builder"
}
& $Builder -Destination $Destination
