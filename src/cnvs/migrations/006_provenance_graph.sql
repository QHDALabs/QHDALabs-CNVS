CREATE TABLE provenance_links (
    link_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES normalized_documents (document_id),
    upstream_document_id TEXT NOT NULL REFERENCES normalized_documents (document_id),
    relationship_type TEXT NOT NULL CHECK (
        relationship_type IN ('CITES', 'QUOTES', 'SYNDICATED', 'DERIVED_FROM')
    ),
    proposed_by TEXT NOT NULL CHECK (length(trim(proposed_by)) > 0),
    proposed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    CHECK (document_id != upstream_document_id)
);

CREATE INDEX provenance_links_upstream
    ON provenance_links (upstream_document_id, document_id);

CREATE TABLE provenance_link_reviews (
    review_id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES provenance_links (link_id),
    decision TEXT NOT NULL CHECK (
        decision IN ('CONFIRMED', 'REJECTED', 'UNRESOLVED')
    ),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX provenance_link_reviews_history
    ON provenance_link_reviews (link_id, reviewed_at, review_id);

CREATE TRIGGER provenance_link_reviews_prevent_cycles
BEFORE INSERT ON provenance_link_reviews
WHEN NEW.decision = 'CONFIRMED'
AND EXISTS (
    WITH RECURSIVE upstream(document_id) AS (
        SELECT link.upstream_document_id
        FROM provenance_links AS link
        WHERE link.link_id = NEW.link_id
        UNION
        SELECT link.upstream_document_id
        FROM provenance_links AS link
        JOIN upstream ON link.document_id = upstream.document_id
        JOIN provenance_link_reviews AS review
            ON review.review_id = (
                SELECT latest.review_id
                FROM provenance_link_reviews AS latest
                WHERE latest.link_id = link.link_id
                ORDER BY latest.rowid DESC
                LIMIT 1
            )
        WHERE review.decision = 'CONFIRMED'
    )
    SELECT 1
    FROM upstream
    WHERE document_id = (
        SELECT document_id FROM provenance_links WHERE link_id = NEW.link_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'confirmed provenance links must not create a cycle');
END;

CREATE TABLE provenance_origin_assessments (
    assessment_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES normalized_documents (document_id),
    origin_status TEXT NOT NULL
        CHECK (origin_status IN ('IDENTIFIED', 'UNCERTAIN', 'UNKNOWN')),
    earliest_origin_document_id TEXT REFERENCES normalized_documents (document_id),
    assessed_by TEXT NOT NULL CHECK (length(trim(assessed_by)) > 0),
    assessed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    CHECK (document_id != earliest_origin_document_id),
    CHECK (
        (origin_status = 'IDENTIFIED' AND earliest_origin_document_id IS NOT NULL)
        OR (origin_status = 'UNCERTAIN')
        OR (origin_status = 'UNKNOWN' AND earliest_origin_document_id IS NULL)
    )
);

CREATE INDEX provenance_origin_assessments_document
    ON provenance_origin_assessments (document_id, assessed_at, assessment_id);

CREATE TABLE provenance_origin_support (
    assessment_id TEXT NOT NULL
        REFERENCES provenance_origin_assessments (assessment_id),
    link_id TEXT NOT NULL REFERENCES provenance_links (link_id),
    PRIMARY KEY (assessment_id, link_id)
);

CREATE TRIGGER provenance_origin_support_requires_confirmed_link
BEFORE INSERT ON provenance_origin_support
WHEN COALESCE((
    SELECT review.decision
    FROM provenance_link_reviews AS review
    WHERE review.link_id = NEW.link_id
    ORDER BY review.rowid DESC
    LIMIT 1
), 'PENDING') != 'CONFIRMED'
BEGIN
    SELECT RAISE(ABORT, 'origin support must reference a confirmed provenance link');
END;

CREATE TABLE independence_assignments (
    assignment_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL CHECK (length(trim(group_id)) > 0),
    member_type TEXT NOT NULL CHECK (
        member_type IN ('DOCUMENT', 'SOURCE', 'EVIDENCE')
    ),
    member_id TEXT NOT NULL CHECK (length(trim(member_id)) > 0),
    proposed_by TEXT NOT NULL CHECK (length(trim(proposed_by)) > 0),
    proposed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX independence_assignments_member
    ON independence_assignments (member_type, member_id, proposed_at);
CREATE INDEX independence_assignments_group
    ON independence_assignments (group_id, proposed_at);

CREATE TRIGGER independence_assignments_require_known_member
BEFORE INSERT ON independence_assignments
WHEN
    (NEW.member_type = 'DOCUMENT' AND NOT EXISTS (
        SELECT 1 FROM normalized_documents WHERE document_id = NEW.member_id
    ))
    OR (NEW.member_type = 'SOURCE' AND NOT (
        EXISTS (
            SELECT 1 FROM normalized_documents WHERE source_id = NEW.member_id
        )
        OR EXISTS (
            SELECT 1 FROM collection_attempts WHERE source_id = NEW.member_id
        )
        OR EXISTS (
            SELECT 1 FROM canonical_records
            WHERE record_type = 'source' AND record_id = NEW.member_id
        )
    ))
    OR (NEW.member_type = 'EVIDENCE' AND NOT EXISTS (
        SELECT 1 FROM canonical_records
        WHERE record_type = 'evidence' AND record_id = NEW.member_id
    ))
BEGIN
    SELECT RAISE(ABORT, 'independence assignment member was not found');
END;

CREATE TABLE independence_assignment_reviews (
    review_id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL
        REFERENCES independence_assignments (assignment_id),
    decision TEXT NOT NULL
        CHECK (decision IN ('ACCEPTED', 'REJECTED', 'UNRESOLVED')),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX independence_assignment_reviews_history
    ON independence_assignment_reviews (assignment_id, reviewed_at, review_id);

CREATE TABLE independence_review_support (
    review_id TEXT NOT NULL
        REFERENCES independence_assignment_reviews (review_id),
    link_id TEXT NOT NULL REFERENCES provenance_links (link_id),
    PRIMARY KEY (review_id, link_id)
);

CREATE TRIGGER independence_assignment_reviews_unique_active_group
BEFORE INSERT ON independence_assignment_reviews
WHEN NEW.decision = 'ACCEPTED'
AND EXISTS (
    SELECT 1
    FROM independence_assignments AS current
    JOIN independence_assignment_reviews AS review
        ON review.review_id = (
            SELECT latest.review_id
            FROM independence_assignment_reviews AS latest
            WHERE latest.assignment_id = current.assignment_id
            ORDER BY latest.rowid DESC
            LIMIT 1
        )
    JOIN independence_assignments AS proposed
        ON proposed.assignment_id = NEW.assignment_id
    WHERE current.member_type = proposed.member_type
      AND current.member_id = proposed.member_id
      AND current.assignment_id != proposed.assignment_id
      AND review.decision = 'ACCEPTED'
)
BEGIN
    SELECT RAISE(ABORT, 'independence member already has an accepted group');
END;

CREATE TRIGGER independence_assignment_reviews_syndication_group_guard
BEFORE INSERT ON independence_assignment_reviews
WHEN NEW.decision = 'ACCEPTED'
AND EXISTS (
    WITH RECURSIVE syndication_component(document_id) AS (
        SELECT document.document_id
        FROM normalized_documents AS document
        JOIN independence_assignments AS proposed
            ON proposed.assignment_id = NEW.assignment_id
        WHERE
            (proposed.member_type = 'DOCUMENT'
             AND document.document_id = proposed.member_id)
            OR (proposed.member_type = 'SOURCE'
                AND document.source_id = proposed.member_id)
            OR (proposed.member_type = 'EVIDENCE'
                AND document.source_id = (
                    SELECT json_extract(evidence.payload_json, '$.source_id')
                    FROM canonical_records AS evidence
                    WHERE evidence.record_type = 'evidence'
                      AND evidence.record_id = proposed.member_id
                ))
        UNION
        SELECT CASE
            WHEN link.document_id = syndication_component.document_id
                THEN link.upstream_document_id
            ELSE link.document_id
        END
        FROM provenance_links AS link
        JOIN syndication_component
            ON link.document_id = syndication_component.document_id
            OR link.upstream_document_id = syndication_component.document_id
        JOIN provenance_link_reviews AS link_review
            ON link_review.review_id = (
                SELECT latest.review_id
                FROM provenance_link_reviews AS latest
                WHERE latest.link_id = link.link_id
                ORDER BY latest.rowid DESC
                LIMIT 1
            )
        WHERE link.relationship_type = 'SYNDICATED'
          AND link_review.decision = 'CONFIRMED'
    )
    SELECT 1
    FROM syndication_component AS component
    JOIN independence_assignments AS existing
        ON (
            (existing.member_type = 'DOCUMENT'
             AND existing.member_id = component.document_id)
            OR (existing.member_type = 'SOURCE'
                AND EXISTS (
                    SELECT 1 FROM normalized_documents AS document
                    WHERE document.source_id = existing.member_id
                      AND document.document_id = component.document_id
                ))
            OR (existing.member_type = 'EVIDENCE'
                AND EXISTS (
                    SELECT 1
                    FROM canonical_records AS evidence
                    JOIN normalized_documents AS document
                        ON document.source_id = json_extract(
                            evidence.payload_json, '$.source_id'
                        )
                    WHERE evidence.record_type = 'evidence'
                      AND evidence.record_id = existing.member_id
                      AND document.document_id = component.document_id
                ))
        )
    JOIN independence_assignment_reviews AS existing_review
        ON existing_review.review_id = (
            SELECT latest.review_id
            FROM independence_assignment_reviews AS latest
            WHERE latest.assignment_id = existing.assignment_id
            ORDER BY latest.rowid DESC
            LIMIT 1
        )
    JOIN independence_assignments AS proposed
        ON proposed.assignment_id = NEW.assignment_id
    WHERE existing.assignment_id != proposed.assignment_id
      AND existing.rowid = (
          SELECT MAX(current.rowid)
          FROM independence_assignments AS current
          WHERE current.member_type = existing.member_type
            AND current.member_id = existing.member_id
      )
      AND existing_review.decision = 'ACCEPTED'
      AND existing.group_id != proposed.group_id
)
BEGIN
    SELECT RAISE(ABORT, 'confirmed syndicated documents must share an independence group');
END;

CREATE TRIGGER provenance_link_reviews_syndication_group_guard
BEFORE INSERT ON provenance_link_reviews
WHEN NEW.decision = 'CONFIRMED'
AND EXISTS (
    SELECT 1
    FROM provenance_links AS proposed_link
    WHERE proposed_link.link_id = NEW.link_id
      AND proposed_link.relationship_type = 'SYNDICATED'
      AND EXISTS (
          WITH RECURSIVE syndication_component(document_id) AS (
              SELECT proposed_link.document_id
              UNION
              SELECT proposed_link.upstream_document_id
              UNION
              SELECT CASE
                  WHEN link.document_id = syndication_component.document_id
                      THEN link.upstream_document_id
                  ELSE link.document_id
              END
              FROM provenance_links AS link
              JOIN syndication_component
                  ON link.document_id = syndication_component.document_id
                  OR link.upstream_document_id = syndication_component.document_id
              JOIN provenance_link_reviews AS link_review
                  ON link_review.review_id = (
                      SELECT latest.review_id
                      FROM provenance_link_reviews AS latest
                      WHERE latest.link_id = link.link_id
                      ORDER BY latest.rowid DESC
                      LIMIT 1
                  )
              WHERE link.relationship_type = 'SYNDICATED'
                AND link_review.decision = 'CONFIRMED'
          )
          SELECT COUNT(DISTINCT assignment.group_id)
          FROM independence_assignments AS assignment
          JOIN independence_assignment_reviews AS assignment_review
              ON assignment_review.review_id = (
                  SELECT latest.review_id
                  FROM independence_assignment_reviews AS latest
                  WHERE latest.assignment_id = assignment.assignment_id
                  ORDER BY latest.rowid DESC
                  LIMIT 1
              )
          WHERE assignment_review.decision = 'ACCEPTED'
            AND assignment.rowid = (
                SELECT MAX(current.rowid)
                FROM independence_assignments AS current
                WHERE current.member_type = assignment.member_type
                  AND current.member_id = assignment.member_id
            )
            AND EXISTS (
                SELECT 1
                FROM normalized_documents AS document
                JOIN syndication_component AS component
                    ON component.document_id = document.document_id
                WHERE
                    (assignment.member_type = 'DOCUMENT'
                     AND assignment.member_id = document.document_id)
                    OR (assignment.member_type = 'SOURCE'
                        AND assignment.member_id = document.source_id)
                    OR (assignment.member_type = 'EVIDENCE'
                        AND assignment.member_id IN (
                            SELECT evidence.record_id
                            FROM canonical_records AS evidence
                            WHERE evidence.record_type = 'evidence'
                              AND json_extract(
                                  evidence.payload_json, '$.source_id'
                              ) = document.source_id
                        ))
            )
          HAVING COUNT(DISTINCT assignment.group_id) > 1
      )
)
BEGIN
    SELECT RAISE(ABORT, 'syndicated documents cannot have different independence groups');
END;

CREATE TRIGGER independence_review_support_requires_confirmed_link
BEFORE INSERT ON independence_review_support
WHEN COALESCE((
    SELECT review.decision
    FROM provenance_link_reviews AS review
    WHERE review.link_id = NEW.link_id
    ORDER BY review.rowid DESC
    LIMIT 1
), 'PENDING') != 'CONFIRMED'
BEGIN
    SELECT RAISE(ABORT, 'independence support must reference a confirmed provenance link');
END;

CREATE TRIGGER independence_review_support_must_involve_member
BEFORE INSERT ON independence_review_support
WHEN NOT EXISTS (
    SELECT 1
    FROM independence_assignment_reviews AS review
    JOIN independence_assignments AS assignment
        ON assignment.assignment_id = review.assignment_id
    JOIN provenance_links AS link ON link.link_id = NEW.link_id
    WHERE review.review_id = NEW.review_id
      AND (
          (
              assignment.member_type = 'DOCUMENT'
              AND (
                  link.document_id = assignment.member_id
                  OR link.upstream_document_id = assignment.member_id
              )
          )
          OR (
              assignment.member_type = 'SOURCE'
              AND EXISTS (
                  SELECT 1 FROM normalized_documents AS document
                  WHERE document.source_id = assignment.member_id
                    AND document.document_id IN (
                        link.document_id, link.upstream_document_id
                    )
              )
          )
          OR (
              assignment.member_type = 'EVIDENCE'
              AND EXISTS (
                  SELECT 1
                  FROM canonical_records AS evidence
                  JOIN normalized_documents AS document
                    ON document.source_id = json_extract(
                        evidence.payload_json, '$.source_id'
                    )
                  WHERE evidence.record_type = 'evidence'
                    AND evidence.record_id = assignment.member_id
                    AND document.document_id IN (
                        link.document_id, link.upstream_document_id
                    )
              )
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'independence support must involve the assigned member');
END;

CREATE TRIGGER provenance_links_no_update
BEFORE UPDATE ON provenance_links
BEGIN
    SELECT RAISE(ABORT, 'provenance links are immutable');
END;

CREATE TRIGGER provenance_links_no_delete
BEFORE DELETE ON provenance_links
BEGIN
    SELECT RAISE(ABORT, 'provenance links are immutable');
END;

CREATE TRIGGER provenance_link_reviews_no_update
BEFORE UPDATE ON provenance_link_reviews
BEGIN
    SELECT RAISE(ABORT, 'provenance link reviews are immutable');
END;

CREATE TRIGGER provenance_link_reviews_no_delete
BEFORE DELETE ON provenance_link_reviews
BEGIN
    SELECT RAISE(ABORT, 'provenance link reviews are immutable');
END;

CREATE TRIGGER provenance_origin_assessments_no_update
BEFORE UPDATE ON provenance_origin_assessments
BEGIN
    SELECT RAISE(ABORT, 'provenance origin assessments are immutable');
END;

CREATE TRIGGER provenance_origin_assessments_no_delete
BEFORE DELETE ON provenance_origin_assessments
BEGIN
    SELECT RAISE(ABORT, 'provenance origin assessments are immutable');
END;

CREATE TRIGGER provenance_origin_support_no_update
BEFORE UPDATE ON provenance_origin_support
BEGIN
    SELECT RAISE(ABORT, 'provenance origin support is immutable');
END;

CREATE TRIGGER provenance_origin_support_no_delete
BEFORE DELETE ON provenance_origin_support
BEGIN
    SELECT RAISE(ABORT, 'provenance origin support is immutable');
END;

CREATE TRIGGER independence_assignments_no_update
BEFORE UPDATE ON independence_assignments
BEGIN
    SELECT RAISE(ABORT, 'independence assignments are immutable');
END;

CREATE TRIGGER independence_assignments_no_delete
BEFORE DELETE ON independence_assignments
BEGIN
    SELECT RAISE(ABORT, 'independence assignments are immutable');
END;

CREATE TRIGGER independence_assignment_reviews_no_update
BEFORE UPDATE ON independence_assignment_reviews
BEGIN
    SELECT RAISE(ABORT, 'independence assignment reviews are immutable');
END;

CREATE TRIGGER independence_assignment_reviews_no_delete
BEFORE DELETE ON independence_assignment_reviews
BEGIN
    SELECT RAISE(ABORT, 'independence assignment reviews are immutable');
END;

CREATE TRIGGER independence_review_support_no_update
BEFORE UPDATE ON independence_review_support
BEGIN
    SELECT RAISE(ABORT, 'independence review support is immutable');
END;

CREATE TRIGGER independence_review_support_no_delete
BEFORE DELETE ON independence_review_support
BEGIN
    SELECT RAISE(ABORT, 'independence review support is immutable');
END;
