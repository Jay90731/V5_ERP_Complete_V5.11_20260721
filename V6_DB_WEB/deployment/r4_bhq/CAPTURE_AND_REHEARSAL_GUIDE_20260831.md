# 最新R3快照取得＋離線升級演練：操作交接

本階段已核准：取得最新R3一致快照，回傳後只在副本升級及驗證BHQ。尚未核准：正式切換、停用舊R3入口、安裝排程、啟動監看。BHQ必須保持formal_enabled=false。

目前本機只找到2026-08-26回傳的舊R3，沒有本次latest_r3.sqlite／CAPTURE_REPORT.json。不能以這份舊回傳本或開發機orders.db代替最新目標資料。

## 一、auntstella電腦需要做的事

### 1. 下載及準備工具（這時不必停止R3）

將已交付的`R4_BHQ_DEPLOYMENT_PREPARATION_20260831_140656.zip`放到：

`C:\Users\auntstella\Downloads\R4_BHQ_DEPLOYMENT_PREPARATION_20260831_140656.zip`

只使用這個140656套件；不要使用140238草稿，也不要使用含開發機資料庫的舊R4整包。

在auntstella開啟Windows PowerShell，貼上以下區塊。它只驗證工具包、解壓到新工具資料夾，不部署到正式V6_DB_WEB，不連線R3。

```powershell
$ErrorActionPreference = 'Stop'
$kitZip = 'C:\Users\auntstella\Downloads\R4_BHQ_DEPLOYMENT_PREPARATION_20260831_140656.zip'
$expectedZipHash = '8ADDA045051FC9E9D15B3BD063768AF7210AD0C82085A75FCB8B3B971A76382E'
if (-not (Test-Path -LiteralPath $kitZip -PathType Leaf)) { throw '找不到指定ZIP，請先放入Downloads。' }
if ((Get-FileHash -LiteralPath $kitZip -Algorithm SHA256).Hash -ne $expectedZipHash) { throw 'ZIP雜湊不符，停止。' }
$toolsStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$toolsDirectory = 'C:\Users\auntstella\Desktop\R4_BHQ_CAPTURE_TOOLS_' + $toolsStamp
if (Test-Path -LiteralPath $toolsDirectory) { throw '工具目錄已存在，請重新執行產生新時間戳。' }
Expand-Archive -LiteralPath $kitZip -DestinationPath $toolsDirectory
$kitRoot = Join-Path $toolsDirectory 'R4_BHQ_DEPLOYMENT_PREPARATION_20260831_140656'
$bundledPython = Join-Path $kitRoot 'V6_DB_WEB\runtime\python\python.exe'
& $bundledPython -X utf8 -B (Join-Path $kitRoot 'V6_DB_WEB\tools\verify_r4_preparation_package.py') $kitZip
if ($LASTEXITCODE -ne 0) { throw '工具包驗證失敗，不可取得快照。' }
Write-Host ('工具已準備：' + $kitRoot)
```

保持同一個PowerShell視窗開啟，下一步會使用剛才的變數。

### 2. 暫停寫入，只做短暫維護

1. 通知其他使用者暫停人工確認、排除與修改。
2. 讓正在執行的ERP掃描完成，或使用R3既有的安全停止功能，等目前SQLite transaction結束、工作顯示已停止後，再關閉ERP掃描控制視窗。
3. 等網站人工提交完成，再安全關閉網站伺服器視窗；只關瀏覽器不代表網站寫入程序已停止。若不確定伺服器如何停止，先停在這裡回報，不要強制終止Python。
4. 圖片清理、其他匯入／SQLite工具也不能同時寫入。避開自動作業會啟動的時段；不能確保沒有寫入者時，不要執行快照。
5. **不刪除或停用舊R3捷徑，不修改排程，不卸載舊版，不執行taskkill /F，不結束所有Python程序。** 這次只是短暫暫停操作，完成快照就可恢復舊R3使用。

來源固定：`C:\Users\auntstella\Desktop\V6_DB_WEB_DUAL_R3_20260825_165102\data\orders.db`。

請先確認磁碟有足夠容量容納一份R3的data/storage/runs/control/backups與設定副本；若要打包回傳，還要預留壓縮檔空間。原檔不會被刪除。

### 3. 取得一致快照

確認上一節完成後，在同一個PowerShell視窗貼上：

```powershell
if ((Read-Host '確認ERP、網站人工修改及其他寫入已安全暫停，請輸入 CAPTURE') -cne 'CAPTURE') { throw '未確認維護，停止。' }
$captureStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$snapshotDirectory = 'C:\Users\auntstella\Desktop\R3_CAPTURE_' + $captureStamp
if (Test-Path -LiteralPath $snapshotDirectory) { throw '快照目錄已存在，拒絕覆蓋。' }
$captureStartedAt = (Get-Date).ToString('o')
$captureTimer = [System.Diagnostics.Stopwatch]::StartNew()
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $kitRoot 'CAPTURE_LATEST_R3.ps1') -OutputDirectory $snapshotDirectory -MaintenanceConfirmed
$captureExitCode = $LASTEXITCODE
$captureTimer.Stop()
if ($captureExitCode -ne 0) { throw '快照失敗：請保留錯誤訊息與輸出目錄，不可拿失敗副本演練。' }
$captureFinishedAt = (Get-Date).ToString('o')
$captureReport = Get-Content -LiteralPath (Join-Path $snapshotDirectory 'CAPTURE_REPORT.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$snapshotInventory = Get-Content -LiteralPath (Join-Path $snapshotDirectory 'r3_inventory.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$snapshotHash = (Get-FileHash -LiteralPath (Join-Path $snapshotDirectory 'latest_r3.sqlite') -Algorithm SHA256).Hash
if ($captureReport.status -ne 'verified_capture') { throw '快照未驗證成功。' }
if ($snapshotHash -ne $captureReport.snapshot_sha256) { throw '快照SHA-256不符。' }
if ($captureReport.source_sha256_before -ne $captureReport.source_sha256_after -or -not $captureReport.source_logical_unchanged) { throw '來源前後不一致，停止。' }
if (-not $captureReport.assets_verified -or -not $captureReport.configurations_verified) { throw '附件或設定副本不一致。' }
if (@($snapshotInventory.integrity_check).Count -ne 1 -or $snapshotInventory.integrity_check[0] -ne 'ok' -or @($snapshotInventory.foreign_key_errors).Count -ne 0) { throw 'SQLite完整性或外鍵未通過。' }
$tableCounts = foreach ($table in $snapshotInventory.tables.PSObject.Properties) {
    [pscustomobject]@{ Table = $table.Name; Rows = $table.Value.count }
}
$tableCounts | Sort-Object Table | Format-Table -AutoSize
$handoffSummary = [ordered]@{
    status = 'verified_capture_ready_for_offline_rehearsal'
    source = $captureReport.source
    started_at = $captureStartedAt
    finished_at = $captureFinishedAt
    capture_seconds = [Math]::Round($captureTimer.Elapsed.TotalSeconds, 2)
    snapshot_sha256 = $snapshotHash
    integrity_check = 'ok'
    foreign_key_errors = 0
    table_counts = @($tableCounts)
}
$summaryPath = Join-Path $snapshotDirectory 'CAPTURE_HANDOFF_SUMMARY.json'
if (Test-Path -LiteralPath $summaryPath) { throw '交接摘要已存在，拒絕覆蓋。' }
$handoffSummary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
Write-Host ('成功，請回傳整個資料夾：' + $snapshotDirectory)
Write-Host ('快照SHA-256：' + $snapshotHash)
Write-Host ('快照耗時秒數：' + $captureTimer.Elapsed.TotalSeconds)
Write-Host '現在可恢復原R3使用；不要啟動新版或BHQ。'
```

實際使用的工具是`CAPTURE_LATEST_R3.ps1`＋`tools/r4_deployment_preflight.py capture-r3`，使用套件內建Python，不需要安裝系統Python。

它先取得與ERP相同的OS寫入鎖，來源使用mode=ro＋query_only，再以SQLite backup API取得包含已提交WAL的一致快照。**不是在寫入中複製orders.db。** 不對R3套migration、不寫backup_records、不checkpoint、不修改原SQLite；共用鎖可能建立獨立orders.db.write.lock檔案。

若出現資料庫正在更新、可見程序仍執行、running scan_job、完整性／外鍵失敗或其他錯誤：停止並回傳完整訊息。即使懷疑是舊狀態，也不能自己改scan_jobs、清除SQLite紀錄或繞過檢查。失敗輸出只留作調查，不拿來演練。

### 4. 回傳哪些檔案

請將剛產生的整個`R3_CAPTURE_時間戳`資料夾，使用USB／核准媒介或壓縮後回傳。這包含業務資料，不要公開分享。壓縮可能耗時；**壓縮期間可以恢復原R3使用**，因為壓縮的是已完成的副本，不是來源資料庫。

應包含：

- latest_r3.sqlite
- CAPTURE_REPORT.json、CAPTURE_HANDOFF_SUMMARY.json
- r3_inventory.json（全部資料表數量、原列雜湊、migration等）
- assets_manifest.json、configuration_manifest.json
- assets資料夾（data/storage/runs/control/backups與相關設定；其中不再複製主orders.db）

不要只回傳orders.db或只傳JSON，缺少附件就不能完成圖片保留驗收。資料太大時可先回傳上述JSON，以便先核對，但實際升級與附件驗證仍須收到完整快照及附件。使用既有壓縮工具或直接USB複製資料夾；不要因大小限制排除檔案。來源資料夾與快照副本都先保留，不刪除。

## 二、收到最新快照後由實作者執行的離線演練

### 來源驗證與隔離

1. 驗證CAPTURE_REPORT的來源正確、時間、SHA、成功結果；重新唯讀檢查快照完整性、外鍵、marker、全部表／版本／人工確認數量，核對附件與設定manifest。
2. 將原始快照作唯讀基準保留，在本機獨立演練目錄建立工作副本；不能使用開發機data/orders.db，不能觸碰目標R3或未來固定正式庫。
3. 與先前2026-08-26回傳本及開發參考資料分別比對，只列出新增／不同，不按ID、日期或筆數自動合併。目標最新快照是唯一基底。

### migration與ERP資料保留

4. 先唯讀核對全部已套用migration的名稱與checksum，使用既有apply_migrations相同計算方式。任何不一致立即停止，不能UPDATE database_migrations強行通過。
5. 完整v7不重跑007；v6且001～006吻合才在工作副本備份後套用已核准007。其他版本／缺漏／不明結構先回報，不能自動初始化。
6. 使用既有備份／backup_records機制只備份及登錄工作副本。每步記錄結構、數量、SHA與完整性，既有原始快照不寫入。
7. 結構升級後，以原欄位投影驗證全部原資料表與原列不變，ERP訂單／版本／明細、人工確認、排除、稽核與附件不得遺失。007只加BHQ結構，ERP discounted_total_amount維持NULL。
8. 原有R4資料修復不能直接重播。若需修正某筆舊資料，另列差異與原因待核准，不默默改寫目標的人工決定。

### BHQ是獨立驗證，不會隨migration自動出現

9. 先計算快照BHQ訂單、版本、明細、門市與金額；已有資料則保留原來源、版本與稽核，只核對。若部分匯入、金額不同或內容衝突，停止回報，不用「已有BHQ」直接判通過。
10. 完全沒有BHQ時，使用已核准7月原始XLS在演練工作副本匯入。該來源本機已找到，SHA為`9F3C1CA4B567B3E57F3EBF05AE07333BFCA7DC61202B2E2CFA0440063C1977BB`，匯入前再次核對。
11. 沿用既有共用匯入／版本服務及OS鎖，單檔transaction。現有正式與測試入口的路徑防護不得移除；使用隔離演練副本的受控流程，不把工作副本偽裝為auntstella正式路徑。
12. 驗證257張、551筆、18間門市、訂單層金額8,742,030；352空代碼排除、15筆零數量、79筆重複產品代碼列、3張原分類..均保留規則。第二次同檔匯入orders／order_versions／order_items／stores／products核心增量全部0。
13. 核對257個單號有無與ERP撞號、實際產品代碼及名稱保護。若產品名稱原本空白，補值須有原值／新值／稽核對照；不能讓允許的產品補值掩蓋其他ERP原列改動。差異與新增audit／import run紀錄分開報告。

### 附件、網站與共用鎖

14. 驗證manifest逐檔SHA、DB引用的待確認圖片及附件能找到且可讀，列出missing／external／invalid清單；原本缺檔與本次搬移漏檔分開，不能以「有複製assets」當成全部圖片通過。外部絕對路徑需要另行核對，不自動猜路徑或修資料。
15. 網站在本機使用演練副本測試，ERP查詢規則不變，BHQ／全部來源金額按訂單只算一次。人工確認回歸使用另一份可丟棄的測試副本，不能污染保留性驗收基準。
16. 檢查ERP、網站、BHQ正式設定皆為唯一固定C:\Users\auntstella\Desktop\V6_DB_WEB\data\orders.db；以臨時資料庫做共用OS鎖競爭測試，不執行ERP擷取／OCR。**目標固定路徑尚未部署，離線設定／鎖測試不冒充目標機正式連線驗收。**

## 三、正式切換的後續步驟（現在不執行）

演練通過並另獲正式切換核准後：

1. 確認停機時段、回復負責人及驗收清單；安全暫停舊R3寫入。
2. 重新取得切換當下最新R3快照與附件。若演練期間仍有新增資料，從這份最新資料重建／驗證候選庫，不能部署先前已過期的演練副本。
3. 驗證最新來源備份與候選庫階段備份可讀、SHA、完整性／外鍵；核對全部資料保留與核准的BHQ增量。
4. 確認固定V6_DB_WEB目錄狀態：若已存在，先備份與核對，不能直接覆蓋；不存在才按核准方案放入程式、候選庫和附件。保留舊R3原目錄。
5. 在固定路徑驗證ERP／網站／BHQ路徑一致、v7/checksum、marker、圖片、統計與共用鎖；先不執行掃描，不啟動BHQ。
6. 正式驗收通過，才核准deployment_acceptance.json並更新日常入口指向固定專案，同時避免舊R3再次寫入。**這些入口變更不在本次快照／演練階段執行。**
7. BHQ仍維持false，日後另外核准監看器安裝與啟用。

失敗時，若新版尚無新寫入，安全退出新版後恢復原R3操作。若新版已有新ERP或人工資料，先備份新版並保留增量，再決定回移／修復；不能直接用舊備份覆蓋造成資料遺失。

### 停機時間

目前沒有最新資料大小與目標機實測速度，不能提供已驗證的確切分鐘數。正式切換可先預留**60～120分鐘作規劃窗口**，不是完成承諾；附件量大時可能更久。演練後會以「最新快照＋附件搬移＋必要migration／BHQ匯入＋驗證」各步實測耗時，加回復緩衝重新估算，再確認窗口。若不符合窗口則不切換。

本次只需在快照工具執行期間暫停R3寫入；工具準備與回傳壓縮可在維護窗口外進行。請回傳CAPTURE_HANDOFF_SUMMARY內實測capture_seconds，作後續估算依據。

## 四、本次停止點與待交付的實測報告

現在等待auntstella回傳新產生的完整R3_CAPTURE資料夾。尚不能填寫「最新快照時間／SHA、升級前後數量、ERP保留、圖片可讀、BHQ匯入與冪等」為通過。

收到後依序完成上述演練，產出Markdown／JSON實測報告，再等待正式切換核准。不操作目標機、不停用舊入口、不安裝BHQ排程。
