param([ValidateSet('Validate','Web','ERP')][string]$Action='Validate')
$ErrorActionPreference = 'Stop'
$Expected = 'C:\Users\auntstella\Desktop\V6_DB_WEB'
$Root = (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
if ($Root.TrimEnd('\') -ine $Expected) { throw "Only the accepted fixed project may run: $Expected" }
$Db = Join-Path $Expected 'data\orders.db'
if (-not (Test-Path -LiteralPath $Db -PathType Leaf)) { throw "Existing formal DB missing; will not create it: $Db" }
if ((Get-Item -LiteralPath $Db).Length -le 0) { throw 'Existing formal DB is empty.' }
$Python = Join-Path $Root 'runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Bundled Python missing.' }
$erp = Get-Content -LiteralPath (Join-Path $Root 'phase3_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$bhq = Get-Content -LiteralPath (Join-Path $Root 'bhq_watcher_formal.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($erp.database_path -ine $Db -or $erp.database_mode -ne 'formal' -or $bhq.database -ine $Db) { throw 'ERP/BHQ database paths are not unified.' }
if ($bhq.formal_enabled -ne $false -or $bhq.formal_enable_confirmation) { throw 'BHQ must remain disabled in this deployment phase.' }
& $Python -X utf8 (Join-Path $Root 'tools\bhq_formal_database_guard.py') --project-root $Root --config (Join-Path $Root 'bhq_watcher_formal.json')
if ($LASTEXITCODE -ne 0) { throw 'Fixed database preflight failed.' }
if ($Action -eq 'Validate') { exit 0 }
$Receipt = Join-Path $Root 'deployment_acceptance.json'
if (-not (Test-Path -LiteralPath $Receipt -PathType Leaf)) { throw 'No approved deployment acceptance receipt. Services remain stopped.' }
$approval = Get-Content -LiteralPath $Receipt -Raw -Encoding UTF8 | ConvertFrom-Json
if ($approval.status -ne 'approved' -or $approval.project_root -ine $Root -or -not $approval.latest_r3_capture_sha256 -or -not $approval.approved_at) {
  throw 'Deployment receipt incomplete. Services remain stopped.'
}
Set-Location -LiteralPath $Root
if ($Action -eq 'Web') {
  & $Python -X utf8 (Join-Path $Root 'web_v1\app.py') --database $Db --host 127.0.0.1 --port 8765
  exit $LASTEXITCODE
}
& (Join-Path $Root 'phase3_launcher.ps1')
