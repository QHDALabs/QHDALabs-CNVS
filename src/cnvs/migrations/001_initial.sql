CREATE TABLE canonical_records (
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    schema_version TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (record_type, record_id),
    CHECK (record_type IN ('event', 'source', 'claim', 'evidence', 'assessment')),
    FOREIGN KEY (record_type, record_id, revision)
        REFERENCES record_revisions (record_type, record_id, revision)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE record_revisions (
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    schema_version TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (record_type, record_id, revision)
);

CREATE INDEX record_revisions_recorded_at
    ON record_revisions (record_type, record_id, recorded_at);

CREATE TRIGGER canonical_records_revision_guard_insert
BEFORE INSERT ON canonical_records
WHEN NOT EXISTS (
    SELECT 1 FROM record_revisions
    WHERE record_type = NEW.record_type
      AND record_id = NEW.record_id
      AND revision = NEW.revision
      AND schema_version = NEW.schema_version
      AND payload_json = NEW.payload_json
)
BEGIN
    SELECT RAISE(ABORT, 'current records must match an immutable revision');
END;

CREATE TRIGGER canonical_records_revision_guard_update
BEFORE UPDATE ON canonical_records
WHEN NOT EXISTS (
    SELECT 1 FROM record_revisions
    WHERE record_type = NEW.record_type
      AND record_id = NEW.record_id
      AND revision = NEW.revision
      AND schema_version = NEW.schema_version
      AND payload_json = NEW.payload_json
)
BEGIN
    SELECT RAISE(ABORT, 'current records must match an immutable revision');
END;

CREATE TRIGGER canonical_records_no_delete
BEFORE DELETE ON canonical_records
BEGIN
    SELECT RAISE(ABORT, 'canonical records must be revised, not deleted');
END;

CREATE TABLE raw_snapshots (
    content_sha256 TEXT PRIMARY KEY CHECK (length(content_sha256) = 64),
    media_type TEXT NOT NULL,
    content BLOB NOT NULL,
    byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE assessment_revisions (
    record_type TEXT NOT NULL DEFAULT 'assessment' CHECK (record_type = 'assessment'),
    assessment_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    event_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    record_set_json TEXT NOT NULL CHECK (json_valid(record_set_json)),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (assessment_id, revision),
    FOREIGN KEY (record_type, assessment_id, revision)
        REFERENCES record_revisions (record_type, record_id, revision)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TRIGGER assessment_revisions_no_update
BEFORE UPDATE ON assessment_revisions
BEGIN
    SELECT RAISE(ABORT, 'assessment revisions are immutable');
END;

CREATE TRIGGER assessment_revisions_no_delete
BEFORE DELETE ON assessment_revisions
BEGIN
    SELECT RAISE(ABORT, 'assessment revisions are immutable');
END;

CREATE TRIGGER record_revisions_no_update
BEFORE UPDATE ON record_revisions
BEGIN
    SELECT RAISE(ABORT, 'record revisions are immutable');
END;

CREATE TRIGGER record_revisions_no_delete
BEFORE DELETE ON record_revisions
BEGIN
    SELECT RAISE(ABORT, 'record revisions are immutable');
END;

CREATE TRIGGER raw_snapshots_no_update
BEFORE UPDATE ON raw_snapshots
BEGIN
    SELECT RAISE(ABORT, 'raw snapshots are immutable');
END;

CREATE TRIGGER raw_snapshots_no_delete
BEFORE DELETE ON raw_snapshots
BEGIN
    SELECT RAISE(ABORT, 'raw snapshots are immutable');
END;
