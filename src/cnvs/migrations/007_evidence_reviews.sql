CREATE TABLE evidence_verification_reviews (
    review_id TEXT PRIMARY KEY,
    evidence_record_type TEXT NOT NULL DEFAULT 'evidence'
        CHECK (evidence_record_type = 'evidence'),
    evidence_id TEXT NOT NULL,
    verification_status TEXT NOT NULL
        CHECK (verification_status IN ('IN_REVIEW', 'VERIFIED', 'REJECTED')),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    analyst_note TEXT,
    FOREIGN KEY (evidence_record_type, evidence_id)
        REFERENCES canonical_records (record_type, record_id)
);

CREATE INDEX evidence_verification_reviews_history
    ON evidence_verification_reviews (evidence_id, reviewed_at, review_id);

CREATE TABLE evidence_notes (
    note_id TEXT PRIMARY KEY,
    evidence_record_type TEXT NOT NULL DEFAULT 'evidence'
        CHECK (evidence_record_type = 'evidence'),
    evidence_id TEXT NOT NULL,
    analyst TEXT NOT NULL CHECK (length(trim(analyst)) > 0),
    noted_at TEXT NOT NULL,
    note TEXT NOT NULL CHECK (length(trim(note)) > 0),
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    FOREIGN KEY (evidence_record_type, evidence_id)
        REFERENCES canonical_records (record_type, record_id)
);

CREATE INDEX evidence_notes_history
    ON evidence_notes (evidence_id, noted_at, note_id);

CREATE TABLE claim_evidence_links (
    link_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_record_type TEXT NOT NULL DEFAULT 'event'
        CHECK (event_record_type = 'event'),
    claim_record_type TEXT NOT NULL DEFAULT 'claim'
        CHECK (claim_record_type = 'claim'),
    claim_id TEXT NOT NULL,
    evidence_record_type TEXT NOT NULL DEFAULT 'evidence'
        CHECK (evidence_record_type = 'evidence'),
    evidence_id TEXT NOT NULL,
    relationship TEXT NOT NULL
        CHECK (relationship IN ('SUPPORTS', 'CONTRADICTS', 'NOT_DIRECTLY_RELEVANT')),
    proposed_by TEXT NOT NULL CHECK (length(trim(proposed_by)) > 0),
    proposed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    FOREIGN KEY (event_record_type, event_id)
        REFERENCES canonical_records (record_type, record_id),
    FOREIGN KEY (claim_record_type, claim_id)
        REFERENCES canonical_records (record_type, record_id),
    FOREIGN KEY (evidence_record_type, evidence_id)
        REFERENCES canonical_records (record_type, record_id)
);

CREATE INDEX claim_evidence_links_claim
    ON claim_evidence_links (claim_id, proposed_at, link_id);
CREATE INDEX claim_evidence_links_evidence
    ON claim_evidence_links (evidence_id, proposed_at, link_id);

CREATE TRIGGER claim_evidence_links_same_event
BEFORE INSERT ON claim_evidence_links
WHEN json_extract((
        SELECT payload_json FROM canonical_records
        WHERE record_type = 'claim' AND record_id = NEW.claim_id
     ), '$.event_id') != NEW.event_id
  OR json_extract((
        SELECT payload_json FROM canonical_records
        WHERE record_type = 'evidence' AND record_id = NEW.evidence_id
     ), '$.event_id') != NEW.event_id
BEGIN
    SELECT RAISE(ABORT, 'claim/evidence links must belong to the same event');
END;

CREATE TABLE claim_evidence_link_reviews (
    review_id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES claim_evidence_links (link_id),
    decision TEXT NOT NULL
        CHECK (decision IN ('LINKED', 'REJECTED', 'UNRESOLVED')),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX claim_evidence_link_reviews_history
    ON claim_evidence_link_reviews (link_id, reviewed_at, review_id);

CREATE TRIGGER claim_evidence_link_reviews_no_duplicate_active_relation
BEFORE INSERT ON claim_evidence_link_reviews
WHEN NEW.decision = 'LINKED'
AND EXISTS (
    SELECT 1
    FROM claim_evidence_links AS target
    JOIN claim_evidence_links AS other
      ON other.claim_id = target.claim_id
     AND other.evidence_id = target.evidence_id
     AND other.link_id != target.link_id
    JOIN claim_evidence_link_reviews AS other_review
      ON other_review.review_id = (
          SELECT latest.review_id
          FROM claim_evidence_link_reviews AS latest
          WHERE latest.link_id = other.link_id
          ORDER BY latest.rowid DESC
          LIMIT 1
      )
    WHERE target.link_id = NEW.link_id
      AND other_review.decision = 'LINKED'
)
BEGIN
    SELECT RAISE(ABORT, 'evidence already has an active relation to this claim');
END;

CREATE TABLE evidence_gaps (
    gap_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_record_type TEXT NOT NULL DEFAULT 'event'
        CHECK (event_record_type = 'event'),
    claim_id TEXT,
    claim_record_type TEXT NOT NULL DEFAULT 'claim'
        CHECK (claim_record_type = 'claim'),
    gap_type TEXT NOT NULL CHECK (gap_type IN ('MISSING', 'INSUFFICIENT', 'CONFLICTING')),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    supporting_link_id TEXT REFERENCES claim_evidence_links (link_id),
    contradicting_link_id TEXT REFERENCES claim_evidence_links (link_id),
    CHECK (
        (gap_type = 'CONFLICTING' AND claim_id IS NOT NULL
            AND supporting_link_id IS NOT NULL AND contradicting_link_id IS NOT NULL)
        OR
        (gap_type != 'CONFLICTING'
            AND supporting_link_id IS NULL AND contradicting_link_id IS NULL)
    ),
    FOREIGN KEY (event_record_type, event_id)
        REFERENCES canonical_records (record_type, record_id),
    FOREIGN KEY (claim_record_type, claim_id)
        REFERENCES canonical_records (record_type, record_id)
);

CREATE INDEX evidence_gaps_event
    ON evidence_gaps (event_id, created_at, gap_id);
CREATE INDEX evidence_gaps_claim
    ON evidence_gaps (claim_id, created_at, gap_id);

CREATE TRIGGER evidence_gaps_validate_event_and_conflict_links
BEFORE INSERT ON evidence_gaps
WHEN (NEW.claim_id IS NOT NULL AND json_extract((
        SELECT payload_json FROM canonical_records
        WHERE record_type = 'claim' AND record_id = NEW.claim_id
     ), '$.event_id') != NEW.event_id)
  OR (NEW.gap_type = 'CONFLICTING' AND (
      NOT EXISTS (
          SELECT 1
          FROM claim_evidence_links AS link
          JOIN claim_evidence_link_reviews AS review
            ON review.review_id = (
                SELECT latest.review_id
                FROM claim_evidence_link_reviews AS latest
                WHERE latest.link_id = link.link_id
                ORDER BY latest.rowid DESC
                LIMIT 1
            )
          WHERE link.link_id = NEW.supporting_link_id
            AND link.event_id = NEW.event_id
            AND link.claim_id = NEW.claim_id
            AND link.relationship = 'SUPPORTS'
            AND review.decision = 'LINKED'
      )
      OR NOT EXISTS (
          SELECT 1
          FROM claim_evidence_links AS link
          JOIN claim_evidence_link_reviews AS review
            ON review.review_id = (
                SELECT latest.review_id
                FROM claim_evidence_link_reviews AS latest
                WHERE latest.link_id = link.link_id
                ORDER BY latest.rowid DESC
                LIMIT 1
            )
          WHERE link.link_id = NEW.contradicting_link_id
            AND link.event_id = NEW.event_id
            AND link.claim_id = NEW.claim_id
            AND link.relationship = 'CONTRADICTS'
            AND review.decision = 'LINKED'
      )
  ))
BEGIN
    SELECT RAISE(ABORT, 'evidence gaps must match their event and conflicts need linked support');
END;

CREATE TABLE evidence_gap_reviews (
    review_id TEXT PRIMARY KEY,
    gap_id TEXT NOT NULL REFERENCES evidence_gaps (gap_id),
    decision TEXT NOT NULL
        CHECK (decision IN ('RESOLVED', 'DISMISSED', 'REOPENED')),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX evidence_gap_reviews_history
    ON evidence_gap_reviews (gap_id, reviewed_at, review_id);

CREATE TRIGGER evidence_verification_reviews_no_update
BEFORE UPDATE ON evidence_verification_reviews
BEGIN
    SELECT RAISE(ABORT, 'evidence verification reviews are immutable');
END;

CREATE TRIGGER evidence_verification_reviews_no_delete
BEFORE DELETE ON evidence_verification_reviews
BEGIN
    SELECT RAISE(ABORT, 'evidence verification reviews are immutable');
END;

CREATE TRIGGER evidence_notes_no_update
BEFORE UPDATE ON evidence_notes
BEGIN
    SELECT RAISE(ABORT, 'evidence notes are immutable');
END;

CREATE TRIGGER evidence_notes_no_delete
BEFORE DELETE ON evidence_notes
BEGIN
    SELECT RAISE(ABORT, 'evidence notes are immutable');
END;

CREATE TRIGGER claim_evidence_links_no_update
BEFORE UPDATE ON claim_evidence_links
BEGIN
    SELECT RAISE(ABORT, 'claim/evidence link proposals are immutable');
END;

CREATE TRIGGER claim_evidence_links_no_delete
BEFORE DELETE ON claim_evidence_links
BEGIN
    SELECT RAISE(ABORT, 'claim/evidence link proposals are immutable');
END;

CREATE TRIGGER claim_evidence_link_reviews_no_update
BEFORE UPDATE ON claim_evidence_link_reviews
BEGIN
    SELECT RAISE(ABORT, 'claim/evidence link reviews are immutable');
END;

CREATE TRIGGER claim_evidence_link_reviews_no_delete
BEFORE DELETE ON claim_evidence_link_reviews
BEGIN
    SELECT RAISE(ABORT, 'claim/evidence link reviews are immutable');
END;

CREATE TRIGGER evidence_gaps_no_update
BEFORE UPDATE ON evidence_gaps
BEGIN
    SELECT RAISE(ABORT, 'evidence gap records are immutable');
END;

CREATE TRIGGER evidence_gaps_no_delete
BEFORE DELETE ON evidence_gaps
BEGIN
    SELECT RAISE(ABORT, 'evidence gap records are immutable');
END;

CREATE TRIGGER evidence_gap_reviews_no_update
BEFORE UPDATE ON evidence_gap_reviews
BEGIN
    SELECT RAISE(ABORT, 'evidence gap reviews are immutable');
END;

CREATE TRIGGER evidence_gap_reviews_no_delete
BEFORE DELETE ON evidence_gap_reviews
BEGIN
    SELECT RAISE(ABORT, 'evidence gap reviews are immutable');
END;
