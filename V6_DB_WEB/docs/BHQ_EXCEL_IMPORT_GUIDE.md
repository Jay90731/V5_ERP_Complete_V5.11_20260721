# BHQ Excel 訂單匯入操作說明

## 安全界線

目前入口只允許寫入專案內的 `data/orders_bhq_test.db`。程式會解析完整絕對路徑；
若傳入正式 `data/orders.db` 或其他資料庫，會直接拒絕。

BHQ 流程直接讀取 Excel，不呼叫 ERP 擷取、歷史掃描或 OCR。

## Dry-run

```powershell
& '.\erp_returns\20260807_six_month_sr\V6_DUAL_MODE_SR_ERP_TEST_20260806\runtime\python\python.exe' `
  -X utf8 '.\bhq_import.py' `
  --source 'C:\Users\USER\Downloads\202607ALL 訂單明細表.xls' `
  --dry-run
```

Dry-run 不開啟資料庫。

## 套用測試 migration

```powershell
& '.\erp_returns\20260807_six_month_sr\V6_DUAL_MODE_SR_ERP_TEST_20260806\runtime\python\python.exe' `
  -X utf8 '.\bhq_import.py' `
  --source 'C:\Users\USER\Downloads\202607ALL 訂單明細表.xls' `
  --migrate-test `
  --db '.\data\orders_bhq_test.db'
```

## 一鍵匯入測試庫

雙擊 `IMPORT_BHQ_TEST.bat`，或執行：

```powershell
.\IMPORT_BHQ_TEST.bat
```

每個檔案使用單一 SQLite transaction。嚴重驗證錯誤、未知門市、跨系統撞號或
執行例外都會回復該批次的訂單、版本、明細、門市與產品變更。

## 驗證

```powershell
& '.\erp_returns\20260807_six_month_sr\V6_DUAL_MODE_SR_ERP_TEST_20260806\runtime\python\python.exe' `
  -X utf8 -m unittest `
  tests.test_migrations tests.test_formal_database tests.test_bhq_import -v
```

## 回復測試庫

1. 關閉使用 `orders_bhq_test.db` 的程式。
2. 保留現況測試庫供問題追查。
3. 將 `backups/formal/orders_20260825_161236_465268.db` 複製為
   `data/orders_bhq_test.db`。
4. 執行 `PRAGMA integrity_check` 與 `PRAGMA foreign_key_check`。

此回復程序只適用測試庫，不得覆蓋正式 `orders.db`。

## 正式模式

正式模式使用獨立參數 `--migrate-formal` 與 `--import-formal`，只接受專案內
`data/orders.db`，並要求 `--approved-sha256`。測試模式的固定路徑限制仍保留。

正式 migration／匯入前應先確認沒有其他 writer，並使用現有正式備份、鎖定、
migration、稽核及驗證機制。正式回復必須使用 `restore-formal`；本次 2026-07
BHQ 匯入的回復點及完整步驟記錄於
`reports/BHQ_FORMAL_DEPLOYMENT_202607ALL.md`。
