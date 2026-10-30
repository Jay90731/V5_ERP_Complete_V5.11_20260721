CREATE TRIGGER IF NOT EXISTS trg_screenshots_relative_path_insert
BEFORE INSERT ON screenshots
WHEN
    NEW.relative_path GLOB '[A-Za-z]:*'
    OR substr(NEW.relative_path, 1, 1) = '/'
    OR substr(NEW.relative_path, 1, 1) = char(92)
BEGIN
    SELECT RAISE(ABORT, 'screenshots.relative_path must be relative');
END;

CREATE TRIGGER IF NOT EXISTS trg_screenshots_relative_path_update
BEFORE UPDATE OF relative_path ON screenshots
WHEN
    NEW.relative_path GLOB '[A-Za-z]:*'
    OR substr(NEW.relative_path, 1, 1) = '/'
    OR substr(NEW.relative_path, 1, 1) = char(92)
BEGIN
    SELECT RAISE(ABORT, 'screenshots.relative_path must be relative');
END;

CREATE TRIGGER IF NOT EXISTS trg_protected_pending_screenshot_delete
BEFORE DELETE ON screenshots
WHEN
    OLD.image_role = 'pending_review'
    AND OLD.protected_until_review = 1
BEGIN
    SELECT RAISE(ABORT, 'pending review screenshot is protected');
END;

