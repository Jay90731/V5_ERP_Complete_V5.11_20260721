function Get-GouOrderNumbersFromOcrLines {
  param([object[]]$Lines)

  $orders = New-Object System.Collections.Generic.List[string]
  foreach ($line in @($Lines)) {
    $compactLine = [regex]::Replace(([string]$line).ToUpperInvariant(), "[^A-Z0-9]", "")
    foreach ($match in [regex]::Matches($compactLine, "GOU[0-9]{8}")) {
      $orders.Add($match.Value)
    }
  }
  return @($orders | Select-Object -Unique)
}

function Get-EndOfListDecision {
  param(
    [int]$UnchangedAttempts,
    [bool]$CheckReliable,
    [object[]]$OrdersBelow
  )

  if ($UnchangedAttempts -lt 3) { return "retry" }
  if (-not $CheckReliable) { return "fail_unverified" }
  if (@($OrdersBelow).Count -gt 0) { return "fail_orders_below" }
  return "complete"
}

function Get-OrderChildMaximizePoint {
  param(
    [int]$Right,
    [int]$Top
  )

  return [pscustomobject]@{
    X = [int]($Right - 46)
    Y = [int]($Top + 10)
  }
}
