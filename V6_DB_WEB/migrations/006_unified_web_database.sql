CREATE TABLE IF NOT EXISTS web_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_metadata (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_order_metadata (
    order_id INTEGER PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
    customer_name TEXT,
    customer_source TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history_month_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL UNIQUE,
    expected_order_no TEXT NOT NULL,
    first_observed_order_no TEXT,
    status TEXT NOT NULL CHECK(status IN ('open', 'covered')),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_cleanup_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    retention_days INTEGER NOT NULL,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    protected_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_pending_images (
    review_queue_id INTEGER PRIMARY KEY REFERENCES review_queue(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    source_relative_path TEXT,
    copied_at TEXT NOT NULL,
    processed_at TEXT,
    retained_until TEXT,
    image_status TEXT NOT NULL DEFAULT 'available'
        CHECK(image_status IN ('available', 'missing', 'deleted'))
);

CREATE TABLE IF NOT EXISTS web_order_version_metadata (
    order_version_id INTEGER PRIMARY KEY REFERENCES order_versions(id) ON DELETE CASCADE,
    customer_name TEXT NOT NULL DEFAULT '',
    review_queue_id INTEGER REFERENCES review_queue(id) ON DELETE SET NULL,
    manual_note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_order_item_metadata (
    order_item_id INTEGER PRIMARY KEY REFERENCES order_items(id) ON DELETE CASCADE,
    product_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_manual_review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_queue_id INTEGER NOT NULL REFERENCES review_queue(id),
    action TEXT NOT NULL CHECK(action IN ('confirmed', 'excluded', 'held')),
    order_no TEXT,
    write_result TEXT NOT NULL,
    backup_relative_path TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    note TEXT,
    actor TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_web_orders_date_category
    ON orders(order_date, category);
CREATE INDEX IF NOT EXISTS idx_web_orders_department_status
    ON orders(department_code, is_voided, status);
CREATE INDEX IF NOT EXISTS idx_web_items_version_code
    ON order_items(order_version_id, gift_code);
CREATE INDEX IF NOT EXISTS idx_web_review_status_updated
    ON review_queue(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_web_products_name
    ON products(product_name);
CREATE INDEX IF NOT EXISTS idx_web_manual_events_review
    ON web_manual_review_events(review_queue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_web_version_customer
    ON web_order_version_metadata(customer_name);

INSERT OR IGNORE INTO web_schema_migrations(version, name, applied_at)
VALUES(1, 'web_v1_read_model', CURRENT_TIMESTAMP);

INSERT OR IGNORE INTO web_schema_migrations(version, name, applied_at)
VALUES(2, 'web_v1_manual_review_writeback', CURRENT_TIMESTAMP);

INSERT OR IGNORE INTO web_order_metadata(
    order_id, customer_name, customer_source, updated_at
)
SELECT id, NULL, 'source_not_available', CURRENT_TIMESTAMP FROM orders;

INSERT INTO system_settings(
    setting_key, setting_value, value_type, description, updated_at
)
VALUES(
    'unified_formal_database',
    'data/orders.db',
    'text',
    'Single writable SQLite used by ERP scans and the local website',
    CURRENT_TIMESTAMP
)
ON CONFLICT(setting_key) DO UPDATE SET
    setting_value = 'data/orders.db',
    description = 'Single writable SQLite used by ERP scans and the local website',
    updated_at = CURRENT_TIMESTAMP;
