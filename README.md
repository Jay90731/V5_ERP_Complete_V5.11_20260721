# Wedding Order Automation V5.11

Windows ERP automation for collecting GOU orders, running local OCR, validating
order details, tracking daily changes, and rebuilding the official department
4001 Excel workbook.

## Workflow

1. Start or activate the local ERP.
2. Wait for the user to complete ERP login manually.
3. Open and maximize the external gift-order screen.
4. Query either the latest seven days or the latest six months.
5. Capture each unique order with stop and resume support.
6. Run local Tesseract OCR and validate department, date, category, and details.
7. Compare the completed batch with order history.
8. Rebuild monthly Excel sheets and preserve pending-review decisions.

## Safety

- ERP credentials are never stored or entered by the tool.
- The tool does not create, edit, delete, save, or print ERP records.
- Official history and Excel files update only after capture, OCR, and validation
  stages complete.
- Completed screenshots are retained for seven days. Unfinished resumable runs
  are not removed.
- ERP screenshots, customer/order data, Excel files, product lookup data,
  runtime binaries, and local state are excluded from Git.

## Local setup

This repository contains source code only. The deployment package additionally
needs local Python, Tesseract OCR, required language data, and the configured
ERP executable.

1. Copy `V5_daily_full_flow/v5_config.example.json` to
   `V5_daily_full_flow/v5_config.json`.
2. Adjust the ERP path and local runtime candidates.
3. Provide `reference_data/MENUITEM.XLS` locally for product-name lookup.
4. Run `CHECK_ENVIRONMENT.bat`.
5. Run `START_V5.bat`.

See `使用說明.txt` for the operational procedure used by the ERP workstation.

