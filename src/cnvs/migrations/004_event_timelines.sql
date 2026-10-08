CREATE TABLE event_source_matches (
    match_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES normalized_documents (document_id),
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    proposed_by TEXT NOT NULL CHECK (length(trim(proposed_by)) > 0),
    proposed_at TEXT NOT NULL,
    event_record_type TEXT NOT NULL DEFAULT 'event' CHECK (event_record_type = 'event'),
    UNIQUE (event_id, document_id),
    FOREIGN KEY (event_record_type, event_id)
        REFERENCES canonical_records (record_type, record_id)
);

CREATE INDEX event_source_matches_document
    ON event_source_matches (document_id, proposed_at);

CREATE TABLE event_source_match_reviews (
    review_id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES event_source_matches (match_id),
    decision TEXT NOT NULL
        CHECK (decision IN ('LINKED', 'REJECTED', 'UNRESOLVED')),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX event_source_match_reviews_history
    ON event_source_match_reviews (match_id, reviewed_at, review_id);

CREATE TABLE event_timeline_entries (
    entry_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    event_time TEXT,
    event_time_rationale TEXT NOT NULL CHECK (length(trim(event_time_rationale)) > 0),
    updated_by TEXT NOT NULL CHECK (length(trim(updated_by)) > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (entry_id, revision),
    FOREIGN KEY (event_id, document_id)
        REFERENCES event_source_matches (event_id, document_id)
);

CREATE INDEX event_timeline_entries_event
    ON event_timeline_entries (event_id, entry_id, revision);

CREATE TRIGGER event_timeline_requires_confirmed_match
BEFORE INSERT ON event_timeline_entries
WHEN COALESCE((
    SELECT review.decision
    FROM event_source_matches AS match
    LEFT JOIN event_source_match_reviews AS review
        ON review.review_id = (
            SELECT latest.review_id
            FROM event_source_match_reviews AS latest
            WHERE latest.match_id = match.match_id
            ORDER BY latest.rowid DESC
            LIMIT 1
        )
    WHERE match.event_id = NEW.event_id
      AND match.document_id = NEW.document_id
), 'PENDING') != 'LINKED'
BEGIN
    SELECT RAISE(ABORT, 'timeline entries require a confirmed event/source match');
END;

CREATE TRIGGER event_source_matches_no_update
BEFORE UPDATE ON event_source_matches
BEGIN
    SELECT RAISE(ABORT, 'event source match proposals are immutable');
END;

CREATE TRIGGER event_source_matches_no_delete
BEFORE DELETE ON event_source_matches
BEGIN
    SELECT RAISE(ABORT, 'event source match proposals are immutable');
END;

CREATE TRIGGER event_source_match_reviews_no_update
BEFORE UPDATE ON event_source_match_reviews
BEGIN
    SELECT RAISE(ABORT, 'event source match reviews are immutable');
END;

CREATE TRIGGER event_source_match_reviews_no_delete
BEFORE DELETE ON event_source_match_reviews
BEGIN
    SELECT RAISE(ABORT, 'event source match reviews are immutable');
END;

CREATE TRIGGER event_timeline_entries_identity_guard
BEFORE INSERT ON event_timeline_entries
WHEN NEW.revision != COALESCE((
    SELECT MAX(previous.revision) + 1
    FROM event_timeline_entries AS previous
    WHERE previous.entry_id = NEW.entry_id
), 1)
OR EXISTS (
    SELECT 1 FROM event_timeline_entries AS previous
    WHERE previous.entry_id = NEW.entry_id
      AND (previous.event_id != NEW.event_id
          OR previous.document_id != NEW.document_id)
)
OR (NEW.revision = 1 AND EXISTS (
    SELECT 1 FROM event_timeline_entries AS existing
    WHERE existing.event_id = NEW.event_id
      AND existing.document_id = NEW.document_id
))
BEGIN
    SELECT RAISE(ABORT, 'timeline entries must be revised sequentially and consistently');
END;

CREATE TRIGGER event_timeline_entries_no_update
BEFORE UPDATE ON event_timeline_entries
BEGIN
    SELECT RAISE(ABORT, 'event timeline revisions are immutable');
END;

CREATE TRIGGER event_timeline_entries_no_delete
BEFORE DELETE ON event_timeline_entries
BEGIN
    SELECT RAISE(ABORT, 'event timeline revisions are immutable');
END;
