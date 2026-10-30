ALTER TABLE stores
ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
CHECK (status IN ('active', 'inactive'));

INSERT INTO stores(
    store_code, store_name, active, created_at, updated_at, status
)
VALUES (
    'SR', 'SR', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'active'
)
ON CONFLICT(store_code) DO UPDATE SET
    store_name = 'SR',
    active = 1,
    status = 'active',
    updated_at = CURRENT_TIMESTAMP;

UPDATE orders
SET store_id = (SELECT id FROM stores WHERE store_code = 'SR'),
    updated_at = CURRENT_TIMESTAMP
WHERE id IN (SELECT id FROM official_orders);

UPDATE orders
SET store_id = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE id NOT IN (SELECT id FROM official_orders);

DELETE FROM store_department_mappings;
DELETE FROM stores WHERE store_code <> 'SR';

INSERT INTO system_settings(
    setting_key, setting_value, value_type, description, updated_at
)
VALUES (
    'official_store_policy',
    'SR',
    'text',
    'All official wedding orders use the single SR store',
    CURRENT_TIMESTAMP
)
ON CONFLICT(setting_key) DO UPDATE SET
    setting_value = 'SR',
    description = 'All official wedding orders use the single SR store',
    updated_at = CURRENT_TIMESTAMP;

INSERT INTO audit_logs(
    event_type, entity_type, entity_id, actor, details_json, created_at
)
VALUES (
    'sr_store_policy_migration',
    'system_settings',
    'official_store_policy',
    'migration_005',
    '{"store_code":"SR","scope":"official_orders","department_code_preserved":true}',
    CURRENT_TIMESTAMP
);
