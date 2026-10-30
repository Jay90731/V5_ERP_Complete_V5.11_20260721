# 婚約訂單管理系統 V6_DB_WEB

目前版本已完成 SQLite 唯一正式資料庫、ERP 每日 7 天／最近 6 個月雙模式、歷史資料回補、OCR 待確認與人工寫回、婚約訂單網站，以及 BHQ Excel 匯入與資料夾監看工具。ERP 與 BHQ 資料會寫入同一個 `data/orders.db`；正式資料庫、截圖、備份與封裝檔不納入 Git。

本 Git 版本保存可重建與維護系統所需的程式碼、migration、測試、操作文件及範例設定。正式訂單資料與含個資的 ERP 截圖只保留在部署電腦及受控備份中。

## 專案界線

- `V5_ERP_Complete_V5.10_20260721` 是唯讀來源，不會被本專案修改。
- 開發資料庫是 `data/orders_test.db`。
- 正式 ERP 電腦未來另外建立 `orders.db`，不連線到開發電腦。
- 程式位於 `src`、`migrations` 與根目錄啟動檔。
- 資料位於 `data`、`storage/screenshots` 與 `backups`。
- 更新程式時不可覆蓋上述資料目錄。
- SQLite 只保存圖片相對路徑與 SHA-256，不保存圖片 BLOB。
- Excel 不是主要儲存方式，後續只提供選擇性匯出。

## 環境

- Windows
- Python 3.8 以上
- 不使用 Docker
- SQLite 核心只使用 Python 標準函式庫
- ERP OCR runtime 需要 Pillow 與 Tesseract；可攜測試包已包含

## 建立資料庫

```powershell
cd C:\Users\USER\OneDrive\文件\婚約\V6_DB_WEB
python v6.py migrate --db data\orders_test.db
```

migration 可重複執行。已套用的 migration 會以版本與 SHA-256 記錄在 `database_migrations`。

## 匯入 V5.10

一鍵指令：

```powershell
.\import_v510.ps1
```

完整指令：

```powershell
python v6.py import-v5 `
  --db data\orders_test.db `
  --source ..\V5_ERP_Complete_V5.10_20260721 `
  --report reports\v510_import.json
```

匯入器讀取：

- `order_incremental_engine/state/order_history.json`
- `order_incremental_engine/state/current_orders.json`

`order_history.json` 的已確認訂單會寫入 `orders`、`order_versions` 與 `order_items`。`current_orders.json` 的待確認資料只寫入 `review_queue`，不會加入正式訂單統計。

## 驗證

```powershell
python v6.py validate-v5 `
  --db data\orders_test.db `
  --source ..\V5_ERP_Complete_V5.10_20260721 `
  --report reports\v510_validation.json
```

驗證內容包含訂單數、多品項、所有明細數字、部門代號、SR 門市規則、重複訂單、重複版本、外鍵及 SQLite quick check。

## 自動化測試

```powershell
python -m unittest discover -s tests -v
```

測試包含：

- schema 與 migration 重跑
- 首次匯入
- 相同資料重跑不重複
- 訂單內容改變建立新版本
- 多品項全部保存
- 待確認跨次保存
- 匯入錯誤整批 rollback
- 正式訂單全部指向唯一的 SR 門市
- 非目標部門、作廢及排除資料不套用 SR
- `department_code` 保留 ERP 原始值
- 資料庫關閉重開

## 正式 ERP 電腦規劃

正式安裝時建立獨立的 `config.json`，將資料庫設為 ERP 電腦本機路徑，例如：

```json
{
  "environment": "production",
  "database_path": "D:\\WeddingOrdersV6\\data\\orders.db",
  "screenshots_path": "D:\\WeddingOrdersV6\\storage\\screenshots",
  "backups_path": "D:\\WeddingOrdersV6\\backups"
}
```

正式資料庫、圖片與備份不可放進程式更新包，也不可指向開發者電腦。

正式資料庫需要套用新 migration 時，執行 `APPLY_FORMAL_MIGRATIONS.bat`。工具會先建立並驗證備份，再套用 migration、寫入 `audit_logs`，最後核對訂單、版本、明細、SR 規則、外鍵及 SQLite 完整性。

## 每日 7 天 OCR 重播

使用既有 OCR JSON 執行：

```powershell
.\replay_daily_7_days.ps1 `
  -OcrJson "C:\path\to\ocr\data\orders.json" `
  -DateFrom "2026-07-15" `
  -DateTo "2026-07-21"
```

或直接使用共用指令：

```powershell
python v6.py replay-ocr `
  --db data\orders_test.db `
  --input "C:\path\to\ocr\data\orders.json" `
  --date-from 2026-07-15 `
  --date-to 2026-07-21 `
  --job-type daily_7_days `
  --report reports\daily_7_days_replay.json
```

每次執行會建立一筆 `scan_jobs`，每張圖片建立一筆 `scan_items`。單筆訂單使用獨立 transaction，單筆錯誤會留下原因並繼續下一筆，不會留下半筆訂單。

結果分類：

- `new`：新訂單及第一個版本
- `unchanged`：內容雜湊相同，不增加版本
- `changed`：完整內容不同，建立新版本及完整明細
- `pending`：寫入 `review_queue`，不納入正式統計
- `non_target_department`：保留掃描紀錄，不納入正式統計
- `voided`：保存作廢狀態及版本，不納入正式統計
- `failed`：保存單筆失敗原因

正式統計可讀取 `official_orders` 與 `official_order_items` view；兩者只包含部門 4001、非作廢且有完整明細的訂單。

正式統計訂單的 `store_id` 固定指向唯一的 SR 門市。這項補充不屬於 ERP 訂單內容，因此不會建立新的 `order_versions`。專案不再串接其他門市系統，也不進行訂單編號門市配對。

## 第二階段驗證

驗證兩次相同資料重播：

```powershell
python v6.py validate-phase2 `
  --db data\orders_test.db `
  --first-job 2 `
  --second-job 3 `
  --report reports\PHASE2_DATABASE_VALIDATION_20260731.json
```

驗證會檢查批次計數、假新增／假異動、待確認保存、訂單與版本重複、圖片 SHA-256、`store_id`、外鍵及 `PRAGMA quick_check`。

## 第三階段 ERP 測試

ERP 測試包：

`dist\V6_PHASE3_ERP_TEST_20260731`

在 ERP 電腦依序執行：

1. `CHECK_V6_PHASE3_ENVIRONMENT.bat`
2. `START_V6_PHASE3_TEST.bat`
3. 點「開始每日7天流程」
4. 人工登入 ERP
5. 點「登入完成，繼續」

流程階段：

- ERP準備
- 查詢訂單
- 批次截圖
- OCR
- SQLite驗證及寫入
- 完成

執行中會顯示目前訂單、完成筆數、待確認數與錯誤原因。停止後可點「繼續未完成流程」，截圖器會依訂單編號跳過已完成資料。

詳細說明：

`docs\PHASE3_ERP_TEST_GUIDE.md`

安全限制：

- `phase3_config.json` 只允許 `data\orders_test.db`。
- 設定成 `orders.db` 會立即停止。
- 不更新 V5.10、正式歷史或 Excel。
- 不保存 ERP 帳號密碼。
- 不操作 ERP 新增、修改、刪除、儲存或列印。

第三階段離線整合已使用真實 118 筆 OCR 連跑兩次，兩次皆為未變更 29、待確認 2、排除 87、新增 0、異動 0、失敗 0。

## 雙辨識模式

啟動 `phase3_launcher.ps1` 後可選擇：

- `開始每日 7 天流程`：查詢當日往前 6 天至當日，使用 `daily_7_days`。
- `開始最近 6 個月流程`：查詢當日往前 6 個月至當日，使用 `monthly_6_months`。

兩個按鈕共用同一套 ERP 查詢、子視窗放大、逐筆截圖、OCR、待確認與 SQLite 寫入服務，正式執行共用唯一的 `data/orders.db`。兩種模式都從本次查詢結果的實際第一筆開始，不限制訂單尾碼，並安全支援 0 筆結果。左側清單 OCR 僅作輔助；第一列點擊與焦點成功後會改讀上方訂單編號，仍讀不到時先存成 `seq_0001.png`。正常 OCR 成功後改用訂單編號命名，辨識失敗則保留原圖並進入待確認。

中途停止後，`繼續未完成流程` 會依最近一次執行的模式續跑，不會把 6 個月批次誤接成 7 天批次。每次結果位於 `runs/YYYY-MM-DD/run_*/`，其中 `run_state.json` 與 `run_summary.json` 會保存模式、工作類型及實際日期範圍。

## 歷史資料狀態

2022 年起的 56 個月份已掃描完成。歷史逐月執行功能已從日常交付包移除，SQLite 內的訂單、版本、明細、待確認、稽核記錄與 56 筆完成月份 `scan_jobs` 均保留，僅供查詢與稽核，不再提供重新執行入口。R6 不可變原始回傳包繼續保留於開發電腦，不納入日常執行流程。
