# BHQ 本機 Excel 監看器：固定路徑離線版

## 目前停止點

目前只完成離線功能與測試，不得在 `auntstella` 電腦安裝、啟用排程或匯入 BHQ。

目標機舊R3資料庫：

`C:\Users\auntstella\Desktop\V6_DB_WEB_DUAL_R3_20260825_165102\data\orders.db`

是明確禁止路徑，監看器、部署器及正式啟用工具都不得連線或修改。

未來唯一允許的正式專案與資料庫：

```text
C:\Users\auntstella\Desktop\V6_DB_WEB
C:\Users\auntstella\Desktop\V6_DB_WEB\data\orders.db
```

正式監看來源仍固定為：

`C:\Users\auntstella\BHQ\_Export\INBOX`

## 不建立資料庫的保護

- 正式模式先比對固定專案根目錄及固定資料庫完整路徑。
- 拒絕R3、其他 `V6_DB_WEB_DUAL_R*`、測試資料庫及其他位置的同名 `orders.db`。
- `data` 不存在、`orders.db` 不存在或檔案為0位元組時立即停止。
- 正式連線只使用既有檔案URI `mode=rw`；驗證連線另設 `PRAGMA query_only=ON`。
- 正式監看匯入不使用會執行 `mkdir` 或建立空白SQLite的一般連線函式。
- 啟動前檢查正式識別標記、migration至少v7、BHQ資料表／欄位／索引、`integrity_check`及外鍵。
- 隨包正式設定維持 `formal_enabled=false` 且確認字串空白；未來仍需另一次明確核准才可雙重啟用。
- 檢查失敗時不建立排程、不移動INBOX、不套migration、不修改SQLite。
- 正式 watcher 類別本身不建立正式監看資料夾；資料夾必須在合法資料庫通過驗證後，由未來的明確啟用程序準備。

## 離線部署包

新版離線部署包不包含：

- `orders.db`
- `data` 資料夾
- migration
- XLS／XLSX、七月來源檔或其他正式資料

部署包中的 `Deploy-BHQ-Watcher-Files-Only.ps1` 即使未來被執行，也只接受固定專案。它會先使用包內保護程式，對既有固定資料庫進行無寫入驗證；全部通過才複製監看器程式檔。它不建立排程、不啟動服務、不匯入BHQ、不移動INBOX。

目前不要在目標機執行部署包。未來順序必須是：

1. 先另外部署最新R4＋BHQ統一專案及已核准的正式資料庫到固定路徑。
2. 驗證固定 `V6_DB_WEB` 與既有 `orders.db`。
3. 經另一次明確核准後，才離線部署監看器程式。
4. 最後另行提供核准來源檔，明確執行正式驗收與排程安裝。

## 共用鎖

ERP與BHQ仍共同呼叫 `database_write_lock(database)`。固定正式資料庫的鎖檔解析為：

`C:\Users\auntstella\Desktop\V6_DB_WEB\data\orders.db.write.lock`

ERP持鎖時BHQ只會等待／deferred，不搶寫。停止BHQ時只使用優雅停止旗標，不終止ERP、網站或其他Python程序。

## 未來日常工具

下列工具已包含，但目前不得在目標機使用：

- `啟動BHQ自動匯入.bat`
- `停止BHQ自動匯入.bat`
- `查看BHQ匯入狀態.bat`
- `開啟BHQ匯入資料夾.bat`
- `重試BHQ失敗檔案.bat`

只有未來固定R4正式資料庫通過全部驗收、且排程另經核准建立後，這些工具才生效。
