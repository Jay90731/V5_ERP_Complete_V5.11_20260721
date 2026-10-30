ALTER TABLE order_versions ADD COLUMN discounted_total_amount INTEGER;

CREATE TABLE bhq_import_runs (
    id INTEGER PRIMARY KEY,
    source_filename TEXT NOT NULL,
    source_file_sha256 TEXT NOT NULL CHECK(length(source_file_sha256) = 64),
    sheet_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source_row_count INTEGER NOT NULL DEFAULT 0,
    source_order_count INTEGER NOT NULL DEFAULT 0,
    inserted_order_count INTEGER NOT NULL DEFAULT 0,
    inserted_version_count INTEGER NOT NULL DEFAULT 0,
    inserted_item_count INTEGER NOT NULL DEFAULT 0,
    skipped_order_count INTEGER NOT NULL DEFAULT 0,
    anomaly_count INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL CHECK(result IN ('running', 'completed', 'failed')),
    error_summary TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE bhq_store_aliases (
    source_store_name TEXT PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE bhq_order_version_metadata (
    order_version_id INTEGER PRIMARY KEY REFERENCES order_versions(id) ON DELETE CASCADE,
    import_run_id INTEGER NOT NULL REFERENCES bhq_import_runs(id),
    raw_store_name TEXT NOT NULL,
    original_total_amount INTEGER NOT NULL,
    source_rows_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_bhq_import_file
ON bhq_import_runs(source_file_sha256, started_at);

CREATE INDEX idx_bhq_version_import
ON bhq_order_version_metadata(import_run_id);

CREATE INDEX idx_order_versions_source_discounted
ON order_versions(source_system, discounted_total_amount);
