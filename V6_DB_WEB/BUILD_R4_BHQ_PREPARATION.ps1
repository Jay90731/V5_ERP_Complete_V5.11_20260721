$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$Name = "R4_BHQ_DEPLOYMENT_PREPARATION_$Stamp"
$Kit = Join-Path $Root "dist\$Name"
$Program = Join-Path $Kit 'V6_DB_WEB'
$R4Source = Join-Path $Root 'dist\V6_DB_WEB_DUAL_R4_20260826_144703'
$RuntimeSource = Join-Path $R4Source 'runtime'
if (-not (Test-Path -LiteralPath (Join-Path $RuntimeSource 'python\python.exe'))) { throw 'Approved R4 bundled runtime missing.' }
$ComponentManifest = Join-Path $R4Source 'components\COMPONENT_SOURCES.json'
$components = Get-Content -LiteralPath $ComponentManifest -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($entry in $components.files.PSObject.Properties) {
  $actual = (Get-FileHash -LiteralPath (Join-Path $Root ('components\' + $entry.Name)) -Algorithm SHA256).Hash
  if ($actual -ne $entry.Value) { throw "Workspace component differs from accepted R4: $($entry.Name). Do not overwrite it." }
}
if (Test-Path -LiteralPath $Kit) { throw 'New preparation directory required.' }
New-Item -ItemType Directory -Path $Program -Force | Out-Null

function Copy-File([string]$Relative) {
  $source = Join-Path $Root $Relative
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing code: $source" }
  $target = Join-Path $Program $Relative
  New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
  Copy-Item -LiteralPath $source -Destination $target
}
function Copy-CodeTree([string]$Relative) {
  $source = Join-Path $Root $Relative
  foreach ($item in Get-ChildItem -LiteralPath $source -File -Recurse) {
    if ($item.FullName -match '[\\/]__pycache__[\\/]' -or $item.Extension -in @('.pyc','.db','.sqlite','.xls','.xlsx','.png')) { continue }
    Copy-File ($item.FullName.Substring($Root.Length + 1))
  }
}
foreach ($relative in @('src','migrations','components','web_v1\wedding_web','web_v1\static')) { Copy-CodeTree $relative }
# Package-only metadata: current source bytes match R4; workspace metadata is stale.
Copy-Item -LiteralPath $ComponentManifest -Destination (Join-Path $Program 'components\COMPONENT_SOURCES.json')
foreach ($relative in @(
  'v6.py','pyproject.toml','phase3_config.json','phase3_labels.json','phase3_worker.ps1','phase3_launcher.ps1',
  'check_phase3_environment.ps1','formal_database_modes.json','formal_database_tools.ps1',
  'bhq_watch.py','bhq_watcher_formal.json','web_v1\app.py','web_v1\validate_database.py',
  'tools\r4_deployment_preflight.py','tools\verify_r4_preparation_package.py','tools\bhq_formal_database_guard.py','tools\Resolve-BhqRuntime.ps1',
  'tests\test_r4_deployment_preflight.py','tests\__init__.py','docs\BHQ_LOCAL_FOLDER_WATCHER.md'
)) { Copy-File $relative }
Copy-Item -LiteralPath $RuntimeSource -Destination (Join-Path $Program 'runtime') -Recurse
Copy-Item -LiteralPath (Join-Path $Root 'deployment\r4_bhq\DEPLOYMENT_PLAN.md') -Destination (Join-Path $Kit 'DEPLOYMENT_PLAN.md')
Copy-Item -LiteralPath (Join-Path $Root 'deployment\r4_bhq\CAPTURE_LATEST_R3.ps1') -Destination (Join-Path $Kit 'CAPTURE_LATEST_R3.ps1')
Copy-Item -LiteralPath (Join-Path $Root 'deployment\r4_bhq\RUN_FIXED_UNIFIED.ps1') -Destination (Join-Path $Program 'RUN_FIXED_UNIFIED.ps1')

$fixedDb = 'C:\Users\auntstella\Desktop\V6_DB_WEB\data\orders.db'
$phase = Get-Content -LiteralPath (Join-Path $Program 'phase3_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$phase.database_path = $fixedDb
$phase.database_mode = 'formal'
$phase.python_candidates = @('runtime\python\python.exe')
$phase.tesseract_candidates = @('runtime\tesseract\tesseract.exe')
$phase.tessdata_candidates = @('runtime\tesseract\tessdata')
$phase | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Program 'phase3_config.json') -Encoding UTF8
$modes = Get-Content -LiteralPath (Join-Path $Program 'formal_database_modes.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$modes.formal.database = $fixedDb
$modes | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Program 'formal_database_modes.json') -Encoding UTF8
$bhq = Get-Content -LiteralPath (Join-Path $Program 'bhq_watcher_formal.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($bhq.database -ine $fixedDb -or $bhq.formal_enabled -ne $false -or $bhq.formal_enable_confirmation) { throw 'BHQ disabled fixed configuration required.' }
foreach ($entry in @(@('START_WEDDING_ORDERS_WEB.bat','Web'),@('START_DAILY_AND_6_MONTHS.bat','ERP'),@('VALIDATE_FIXED_DATABASE.bat','Validate'))) {
  $batch = "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0RUN_FIXED_UNIFIED.ps1`" -Action $($entry[1])`r`npause`r`n"
  Set-Content -LiteralPath (Join-Path $Program $entry[0]) -Value $batch -Encoding ASCII
}
@'
R4 + BHQ DEPLOYMENT PREPARATION ONLY

Do not copy this over R3 or the formal database. Do not start services.
Read DEPLOYMENT_PLAN.md and obtain approval first.
No database, Excel or business images are included.
Latest target R3 snapshot and reconciliation are still required.
CAPTURE_LATEST_R3.ps1 is a future maintenance capture tool, not an installer.
V6_DB_WEB contains code/runtime only. There is no data directory.
BHQ is disabled. No watcher scheduled-task installer is included.
Service launchers refuse to run without a future approved deployment_acceptance.json.
'@ | Set-Content -LiteralPath (Join-Path $Kit 'READ_FIRST.txt') -Encoding UTF8

$forbidden = @(Get-ChildItem -LiteralPath $Kit -Recurse -File | Where-Object {
  $_.Extension -in @('.db','.sqlite','.xls','.xlsx') -or $_.Name -match '^(ACTIVATE_BHQ|INSTALL_BHQ|promote_r[34]|replay_r4)'
})
if ($forbidden.Count -or (Test-Path -LiteralPath (Join-Path $Program 'data')) -or (Test-Path -LiteralPath (Join-Path $Program 'deployment_acceptance.json'))) {
  throw 'Preparation kit contains forbidden live data/activation material.'
}
$items = @(Get-ChildItem -LiteralPath $Kit -File -Recurse)
$manifest = [ordered]@{
  package = $Name
  status = 'prepared_only_requires_latest_R3_and_approval'
  fixed_root = 'C:\Users\auntstella\Desktop\V6_DB_WEB'
  included_database = $false
  bhq_enabled = $false
  install_or_start_executed = $false
  component_manifest_source = 'V6_DB_WEB_DUAL_R4_20260826_144703'
  erp_ocr_bytes_match_accepted_r4 = $true
  generated_at = (Get-Date).ToString('o')
  files = @($items | ForEach-Object {
    [ordered]@{ path=$_.FullName.Substring($Kit.Length+1).Replace('\','/'); bytes=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
  })
}
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Kit 'PACKAGE_MANIFEST.json') -Encoding UTF8
$zip = "$Kit.zip"
Compress-Archive -LiteralPath $Kit -DestinationPath $zip -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
Set-Content -LiteralPath "$zip.sha256.txt" -Value "$hash  $(Split-Path -Leaf $zip)" -Encoding ASCII
Write-Output "PACKAGE=$zip"
Write-Output "SHA256=$hash"
