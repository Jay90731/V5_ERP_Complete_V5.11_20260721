# Wedding Order System V6

Windows ERP automation and a local web application for collecting GOU orders,
running OCR, reviewing exceptions, and storing ERP and BHQ orders in one SQLite
database. The previous V5.11 source is retained for reference; active development
is under [`V6_DB_WEB`](V6_DB_WEB/).

## Workflow

1. Start or activate the local ERP.
2. Wait for the user to complete ERP login manually.
3. Open and maximize the external gift-order screen.
4. Query either the latest seven days or the latest six months.
5. Capture each unique order with stop and resume support.
6. Run local Tesseract OCR and validate department, date, category, and details.
7. Write new or changed versions to the unified SQLite database.
8. Review OCR exceptions in the local website and write corrections back safely.
9. Import BHQ Excel exports into the same database through the guarded watcher.

## Safety

- ERP credentials are never stored or entered by the tool.
- The tool does not create, edit, delete, save, or print ERP records.
- The official SQLite database updates only after capture, OCR, and validation
  stages complete.
- Completed screenshots are retained for seven days. Unfinished resumable runs
  are not removed.
- ERP screenshots, customer/order data, databases, Excel files, backups,
  deployment packages, runtime binaries, and local state are excluded from Git.

## Local setup

This repository contains source code only. Production deployment additionally
needs the portable runtime, Tesseract OCR language data, the configured ERP
executable, and the workstation's existing `data/orders.db`.

1. Read [`V6_DB_WEB/README.md`](V6_DB_WEB/README.md).
2. Use `V6_DB_WEB/config.example.json` and the example BHQ watcher settings as
   templates; do not commit workstation-specific configuration.
3. Run the offline tests before building a deployment package.
4. Validate and back up the target database before any formal update.

See `V6_DB_WEB/docs/` for database, backup, ERP, web, and BHQ operating guides.

