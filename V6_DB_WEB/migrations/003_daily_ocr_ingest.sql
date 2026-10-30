CREATE TABLE scan_jobs_new (
    id INTEGER PRIMARY KEY,
    job_type TEXT NOT NULL CHECK (
        job_type IN (
            'daily_7_days',
            'monthly_6_months',
            'history_2_years',
            'single_order',
            'v5_import'
        )
    ),
    date_from TEXT,
    date_to TEXT,
    query_start_date TEXT,
    query_end_date TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'stopped')
    ),
    total_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    source_name TEXT,
    source_file TEXT,
    summary_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO scan_jobs_new(
    id, job_type, date_from, date_to, query_start_date, query_end_date,
    status, total_count, success_count, unchanged_count, changed_count,
    pending_count, excluded_count, failed_count, started_at, finished_at,
    source_name, source_file, summary_json, error_message, created_at, updated_at
)
SELECT
    id,
    CASE job_type
        WHEN 'daily_7d' THEN 'daily_7_days'
        WHEN 'monthly_6m' THEN 'monthly_6_months'
        WHEN 'history_2y' THEN 'history_2_years'
        ELSE job_type
    END,
    query_start_date,
    query_end_date,
    query_start_date,
    query_end_date,
    status,
    0, 0, 0, 0, 0, 0, 0,
    started_at,
    finished_at,
    source_name,
    NULL,
    summary_json,
    error_message,
    created_at,
    updated_at
FROM scan_jobs;

CREATE TABLE scan_items_backup AS SELECT * FROM scan_items;
DROP TABLE scan_items;
DROP TABLE scan_jobs;
ALTER TABLE scan_jobs_new RENAME TO scan_jobs;

CREATE TABLE scan_items (
    id INTEGER PRIMARY KEY,
    scan_job_id INTEGER NOT NULL,
    source_key TEXT NOT NULL,
    image_name TEXT,
    temporary_key TEXT,
    order_no TEXT,
    order_id INTEGER,
    order_version_id INTEGER,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued',
            'captured',
            'ocr_success',
            'pending_review',
            'imported',
            'skipped',
            'failed'
        )
    ),
    result_code TEXT NOT NULL CHECK (
        result_code IN (
            'new',
            'unchanged',
            'changed',
            'pending',
            'non_target_department',
            'voided',
            'failed'
        )
    ),
    screenshot_id INTEGER,
    image_hash TEXT CHECK (image_hash IS NULL OR length(image_hash) = 64),
    image_relative_path TEXT,
    image_status TEXT NOT NULL DEFAULT 'unknown' CHECK (
        image_status IN ('available', 'missing', 'not_provided', 'unknown')
    ),
    raw_ocr_payload_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    reason TEXT,
    started_at TEXT,
    finished_at TEXT,
    processed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (scan_job_id) REFERENCES scan_jobs(id) ON DELETE CASCADE,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL,
    FOREIGN KEY (order_version_id) REFERENCES order_versions(id) ON DELETE SET NULL,
    FOREIGN KEY (screenshot_id) REFERENCES screenshots(id) ON DELETE SET NULL,
    UNIQUE (scan_job_id, source_key)
);

INSERT INTO scan_items(
    id, scan_job_id, source_key, image_name, temporary_key, order_no,
    order_id, order_version_id, status, result_code, screenshot_id,
    image_hash, image_relative_path, image_status, raw_ocr_payload_json,
    validation_json, reason, started_at, finished_at, processed_at, created_at
)
SELECT
    id,
    scan_job_id,
    'legacy:' || id,
    NULL,
    NULL,
    order_no,
    order_id,
    NULL,
    status,
    CASE
        WHEN status = 'failed' THEN 'failed'
        WHEN status = 'pending_review' THEN 'pending'
        WHEN status = 'skipped' THEN 'non_target_department'
        ELSE 'unchanged'
    END,
    screenshot_id,
    NULL,
    NULL,
    'unknown',
    '{}',
    '{}',
    reason,
    NULL,
    NULL,
    processed_at,
    created_at
FROM scan_items_backup;

DROP TABLE scan_items_backup;

CREATE INDEX idx_scan_jobs_type_created
ON scan_jobs(job_type, created_at);

CREATE INDEX idx_scan_items_job_status
ON scan_items(scan_job_id, status);

CREATE INDEX idx_scan_items_job_result
ON scan_items(scan_job_id, result_code);

CREATE INDEX idx_scan_items_order
ON scan_items(order_no, processed_at);

CREATE VIEW official_orders AS
SELECT
    o.*,
    v.detail_count,
    v.total_quantity,
    v.total_list_amount,
    v.total_discount_amount
FROM orders o
JOIN order_versions v ON v.id = o.current_version_id
WHERE
    o.department_code = '4001'
    AND o.is_voided = 0
    AND o.status = 'confirmed'
    AND v.detail_count > 0;

CREATE VIEW official_order_items AS
SELECT i.*
FROM order_items i
JOIN official_orders o ON o.current_version_id = i.order_version_id;

