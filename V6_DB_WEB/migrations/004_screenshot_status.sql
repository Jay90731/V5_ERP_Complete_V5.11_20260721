ALTER TABLE screenshots
ADD COLUMN image_status TEXT NOT NULL DEFAULT 'available'
CHECK (image_status IN ('available', 'missing', 'archived', 'deleted'));
