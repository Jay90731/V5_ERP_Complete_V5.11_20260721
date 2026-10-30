# 統一正式資料庫備份與還原

## 自動備份

- ERP 每日 7 天與最近 6 個月流程在 SQLite 寫入前建立正式備份。
- 網站每次人工確認、排除或暫存前建立人工處理備份。
- 備份會執行 SQLite `quick_check` 與外鍵檢查，通過後才進行寫入。

備份位置：

- 掃描與手動正式備份：`backups/formal/`
- 人工處理前備份：`backups/manual_review/`
- 整併前兩份資料庫：`backups/database_consolidation/20260825_110228/`
- R6 原始不可變備份：`backups/R6_RETURN_20260824_IMMUTABLE/`

## 手動備份

雙擊 `BACKUP_FORMAL_DATABASE.bat`。產生的檔名包含日期時間、SHA-256 與驗證紀錄。

## 還原

1. 關閉網站與 ERP 掃描工具。
2. 雙擊 `RESTORE_FORMAL_DATABASE.bat`。
3. 輸入欲還原的 `.db` 完整路徑。
4. 依畫面輸入確認文字。
5. 程式會先備份目前資料庫，再驗證來源，最後原子替換 `data/orders.db`。

2026-08-25 已將正式備份還原到隔離測試資料夾並重新驗證：10,498 張訂單、10,574 個版本、11,339 筆明細、`quick_check=ok`、外鍵錯誤 0。測試沒有覆蓋正式資料庫。
