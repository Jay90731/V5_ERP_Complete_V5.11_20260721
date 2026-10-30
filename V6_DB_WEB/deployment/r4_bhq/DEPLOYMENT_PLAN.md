# R4＋BHQ統一專案部署方案（待確認，禁止執行部署）

## 本次交付與停止點

只準備程式、內建runtime、唯讀盤點／快照／比對工具及部署方案。沒有操作auntstella電腦，沒有取得其最新資料，沒有建立正式資料庫、部署R4、套migration、匯入BHQ、安裝排程或啟動服務。

本包不是可直接覆蓋的資料包；不含開發機orders.db、data資料夾、Excel、截圖或正式資料。BHQ設定保持formal_enabled=false、確認字串空白。套件內沒有資料庫升級、合併或正式替換的一鍵入口。啟動入口須等待日後核准驗收憑證，預設拒絕啟動。

唯一未來正式位置：`C:\Users\auntstella\Desktop\V6_DB_WEB\data\orders.db`。

目前受保護來源：`C:\Users\auntstella\Desktop\V6_DB_WEB_DUAL_R3_20260825_165102\data\orders.db`。R3是資料來源與回復保留本，不是BHQ監看目標；絕不把開發機統一庫覆蓋過去。

## 1. 如何取得並核對目標最新R3

方案核准後，在目標機安排維護時段。請使用者安全結束ERP掃描、網站人工確認、圖片清理及其他SQLite寫入者；不得kill正在執行的transaction。記錄既有排程／捷徑指向，暫停會再啟動R3的觸發來源。資料蒐集工具本身不停止程序、不修改排程。

再執行套件頂層的CAPTURE_LATEST_R3.ps1，必須明確傳入-MaintenanceConfirmed與新的輸出資料夾。此工具只接受上述R3來源：

- 檢查既有非空orders.db、正式marker、完整性／外鍵、無running scan_job，及可見寫入程序均已退出。
- 使用ERP相同的OS寫入鎖，來源連線使用URI mode=ro＋query_only。以SQLite backup API擷取一致快照，包含已提交WAL內容；不直接Copy-Item正在使用的orders.db、不checkpoint來源。
- 產生latest_r3.sqlite、r3_inventory.json、CAPTURE_REPORT.json及data/storage/runs/control/backups的檔案副本與SHA-256清單，另保存phase3_config等設定與latest_run_path.txt供核對，不直接拿舊resume指標啟動新版。保留圖片、待確認附件、備份與執行證據，不改動來源SQLite；共用鎖可能建立orders.db.write.lock操作檔。
- 來源前後全資料表邏輯雜湊及實體DB雜湊必須相同，副本integrity_check=ok、外鍵0，附件副本雜湊一致；不符就標示失敗、不得作部署來源。
- 初次蒐集不在受保護R3的backup_records插入資料；外部CAPTURE_REPORT記錄這次只讀快照。既有正式備份／backup_records機制待核准的升級副本階段沿用。
- 建議另外完整封存舊R3專案、設定、runtime、所有圖片與排程匯出；原R3目錄保持不動。即使只回傳盤點JSON，也只能比較，完整保留／升級仍需要快照與附件。

將快照與附件透過核准的本機媒介／USB等回傳；它包含業務資料，不應公開分享。這不是BHQ的OneDrive中繼匯入流程。

比對不能只看檔案日期、MAX(id)或總筆數。使用r4_deployment_preflight.py：

1. 對最新R3、先前R3回傳本及開發機參考庫分別產生inventory（全部唯讀）。
2. compare列出target_only、reference_only與衝突：order_no、每張單version_no/content_hash/current version，以及所有明細、review_queue、web_manual_review_events、audit_logs、產品、門市、截圖等逐表逐列雜湊。
3. 時間戳只用作線索，不據此自動覆蓋；人工確認／排除與同單號版本差異須單獨審核。ID可能在兩邊各自增加，不能以ID相同代表同筆內容。
4. 沒有最新R3回傳前，無法聲稱目標機沒有新增資料；本次也不做此假設。

## 2. 保留全部資料並升級

核准最新快照SHA及差異清單後，永遠以最新目標R3一致副本建立隔離candidate；不是從開發機庫起步。保留全部原ID、版本、明細、人工確認、排除、待確認、稽核、產品、門市、圖片路徑與附件。若欄位內保存R3絕對路徑，先列清單；不改寫歷史稽核或resume舊執行工作。程式設定的工作目錄另改為固定路徑。

分開處理「結構升級」與「資料內容修正」：

- 已是v7：驗證migration checksum與BHQ欄位／索引，跳過007，不重跑。
- 已是v6且001～006 checksum吻合：只在candidate備份後使用既有apply_migrations()，僅執行尚未套用的007，SQL在transaction內；001～006只核對紀錄，不再執行SQL。
- 低於v6、未知／超前migration、checksum不同、marker異常或結構與版本矛盾：停止，先提出專屬升級方案，不自動重建／重置資料庫。
- 在candidate使用既有正式備份API與backup_records登錄；每個階段備份皆有完整路徑、SHA-256、完整性與外鍵結果。
- 純結構升級後用原R3欄位投影產生candidate inventory，再執行retention：所有原資料表、原列、原欄位必須逐筆保留，新增欄位／表允許，但原資料遺失或改值即失敗。備份登錄只能是新增資料。

R4開發機曾做日期修正與待確認整理，並不是目標最新資料的完整替代品。不得直接重播舊修復資料或執行promote_r4_repair.py覆蓋candidate。開發機獨有訂單、已核准日期修正與目標人工確認的衝突，逐筆列成差異；有必要且另經核准時才使用既有版本／稽核流程套用，保留原歷史。不能默默以「日期較新」取勝。

BHQ分支也先核對：

- 最新R3已有BHQ：保留原匯入紀錄與版本，只比對來源／內容，不重新匯入。
- 最新R3沒有BHQ：先提出需補入的257／551與核准XLS SHA清單；另經核准後才在candidate使用共用匯入服務、單檔transaction、撞號保護、產品名稱保護與冪等驗證。不得搬開發機SQLite整庫。本次不包含XLS、不執行這一步。
- 不管有無BHQ資料，監看器與排程保持關閉；手動核准的candidate資料整合不等於核准自動監看。

任何核准內容修正會打破原列完全相同的檢查；必須用明確變更清單、原始快照、版本與稽核解釋每一差異，不能只降低驗收標準。

## 3. 備份、部署、驗證與回復

### 切換前

1. 核准最新R3快照及比對報告後才準備candidate。若驗證期間目標R3繼續寫入，切換日必須重新維護停機、重新擷取／比對，從最新副本重新演練；不得套用過期candidate。
2. 建立不可變R3完整備份、candidate migration前／後備份、核准內容整合前／後備份；驗證檔案可讀、SHA、完整性、外鍵、附件。
3. 核對固定V6_DB_WEB是否已存在。不存在才規劃新目錄；已存在則先完整備份並比對，絕不直接覆寫。
4. 程式在獨立staging備妥：本包純程式＋runtime、目標原本設定（ERP執行檔位置須核對）、candidate資料及圖片。此staging不是第二個正式寫入庫，不能啟動正式服務。

### 切換與驗證

5. 所有舊／新程序退出並封鎖舊排程、捷徑再次寫R3，確認最後來源logical hash與已核准candidate基底相同。OS鎖是按資料庫完整路徑產生；R3與固定新路徑的鎖不同，所以不能同時開舊版與新版寫入。
6. 另經核准後才把已驗證staging交付到固定V6_DB_WEB。不修改舊R3，保留原目錄作回復；不得直接套用開發機DB。
7. 先只讀驗證：資料庫v7/checksum、marker、integrity=ok、外鍵0、所有原列／核准差異、原人工確認與排除、圖片可讀、訂單與版本無重複。統計用目標當下基準，不硬套R4舊報告總筆數。
8. ERP篩選前後一致，BHQ若核准整合則257／551／8,742,030，總額按訂單一次；網站統一查詢與人工確認回歸先在candidate副本測試。固定路徑網站先唯讀健康檢查；ERP只核對設定及共用鎖，不自行執行掃描。
9. 驗收通過才簽發deployment_acceptance.json並切換捷徑／日常操作。未簽發前本包的正式啟動入口拒絕執行。BHQ formal_enabled依然false、不安裝排程、不啟動監看。

### 失敗回復

- 切換前失敗：只保留candidate／報告作調查，舊R3未改；不發布、不刪除舊資料。
- 切換後尚未新增寫入：安全停止新程序、隔離新目錄及記錄，核對最後R3備份後，另經核准恢復原R3執行路徑與捷徑；不得兩套同時寫。
- 切換後已新增ERP／人工確認資料：先備份新固定庫與附件，列出切換後增量，審核回移／修復；不能直接退回舊R3而丟失新增資料。
- 若需還原固定庫，依既有正式restore機制，在所有程序退出後執行；先備份當前庫，檢查還原備份位於核准backups/formal、SHA與完整性，保留資料庫及附件一致版本。程式版、設定與資料庫版須成對，不用R3程式盲開新結構。

## 4. 統一指向方式

| 元件 | 固定設定 | 連線／寫入規則 |
|---|---|---|
| ERP | phase3_config.json的database_path為固定絕對orders.db；mode=formal | 既有共用database_write_lock；日常7日／6月流程，不啟動歷史掃描 |
| 網站 | 固定入口明確傳--database同一orders.db | Repository維持mode=ro＋query_only；人工確認使用同一傳入路徑及共用OS鎖 |
| BHQ | bhq_watcher_formal.json固定相同orders.db | existing mode=rw＋啟動健康檢查；formal_enabled=false；不安裝排程 |
| OS鎖 | 固定data\orders.db.write.lock | 所有寫入者同一完整路徑；無第二個orders_web.db |

固定入口先核對專案根目錄、DB已存在非空、marker／migration／結構、ERP/BHQ設定一致。缺檔不建立DB或data。不依賴系統Python，不把測試／R3路徑轉成正式目標。套件只將設定模板改為固定路徑，沒有變更ERP擷取／OCR或網站查詢算法。

## 核准點

目前等待部署方案確認；即使確認方案，也要先取得最新R3快照與比對結果，再核准candidate內容及切換。順序不可跳過：最新資料核對 → 統一專案與資料庫部署 → 驗證 → 最後另行核准BHQ監看器啟用。
