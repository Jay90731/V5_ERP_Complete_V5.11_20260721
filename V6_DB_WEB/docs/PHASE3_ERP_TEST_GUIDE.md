# V6 第三階段 ERP 每日 7 天測試說明

## 測試範圍

這是第三階段測試版，只會使用 `data/orders_test.db`。不會建立正式 `orders.db`，不會更新 V5.10，也不會產生正式 Excel。

工具只會：

1. 啟動或尋找 ERP。
2. 等待使用者人工登入。
3. 進入外商禮盒訂單登錄。
4. 查詢最近 7 個日曆日（包含執行當日）的 GOU 訂單。
5. 逐筆切換及截圖。
6. 執行 OCR。
7. 透過 V6 共用服務寫入測試 SQLite。

工具不會操作新增、修改、刪除、儲存或列印。

## 第一次執行

1. 將完整測試包放在 ERP 電腦本機，例如桌面。
2. 雙擊 `CHECK_V6_PHASE3_ENVIRONMENT.bat`。
3. 確認畫面顯示環境檢查通過。
4. 雙擊 `START_V6_PHASE3_TEST.bat`。
5. 點「開始每日7天流程」。
6. 在 ERP 人工輸入帳號及密碼。
7. ERP 登入完成後，回到工具點「登入完成，繼續」。
8. 工具會在查詢完成後自動最大化訂單子視窗，並確認至少為 `1700×800`。
9. 如果自動最大化都失敗，工具會顯示「請手動最大化ERP訂單子視窗」：
   - 切換到 ERP，手動按訂單子視窗右上角的最大化按鈕。
   - 回到工具，點「已手動最大化，繼續」。
   - 工具重新驗證尺寸；未達 `1700×800` 不會截圖。
10. 等待「完成」。
11. 點「查看本次紀錄」，確認有：
   - `run_state.json`
   - `daily_summary.json`
   - `daily_summary.txt`
   - `phase3_run.log`
   - `ocr/data/orders.json`

## 停止及續跑

- 執行中點「停止」，工具會在安全位置停止。
- 截圖階段會完成目前這一筆後停止。
- 再開啟工具，點「繼續未完成流程」。
- 已完成的截圖會依訂單編號跳過。
- SQLite 寫入階段很短，開始寫入後停止按鈕會停用，避免半批狀態。

## 同一天第二次測試

第一次完成後，再執行相同流程一次。預期：

- 新增為 0。
- 已變更為 0。
- 相同訂單全部為未變更。
- 不增加重複訂單或重複版本。
- 待確認不重複。

## R3 視窗診斷

每次最大化前，執行紀錄會保存訂單子視窗完整 parent hierarchy，包括：

- handle 及 parent handle
- process id
- window class
- window title
- visible 及 zoomed 狀態
- window rect

R3 會依序嘗試 `ShowWindow`、真正 `MDIClient` 的 `WM_MDIMAXIMIZE`、`WM_SYSCOMMAND + SC_MAXIMIZE`、雙擊標題列中央及依系統按鈕尺寸計算的最大化按鈕。

ERP 電腦必須先執行 `CHECK_V6_PHASE3_ENVIRONMENT.bat`。每次執行都會在 `environment_reports` 建立新的 `phase3_environment_YYYYMMDD_HHMMSS.json` 與 `.txt`，不可使用打包電腦產生的舊報告代替。

## R4 訂單清單導航

R4 保留 R3 的子視窗最大化及 `1700×800` 驗證，訂單切換改為：

1. 以左側列選取箭頭區定位目前列。
2. 使用 `WindowFromPoint` 取得實際訂單清單 control handle。
3. 記錄 ERP PID、程序名稱、視窗標題、handle、control class 及 thread id。
4. 必要時使用 `AttachThreadInput + SetFocus` 將焦點設定到清單 control。
5. 將 `WM_KEYDOWN/WM_KEYUP` 的 `VK_DOWN` 直接 `PostMessage` 到 control，不使用 foreground `SendKeys`。
6. 讀取畫面訂單編號，確認已由上一筆切換到下一筆。
7. 若沒有改變，直接點擊下一列的箭頭區，再次驗證訂單編號。

藍色反白只用來找出目前列的位置，不代表已取得鍵盤焦點。

## R4.1 進度檔與續跑修正

R4.1 不變更 R3/R4 已通過的視窗放大、清單定位、逐筆截圖、OCR 或 SQLite 寫入流程。

- `run_state.json` 與批次狀態改用單一寫入鎖、唯一暫存檔、Windows 原子覆蓋及最多 12 次重試。
- 點「繼續未完成流程」時，啟動器、工作程序、Python 管線及截圖程序會依序清除並驗證舊 STOP 旗標。
- 每次開始或續跑都會自動執行環境檢查，並在 `environment_reports` 產生帶時間戳的 `.json` 與 `.txt` 報告。
- 若自動環境檢查未通過，流程會在操作 ERP 前停止，並保留報告供檢查。

## 測試完成後帶回

請將以下內容帶回開發電腦驗證：

- `runs` 整個資料夾
- `environment_reports` 整個資料夾
- `data/orders_test.db`

不要只帶截圖；批次狀態、摘要與資料庫需要一起檢查。

## 判定通過

第三階段只有在 ERP 電腦實跑兩次後，以下項目全部成立才算正式通過：

- 截圖數等於 OCR 數及 `scan_items` 數。
- 第二次沒有假新增及假異動。
- SQLite `quick_check=ok`。
- 外鍵錯誤為 0。
- 沒有建立 `orders.db`。
- ERP 沒有被新增、修改、刪除、儲存或列印。
