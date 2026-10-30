# V6 第四階段正式資料庫操作說明

## 資料庫模式

- 測試資料庫：`data/orders_test.db`
- 正式資料庫：`data/orders.db`
- 模式設定：`formal_database_modes.json`

一般測試命令會拒絕寫入任何名為 `orders.db` 的檔案。正式命令只允許專案內固定的 `data/orders.db`，且資料庫必須包含 `database_mode=formal` 安全標記。

## 第一次初始化

雙擊 `INITIALIZE_FORMAL_DATABASE.bat`，依提示輸入 `建立正式資料庫`。

初始化會使用 `migrations` 建立全新資料庫，不會複製、改名或修改 `orders_test.db`。如果 `orders.db` 已存在，初始化會直接停止，不會覆蓋。

## 匯入 V5.10 六個月基準

雙擊 `IMPORT_V5_BASELINE_TO_FORMAL_DATABASE.bat`。

每次匯入前會先在 `backups/formal` 建立帶日期時間的備份並驗證。整批匯入使用 SQLite transaction；任一訂單無法解析時整批 rollback。

相同 V5.10 資料可以安全重跑：訂單、版本、明細與 `review_queue` 不會重複。每次匯入仍會保留獨立的 `scan_jobs` 與待確認 `scan_items`，作為本次操作稽核紀錄。

## 驗證

雙擊 `VALIDATE_FORMAL_DATABASE.bat`。

驗證報告會寫入 `reports`，內容包含基準筆數、重複檢查、`store_id`、SQLite `quick_check`、外鍵及 V5.10 欄位/明細逐筆比對。

## 備份

雙擊 `BACKUP_FORMAL_DATABASE.bat`。

備份位置：`backups/formal/orders_YYYYMMDD_HHMMSS_ffffff.db`

備份完成後會驗證 SHA-256、`quick_check`、外鍵及正式模式標記，並寫入 `backup_records`。

## 還原

1. 雙擊 `RESTORE_FORMAL_DATABASE.bat`。
2. 從畫面列出的備份中選擇一個，輸入完整路徑。
3. 依提示輸入 `還原正式資料庫`。

還原前會先備份目前 `orders.db`。指定備份驗證成功後才會複製至暫存檔並原子替換正式庫；失敗時保留原正式庫。

## 圖片目錄

```text
data/images/
  official/YYYY/MM/
  pending_review/YYYY/MM/
  archive/
```

SQLite 僅保存相對路徑、SHA-256、版本關聯、圖片角色、狀態及待確認保護標記，不保存 BLOB。第四階段沒有複製 V5.10 舊路徑圖片，也沒有捏造缺失圖片紀錄。

## 本階段界線

第四階段尚未連接網站，也未啟動最近兩年 ERP 歷史掃描。V5.10、R4/R4.1、`orders_test.db` 及既有 `runs` 均為唯讀保留基準。
