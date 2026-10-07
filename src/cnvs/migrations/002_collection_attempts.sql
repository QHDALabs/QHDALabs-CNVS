CREATE TABLE collection_attempts (
    collection_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_metadata_json TEXT NOT NULL
        CHECK (json_valid(source_metadata_json))
        CHECK (json_type(source_metadata_json) = 'object'),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    status TEXT NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED', 'BLOCKED')),
    retry_of TEXT UNIQUE REFERENCES collection_attempts (collection_id),
    source_url TEXT NOT NULL,
    final_url TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    http_status INTEGER CHECK (http_status BETWEEN 100 AND 599),
    content_type TEXT,
    content_sha256 TEXT CHECK (
        content_sha256 IS NULL OR length(content_sha256) = 64
    ),
    archived_content_sha256 TEXT REFERENCES raw_snapshots (content_sha256),
    origin_identifiers_json TEXT NOT NULL
        CHECK (json_valid(origin_identifiers_json))
        CHECK (json_type(origin_identifiers_json) = 'array'),
    error_code TEXT,
    error_message TEXT,
    UNIQUE (source_id, attempt_number),
    CHECK (
        archived_content_sha256 IS NULL
        OR archived_content_sha256 = content_sha256
    ),
    CHECK (
        status != 'SUCCEEDED'
        OR (
            http_status BETWEEN 200 AND 299
            AND final_url IS NOT NULL
            AND content_type IS NOT NULL
            AND content_sha256 IS NOT NULL
        )
    ),
    CHECK (
        (status = 'SUCCEEDED' AND error_code IS NULL AND error_message IS NULL)
        OR (status != 'SUCCEEDED' AND error_code IS NOT NULL AND error_message IS NOT NULL)
    )
);

CREATE INDEX collection_attempts_source_history
    ON collection_attempts (source_id, attempt_number DESC);

CREATE TRIGGER collection_attempt_retry_guard
BEFORE INSERT ON collection_attempts
WHEN (
    NEW.retry_of IS NULL
    AND NEW.attempt_number != 1
) OR (
    NEW.retry_of IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM collection_attempts previous
        WHERE previous.collection_id = NEW.retry_of
          AND previous.source_id = NEW.source_id
          AND previous.attempt_number = NEW.attempt_number - 1
          AND previous.status = 'FAILED'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'collection retries must follow a failed attempt');
END;

CREATE TRIGGER collection_attempts_no_update
BEFORE UPDATE ON collection_attempts
BEGIN
    SELECT RAISE(ABORT, 'collection attempts are immutable');
END;

CREATE TRIGGER collection_attempts_no_delete
BEFORE DELETE ON collection_attempts
BEGIN
    SELECT RAISE(ABORT, 'collection attempts are immutable');
END;
