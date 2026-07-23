$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $root "components\erp_capture\capture_guard_helpers.ps1")

function Assert-Equal {
  param([object]$Expected, [object]$Actual, [string]$Name)
  if ([string]$Expected -ne [string]$Actual) {
    throw "$Name failed: expected=[$Expected] actual=[$Actual]"
  }
  Write-Output "PASS $Name"
}

$orders = @(Get-GouOrderNumbersFromOcrLines -Lines @(
  "gee GOU260702 11",
  "noise GOU26070212 noise",
  "gee GOU260702 11"
))
Assert-Equal -Expected 2 -Actual $orders.Count -Name "OCR order count and de-duplication"
Assert-Equal -Expected "GOU26070211" -Actual $orders[0] -Name "OCR spaced order normalization"
Assert-Equal -Expected "retry" -Actual (Get-EndOfListDecision -UnchangedAttempts 2 -CheckReliable $true -OrdersBelow @()) -Name "Two unchanged attempts retry"
Assert-Equal -Expected "fail_unverified" -Actual (Get-EndOfListDecision -UnchangedAttempts 3 -CheckReliable $false -OrdersBelow @()) -Name "Unverified list end fails"
Assert-Equal -Expected "fail_orders_below" -Actual (Get-EndOfListDecision -UnchangedAttempts 3 -CheckReliable $true -OrdersBelow @("GOU26070212")) -Name "Visible order below fails"
Assert-Equal -Expected "complete" -Actual (Get-EndOfListDecision -UnchangedAttempts 3 -CheckReliable $true -OrdersBelow @()) -Name "Verified empty list completes"
$maximizePoint = Get-OrderChildMaximizePoint -Right 1482 -Top 177
Assert-Equal -Expected 1436 -Actual $maximizePoint.X -Name "ERP child maximize button X"
Assert-Equal -Expected 187 -Actual $maximizePoint.Y -Name "ERP child maximize button Y"

Write-Output "All capture guard tests passed."
