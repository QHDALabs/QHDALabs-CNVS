CREATE TABLE report_snapshots (
    report_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    assessment_id TEXT NOT NULL,
    assessment_revision INTEGER NOT NULL CHECK (assessment_revision >= 1),
    high_impact INTEGER NOT NULL CHECK (high_impact IN (0, 1)),
    generated_at TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL CHECK (json_valid(input_snapshot_json)),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    markdown TEXT NOT NULL,
    html TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    FOREIGN KEY (assessment_id, assessment_revision)
        REFERENCES assessment_revisions (assessment_id, revision)
);

CREATE INDEX report_snapshots_event
    ON report_snapshots (event_id, generated_at, report_id);

CREATE TRIGGER report_snapshots_event_guard
BEFORE INSERT ON report_snapshots
WHEN NOT EXISTS (
    SELECT 1 FROM assessment_revisions
    WHERE assessment_id = NEW.assessment_id
      AND revision = NEW.assessment_revision
      AND event_id = NEW.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'report event must match its assessment snapshot');
END;

CREATE TRIGGER report_snapshots_no_update
BEFORE UPDATE ON report_snapshots
BEGIN
    SELECT RAISE(ABORT, 'report snapshots are immutable');
END;

CREATE TRIGGER report_snapshots_no_delete
BEFORE DELETE ON report_snapshots
BEGIN
    SELECT RAISE(ABORT, 'report snapshots are immutable');
END;

CREATE TABLE report_reviews (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL UNIQUE,
    report_id TEXT NOT NULL REFERENCES report_snapshots (report_id),
    decision TEXT NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX report_reviews_history
    ON report_reviews (report_id, sequence);

CREATE TRIGGER report_reviews_no_update
BEFORE UPDATE ON report_reviews
BEGIN
    SELECT RAISE(ABORT, 'report reviews are append-only');
END;

CREATE TRIGGER report_reviews_no_delete
BEFORE DELETE ON report_reviews
BEGIN
    SELECT RAISE(ABORT, 'report reviews are append-only');
END;
