CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    order_no TEXT NOT NULL UNIQUE,
    current_version_id INTEGER,
    current_content_hash TEXT NOT NULL,
    order_date TEXT NOT NULL,
    category TEXT NOT NULL,
    department_code TEXT NOT NULL,
    store_id INTEGER,
    is_voided INTEGER NOT NULL DEFAULT 0 CHECK (is_voided IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'archived')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE SET NULL,
    FOREIGN KEY (current_version_id) REFERENCES order_versions(id)
);

CREATE TABLE IF NOT EXISTS order_versions (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    order_date TEXT NOT NULL,
    category TEXT NOT NULL,
    department_code TEXT NOT NULL,
    is_voided INTEGER NOT NULL DEFAULT 0 CHECK (is_voided IN (0, 1)),
    detail_count INTEGER NOT NULL DEFAULT 0,
    total_quantity INTEGER NOT NULL DEFAULT 0,
    total_list_amount INTEGER NOT NULL DEFAULT 0,
    total_discount_amount INTEGER NOT NULL DEFAULT 0,
    source_system TEXT NOT NULL,
    source_version_id TEXT,
    source_payload_json TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    UNIQUE (order_id, version_no),
    UNIQUE (order_id, content_hash)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    gift_code TEXT NOT NULL UNIQUE,
    product_name TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_version_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    gift_code TEXT NOT NULL,
    product_id INTEGER,
    list_price INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    total_amount INTEGER NOT NULL,
    discount_amount INTEGER NOT NULL,
    item_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_version_id) REFERENCES order_versions(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL,
    UNIQUE (order_version_id, line_no)
);

CREATE TABLE IF NOT EXISTS scan_jobs (
    id INTEGER PRIMARY KEY,
    job_type TEXT NOT NULL CHECK (job_type IN ('daily_7d', 'monthly_6m', 'history_2y', 'single_order', 'v5_import')),
    query_start_date TEXT,
    query_end_date TEXT,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'stopped')),
    started_at TEXT,
    finished_at TEXT,
    source_name TEXT,
    summary_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_items (
    id INTEGER PRIMARY KEY,
    scan_job_id INTEGER NOT NULL,
    order_no TEXT,
    order_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('queued', 'captured', 'ocr_success', 'pending_review', 'imported', 'skipped', 'failed')),
    screenshot_id INTEGER,
    reason TEXT,
    processed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (scan_job_id) REFERENCES scan_jobs(id) ON DELETE CASCADE,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL,
    FOREIGN KEY (screenshot_id) REFERENCES screenshots(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    order_no TEXT,
    scan_item_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'retry_requested', 'excluded', 'resolved')),
    reason TEXT NOT NULL,
    processing_note TEXT,
    raw_payload_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (scan_item_id) REFERENCES scan_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
    relative_path TEXT NOT NULL UNIQUE,
    order_version_id INTEGER,
    scan_item_id INTEGER,
    image_role TEXT NOT NULL CHECK (image_role IN ('official', 'pending_review', 'ocr_temp')),
    file_size INTEGER,
    captured_at TEXT,
    protected_until_review INTEGER NOT NULL DEFAULT 0 CHECK (protected_until_review IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_version_id) REFERENCES order_versions(id) ON DELETE SET NULL,
    FOREIGN KEY (scan_item_id) REFERENCES scan_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY,
    store_code TEXT NOT NULL UNIQUE,
    store_name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_department_mappings (
    id INTEGER PRIMARY KEY,
    department_code TEXT NOT NULL,
    store_id INTEGER NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE,
    UNIQUE (department_code, valid_from)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'text',
    description TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_records (
    id INTEGER PRIMARY KEY,
    backup_type TEXT NOT NULL CHECK (backup_type IN ('manual', 'scheduled', 'pre_update')),
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    database_size INTEGER,
    status TEXT NOT NULL CHECK (status IN ('created', 'verified', 'failed', 'restored')),
    created_at TEXT NOT NULL,
    verified_at TEXT,
    restored_at TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date);
CREATE INDEX IF NOT EXISTS idx_orders_department ON orders(department_code);
CREATE INDEX IF NOT EXISTS idx_order_versions_order ON order_versions(order_id, version_no);
CREATE INDEX IF NOT EXISTS idx_order_items_version ON order_items(order_version_id, line_no);
CREATE INDEX IF NOT EXISTS idx_scan_jobs_type_created ON scan_jobs(job_type, created_at);
CREATE INDEX IF NOT EXISTS idx_scan_items_job_status ON scan_items(scan_job_id, status);
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_screenshots_version ON screenshots(order_version_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_official_screenshot_per_version
ON screenshots(order_version_id)
WHERE image_role = 'official' AND order_version_id IS NOT NULL;

