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

function Get-QueryFirstOrderDecision {
  param(
    [object[]]$DetectedOrders,
    [string]$SelectedOrder,
    [bool]$DetectionReliable,
    [bool]$ClickSucceeded = $true,
    [bool]$FocusMatched = $true,
    [string]$ProvisionalOrder = "seq_0001"
  )

  $orders = @(
    $DetectedOrders |
      ForEach-Object { ([string]$_).ToUpperInvariant() } |
      Where-Object { $_ -match '^GOU[0-9]{8}$' } |
      Select-Object -Unique
  )
  $selected = ([string]$SelectedOrder).ToUpperInvariant()
  if ($DetectionReliable -and $orders.Count -eq 0) {
    return [pscustomobject]@{
      Action = "empty"
      Reason = "query_returned_zero_orders"
      OrderNumber = ""
      OrderNumberKnown = $false
    }
  }
  if (-not $ClickSucceeded) {
    return [pscustomobject]@{
      Action = "fail"
      Reason = "first_query_result_click_failed"
      OrderNumber = $selected
      OrderNumberKnown = $false
    }
  }
  if (-not $FocusMatched) {
    return [pscustomobject]@{
      Action = "fail"
      Reason = "first_query_result_focus_failed"
      OrderNumber = $selected
      OrderNumberKnown = $false
    }
  }
  if ($selected -notmatch '^GOU[0-9A-Z-]{6,20}$') {
    return [pscustomobject]@{
      Action = "provisional"
      Reason = "header_order_number_unreadable_capture_first"
      OrderNumber = $ProvisionalOrder
      OrderNumberKnown = $false
    }
  }
  return [pscustomobject]@{
    Action = "start"
    Reason = "first_query_result_focused_header_order_read"
    OrderNumber = $selected
    OrderNumberKnown = $true
  }
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
