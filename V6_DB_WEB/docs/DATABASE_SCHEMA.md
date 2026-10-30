# V6 資料表與欄位說明

## orders

每張訂單一列，以 `order_no` 唯一識別。

| 欄位 | 說明 |
|---|---|
| id | SQLite 主鍵 |
| order_no | ERP 訂單編號，唯一 |
| current_version_id | 目前版本，指向 `order_versions` |
| current_content_hash | 目前完整訂單內容 SHA-256 |
| order_date | 訂貨日期，ISO `YYYY-MM-DD` |
| category | 訂單分類 |
| department_code | ERP 原始部門代號 |
| store_id | 正式統計訂單固定指向 SR；排除資料為 SQL NULL |
| is_voided | 是否作廢 |
| status | confirmed 或 archived |
| first_seen_at / last_seen_at | 首次及最近看見時間 |
| created_at / updated_at | 建立及更新時間 |

SR 指派只更新 `orders.store_id`，不屬於訂單內容異動，不建立新版本。`department_code` 永遠保存 ERP 原始值，不改成 SR。

## order_versions

保存每張訂單的完整版本。`order_id + content_hash` 唯一，相同內容不重複新增。

| 欄位 | 說明 |
|---|---|
| order_id | 所屬訂單 |
| version_no | 訂單內遞增版本號 |
| content_hash | 日期、分類、部門、作廢狀態及完整明細的 SHA-256 |
| order_date / category / department_code | 此版本主檔 |
| detail_count | 明細列數 |
| total_quantity | 明細數量合計 |
| total_list_amount | 明細總金額合計 |
| total_discount_amount | 折後金額合計 |
| source_system | 來源系統 |
| source_version_id | V5.10 原版本識別 |
| source_payload_json | 原始版本 JSON |
| effective_at | 版本生效時間 |

## order_items

一個版本可以有多筆明細。使用 `line_no` 保存原順序；相同明細列不會自行合併。

| 欄位 | 說明 |
|---|---|
| order_version_id | 所屬版本 |
| line_no | 明細順序 |
| gift_code | ERP 禮盒代號 |
| product_id | 對應 `products`，可為 NULL |
| list_price | 定價 |
| quantity | 數量 |
| total_amount | 總金額 |
| discount_amount | 折後金額 |
| item_hash | 單列內容 SHA-256 |

## scan_jobs

每次掃描或匯入工作。`job_type` 可使用 `daily_7_days`、`monthly_6_months`、`history_2_years`、`single_order` 與 `v5_import`。

| 欄位 | 說明 |
|---|---|
| date_from / date_to | 查詢日期區間 |
| status | running、completed、failed 或 partial |
| total_count | 本批 OCR 總筆數 |
| success_count | 新增訂單數 |
| unchanged_count | 未變更數 |
| changed_count | 已變更數 |
| pending_count | 待確認數 |
| excluded_count | 非目標部門及作廢數 |
| failed_count | 單筆失敗數 |
| source_file | OCR JSON 來源 |
| summary_json | 批次摘要 |

## scan_items

掃描工作中的每張圖片及訂單處理狀態。保存原始 OCR payload、驗證結果、圖片狀態、結果代碼及錯誤原因。

| 欄位 | 說明 |
|---|---|
| scan_job_id | 所屬掃描工作 |
| source_key / temporary_key | 圖片或暫時識別，供失敗資料去重 |
| order_no | 校正後可識別的 ERP 訂單編號 |
| order_id / order_version_id | 成功寫入時連結訂單及版本 |
| result_code | new、unchanged、changed、pending、non_target_department、voided、failed |
| raw_ocr_payload_json | 原始 OCR JSON，不因校正而覆蓋 |
| validation_json | 驗證結果與校正警告 |
| image_hash | 圖片 SHA-256 |
| image_relative_path | 圖片相對路徑 |
| image_status | available、missing 或 not_provided |
| reason | 待確認、排除或失敗原因 |

## review_queue

待確認與人工處理佇列。使用 `source_key` 去重，未處理資料不會因下次匯入消失。

| 欄位 | 說明 |
|---|---|
| source_key | 來源唯一鍵 |
| order_no | 能辨識時保存訂單編號 |
| status | pending、retry_requested、excluded、resolved |
| reason | 待確認原因 |
| processing_note | 人工備註 |
| raw_payload_json | 原始待確認資料 |
| first_seen_at / last_seen_at | 首次及最近出現時間 |

## screenshots

只保存圖片資訊，不保存 BLOB。

| 欄位 | 說明 |
|---|---|
| sha256 | 圖片 SHA-256，唯一 |
| relative_path | 相對路徑，唯一 |
| order_version_id | 所屬訂單版本 |
| image_role | official、pending_review、ocr_temp |
| protected_until_review | 待確認完成前禁止刪除 |

部分唯一索引確保每個訂單版本最多一張 `official` 圖片。

## official_orders / official_order_items

第二階段建立的正式統計 view。只包含部門代號 4001、非作廢且目前版本具有明細的訂單。非 4001、作廢及待確認資料不會出現在 view 中。

## products

以 `gift_code` 保存品號，`product_name` 預留 MENUITEM 中文品名。

## stores

門市主檔只保留一筆啟用中的 SR：`store_code=SR`、`store_name=SR`、`status=active`。

## store_department_mappings

保留既有資料表結構以維持 migration 相容性，目前不使用且不建立門市配對資料。專案已取消門市系統匯出、訂單編號門市配對及門市配對待確認功能。

## audit_logs

保存匯入、人工修正、備份還原等操作紀錄。

## system_settings

系統設定鍵值。不得保存 ERP 密碼。

## database_migrations

保存 migration 版本、檔名、SHA-256 及套用時間。

## backup_records

保存備份相對路徑、SHA-256、大小、驗證及還原狀態。
