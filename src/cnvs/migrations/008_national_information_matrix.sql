CREATE TABLE event_country_coverage_plans (
    plan_id TEXT PRIMARY KEY,
    event_record_type TEXT NOT NULL DEFAULT 'event'
        CHECK (event_record_type = 'event'),
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    countries_json TEXT NOT NULL
        CHECK (json_valid(countries_json) AND json_type(countries_json) = 'array'),
    outside_catalog_json TEXT NOT NULL
        CHECK (json_valid(outside_catalog_json)
            AND json_type(outside_catalog_json) = 'array'),
    proposed_by TEXT NOT NULL CHECK (length(trim(proposed_by)) > 0),
    proposed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    UNIQUE (event_id, revision),
    FOREIGN KEY (event_record_type, event_id)
        REFERENCES canonical_records (record_type, record_id)
);

CREATE INDEX event_country_coverage_plans_history
    ON event_country_coverage_plans (event_id, revision);

CREATE TABLE event_country_coverage_reviews (
    review_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES event_country_coverage_plans (plan_id),
    decision TEXT NOT NULL
        CHECK (decision IN ('ACCEPTED', 'REJECTED', 'UNRESOLVED', 'SUPERSEDED')),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX event_country_coverage_reviews_history
    ON event_country_coverage_reviews (plan_id, reviewed_at, review_id);

CREATE TRIGGER event_country_coverage_reviews_single_active_plan
BEFORE INSERT ON event_country_coverage_reviews
WHEN NEW.decision = 'ACCEPTED'
AND EXISTS (
    SELECT 1
    FROM event_country_coverage_plans AS target
    JOIN event_country_coverage_plans AS other
      ON other.event_id = target.event_id
     AND other.plan_id != target.plan_id
    JOIN event_country_coverage_reviews AS other_review
      ON other_review.review_id = (
          SELECT latest.review_id
          FROM event_country_coverage_reviews AS latest
          WHERE latest.plan_id = other.plan_id
          ORDER BY latest.rowid DESC
          LIMIT 1
      )
    WHERE target.plan_id = NEW.plan_id
      AND other_review.decision = 'ACCEPTED'
)
BEGIN
    SELECT RAISE(ABORT, 'an event can have only one accepted country coverage plan');
END;

CREATE TABLE country_matrix_assessments (
    assessment_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES event_country_coverage_plans (plan_id),
    event_id TEXT NOT NULL,
    event_record_type TEXT NOT NULL DEFAULT 'event'
        CHECK (event_record_type = 'event'),
    country_code TEXT NOT NULL CHECK (length(country_code) = 2),
    dominant_frame TEXT NOT NULL CHECK (length(trim(dominant_frame)) > 0),
    attribution_summary TEXT NOT NULL
        CHECK (length(trim(attribution_summary)) > 0),
    occurrence_confidence TEXT NOT NULL
        CHECK (occurrence_confidence IN ('LOW', 'MEDIUM', 'HIGH', 'UNKNOWN')),
    method_confidence TEXT NOT NULL
        CHECK (method_confidence IN ('LOW', 'MEDIUM', 'HIGH', 'UNKNOWN')),
    attribution_confidence TEXT NOT NULL
        CHECK (attribution_confidence IN ('LOW', 'MEDIUM', 'HIGH', 'UNKNOWN')),
    omissions TEXT NOT NULL CHECK (length(trim(omissions)) > 0),
    contradictions TEXT NOT NULL CHECK (length(trim(contradictions)) > 0),
    supporting_source_ids_json TEXT NOT NULL
        CHECK (json_valid(supporting_source_ids_json)
            AND json_type(supporting_source_ids_json) = 'array'),
    supporting_claim_ids_json TEXT NOT NULL
        CHECK (json_valid(supporting_claim_ids_json)
            AND json_type(supporting_claim_ids_json) = 'array'),
    supporting_evidence_ids_json TEXT NOT NULL
        CHECK (json_valid(supporting_evidence_ids_json)
            AND json_type(supporting_evidence_ids_json) = 'array'),
    analyst TEXT NOT NULL CHECK (length(trim(analyst)) > 0),
    created_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    FOREIGN KEY (event_record_type, event_id)
        REFERENCES canonical_records (record_type, record_id),
    UNIQUE (plan_id, country_code, assessment_id)
);

CREATE INDEX country_matrix_assessments_history
    ON country_matrix_assessments (plan_id, country_code, created_at, assessment_id);

CREATE TRIGGER country_matrix_assessments_require_accepted_plan
BEFORE INSERT ON country_matrix_assessments
WHEN NOT EXISTS (
    SELECT 1
    FROM event_country_coverage_reviews AS review
    WHERE review.plan_id = NEW.plan_id
      AND review.review_id = (
          SELECT latest.review_id
          FROM event_country_coverage_reviews AS latest
          WHERE latest.plan_id = NEW.plan_id
          ORDER BY latest.rowid DESC
          LIMIT 1
      )
      AND review.decision = 'ACCEPTED'
)
OR NOT EXISTS (
    SELECT 1
    FROM event_country_coverage_plans AS plan
    WHERE plan.plan_id = NEW.plan_id AND plan.event_id = NEW.event_id
      AND EXISTS (
          SELECT 1
          FROM json_each(plan.countries_json) AS country
          WHERE json_extract(country.value, '$.code') = NEW.country_code
      )
)
BEGIN
    SELECT RAISE(ABORT, 'matrix assessments require an accepted plan country');
END;

CREATE TABLE country_matrix_assessment_reviews (
    review_id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL
        REFERENCES country_matrix_assessments (assessment_id),
    decision TEXT NOT NULL
        CHECK (decision IN ('ACCEPTED', 'REJECTED', 'UNRESOLVED')),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX country_matrix_assessment_reviews_history
    ON country_matrix_assessment_reviews (assessment_id, reviewed_at, review_id);

CREATE TRIGGER country_matrix_assessment_reviews_single_active
BEFORE INSERT ON country_matrix_assessment_reviews
WHEN NEW.decision = 'ACCEPTED'
AND EXISTS (
    SELECT 1
    FROM country_matrix_assessments AS target
    JOIN country_matrix_assessments AS other
      ON other.plan_id = target.plan_id
     AND other.country_code = target.country_code
     AND other.assessment_id != target.assessment_id
    JOIN country_matrix_assessment_reviews AS other_review
      ON other_review.review_id = (
          SELECT latest.review_id
          FROM country_matrix_assessment_reviews AS latest
          WHERE latest.assessment_id = other.assessment_id
          ORDER BY latest.rowid DESC
          LIMIT 1
      )
    WHERE target.assessment_id = NEW.assessment_id
      AND other_review.decision = 'ACCEPTED'
)
BEGIN
    SELECT RAISE(ABORT, 'a country can have only one accepted matrix assessment per plan');
END;

CREATE TRIGGER event_country_coverage_plans_no_update
BEFORE UPDATE ON event_country_coverage_plans
BEGIN
    SELECT RAISE(ABORT, 'country coverage plans are immutable');
END;

CREATE TRIGGER event_country_coverage_plans_no_delete
BEFORE DELETE ON event_country_coverage_plans
BEGIN
    SELECT RAISE(ABORT, 'country coverage plans are immutable');
END;

CREATE TRIGGER event_country_coverage_reviews_no_update
BEFORE UPDATE ON event_country_coverage_reviews
BEGIN
    SELECT RAISE(ABORT, 'country coverage reviews are immutable');
END;

CREATE TRIGGER event_country_coverage_reviews_no_delete
BEFORE DELETE ON event_country_coverage_reviews
BEGIN
    SELECT RAISE(ABORT, 'country coverage reviews are immutable');
END;

CREATE TRIGGER country_matrix_assessments_no_update
BEFORE UPDATE ON country_matrix_assessments
BEGIN
    SELECT RAISE(ABORT, 'country matrix assessments are immutable');
END;

CREATE TRIGGER country_matrix_assessments_no_delete
BEFORE DELETE ON country_matrix_assessments
BEGIN
    SELECT RAISE(ABORT, 'country matrix assessments are immutable');
END;

CREATE TRIGGER country_matrix_assessment_reviews_no_update
BEFORE UPDATE ON country_matrix_assessment_reviews
BEGIN
    SELECT RAISE(ABORT, 'country matrix assessment reviews are immutable');
END;

CREATE TRIGGER country_matrix_assessment_reviews_no_delete
BEFORE DELETE ON country_matrix_assessment_reviews
BEGIN
    SELECT RAISE(ABORT, 'country matrix assessment reviews are immutable');
END;
