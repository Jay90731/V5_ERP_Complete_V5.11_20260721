param(
  [Parameter(Mandatory = $true)]
  [string]$ProjectRoot
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$candidates = @()
$direct = Join-Path $root "runtime\python\python.exe"
if (Test-Path -LiteralPath $direct -PathType Leaf) {
  $candidates += Get-Item -LiteralPath $direct
}
$dist = Join-Path $root "dist"
if (Test-Path -LiteralPath $dist -PathType Container) {
  $candidates += Get-ChildItem -LiteralPath $dist -Filter python.exe -File -Recurse |
    Where-Object { $_.FullName -match '[\\/]runtime[\\/]python[\\/]python\.exe$' }
}
$runtime = $candidates | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if ($null -eq $runtime) {
  throw "找不到專案內建 Python runtime（不使用系統 Python）。"
}
& $runtime.FullName -X utf8 -c "import sqlite3, xlrd; print('BHQ_RUNTIME_OK')" | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "專案內建 Python 缺少 BHQ 所需模組：$($runtime.FullName)"
}
Write-Output $runtime.FullName
