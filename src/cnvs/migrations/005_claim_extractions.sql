CREATE TABLE claim_extractions (
    candidate_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_text_sha256 TEXT NOT NULL CHECK (length(source_text_sha256) = 64),
    span_start INTEGER NOT NULL CHECK (span_start >= 0),
    span_end INTEGER NOT NULL CHECK (span_end > span_start),
    span_text TEXT NOT NULL CHECK (length(trim(span_text)) > 0),
    subject TEXT NOT NULL CHECK (length(trim(subject)) > 0),
    predicate TEXT NOT NULL CHECK (length(trim(predicate)) > 0),
    object TEXT NOT NULL CHECK (length(trim(object)) > 0),
    claim_type TEXT NOT NULL CHECK (length(trim(claim_type)) > 0),
    attribution TEXT,
    modality TEXT NOT NULL CHECK (length(trim(modality)) > 0),
    extraction_method TEXT NOT NULL CHECK (length(trim(extraction_method)) > 0),
    extractor TEXT NOT NULL CHECK (length(trim(extractor)) > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id, document_id)
        REFERENCES event_source_matches (event_id, document_id)
);

CREATE INDEX claim_extractions_event
    ON claim_extractions (event_id, created_at);
CREATE INDEX claim_extractions_document
    ON claim_extractions (document_id, span_start);

CREATE TRIGGER claim_extractions_require_linked_source
BEFORE INSERT ON claim_extractions
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
    SELECT RAISE(ABORT, 'claim extractions require a confirmed event/source match');
END;

CREATE TRIGGER claim_extractions_require_source_provenance
BEFORE INSERT ON claim_extractions
WHEN NOT EXISTS (
    SELECT 1
    FROM normalized_documents AS document
    WHERE document.document_id = NEW.document_id
      AND document.source_id = NEW.source_id
      AND document.text_sha256 = NEW.source_text_sha256
      AND substr(
          document.original_text,
          NEW.span_start + 1,
          NEW.span_end - NEW.span_start
      ) = NEW.span_text
)
BEGIN
    SELECT RAISE(ABORT, 'claim extraction span or source provenance is invalid');
END;

CREATE TABLE claim_extraction_reviews (
    review_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES claim_extractions (candidate_id),
    decision TEXT NOT NULL
        CHECK (decision IN ('ACCEPTED', 'REJECTED', 'CORRECTED', 'UNRESOLVED')),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    correction_json TEXT CHECK (
        correction_json IS NULL OR json_valid(correction_json)
    ),
    CHECK (
        (decision = 'CORRECTED' AND correction_json IS NOT NULL)
        OR (decision != 'CORRECTED' AND correction_json IS NULL)
    )
);

CREATE INDEX claim_extraction_reviews_history
    ON claim_extraction_reviews (candidate_id, reviewed_at, review_id);

CREATE TRIGGER claim_extraction_acceptance_requires_link
BEFORE INSERT ON claim_extraction_reviews
WHEN NEW.decision = 'ACCEPTED'
AND COALESCE((
    SELECT review.decision
    FROM claim_extractions AS candidate
    JOIN event_source_matches AS match
        ON match.event_id = candidate.event_id
        AND match.document_id = candidate.document_id
    LEFT JOIN event_source_match_reviews AS review
        ON review.review_id = (
            SELECT latest.review_id
            FROM event_source_match_reviews AS latest
            WHERE latest.match_id = match.match_id
            ORDER BY latest.rowid DESC
            LIMIT 1
        )
    WHERE candidate.candidate_id = NEW.candidate_id
), 'PENDING') != 'LINKED'
BEGIN
    SELECT RAISE(ABORT, 'claim acceptance requires a confirmed event/source match');
END;

CREATE TRIGGER claim_extractions_no_update
BEFORE UPDATE ON claim_extractions
BEGIN
    SELECT RAISE(ABORT, 'claim extraction candidates are immutable');
END;

CREATE TRIGGER claim_extractions_no_delete
BEFORE DELETE ON claim_extractions
BEGIN
    SELECT RAISE(ABORT, 'claim extraction candidates are immutable');
END;

CREATE TRIGGER claim_extraction_reviews_no_update
BEFORE UPDATE ON claim_extraction_reviews
BEGIN
    SELECT RAISE(ABORT, 'claim extraction reviews are immutable');
END;

CREATE TRIGGER claim_extraction_reviews_no_delete
BEFORE DELETE ON claim_extraction_reviews
BEGIN
    SELECT RAISE(ABORT, 'claim extraction reviews are immutable');
END;
