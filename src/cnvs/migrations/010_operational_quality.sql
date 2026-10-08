CREATE TABLE processing_attempts (
    processing_attempt_id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES collection_attempts (collection_id),
    stage TEXT NOT NULL CHECK (stage = 'NORMALIZATION'),
    status TEXT NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    error_code TEXT,
    CHECK (
        (status = 'SUCCEEDED' AND error_code IS NULL)
        OR (status = 'FAILED' AND error_code IS NOT NULL)
    )
);

CREATE INDEX processing_attempts_collection
    ON processing_attempts (collection_id, started_at);
CREATE INDEX processing_attempts_status
    ON processing_attempts (stage, status, error_code);

CREATE TRIGGER collection_attempt_limit
BEFORE INSERT ON collection_attempts
WHEN NEW.attempt_number > 3
BEGIN
    SELECT RAISE(ABORT, 'collection attempt limit reached (3)');
END;

CREATE TRIGGER processing_attempts_no_update
BEFORE UPDATE ON processing_attempts
BEGIN
    SELECT RAISE(ABORT, 'processing attempts are immutable');
END;

CREATE TRIGGER processing_attempts_no_delete
BEFORE DELETE ON processing_attempts
BEGIN
    SELECT RAISE(ABORT, 'processing attempts are immutable');
END;
