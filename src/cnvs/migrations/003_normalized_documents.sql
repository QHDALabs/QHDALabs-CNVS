CREATE TABLE normalized_documents (
    document_id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES collection_attempts (collection_id),
    source_id TEXT NOT NULL,
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    origin_identifier TEXT NOT NULL,
    original_title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    original_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    original_url TEXT,
    normalized_url TEXT,
    publisher_original TEXT NOT NULL,
    publisher_normalized TEXT NOT NULL,
    language TEXT NOT NULL,
    language_source TEXT NOT NULL
        CHECK (language_source IN ('DOCUMENT_DECLARED', 'REGISTRY', 'UNKNOWN')),
    language_review_status TEXT NOT NULL
        CHECK (language_review_status IN ('RECORDED', 'REVIEW_REQUIRED', 'UNKNOWN')),
    registry_language TEXT,
    declared_language TEXT,
    original_published_at TEXT,
    normalized_published_at TEXT,
    publication_timezone_known INTEGER NOT NULL
        CHECK (publication_timezone_known IN (0, 1)),
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64),
    normalized_text_sha256 TEXT NOT NULL
        CHECK (length(normalized_text_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (collection_id, item_index),
    CHECK (
        (language = 'unknown' AND language_source = 'UNKNOWN'
            AND language_review_status IN ('UNKNOWN', 'REVIEW_REQUIRED'))
        OR (language != 'unknown' AND language_source != 'UNKNOWN'
            AND language_review_status != 'UNKNOWN')
    ),
    CHECK (
        (normalized_published_at IS NULL AND publication_timezone_known = 0)
        OR normalized_published_at IS NOT NULL
    )
);

CREATE INDEX normalized_documents_source
    ON normalized_documents (source_id, created_at);
CREATE INDEX normalized_documents_text_hash
    ON normalized_documents (normalized_text_sha256);
CREATE INDEX normalized_documents_url
    ON normalized_documents (normalized_url);

CREATE TABLE translations (
    translation_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES normalized_documents (document_id),
    source_text_sha256 TEXT NOT NULL CHECK (length(source_text_sha256) = 64),
    source_language TEXT NOT NULL,
    target_language TEXT NOT NULL,
    translated_text TEXT NOT NULL CHECK (length(trim(translated_text)) > 0),
    translation_method TEXT NOT NULL CHECK (length(trim(translation_method)) > 0),
    translator TEXT NOT NULL CHECK (length(trim(translator)) > 0),
    translated_at TEXT NOT NULL,
    CHECK (source_language != target_language)
);

CREATE INDEX translations_document
    ON translations (document_id, translated_at);

CREATE TABLE duplicate_relationships (
    relationship_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES normalized_documents (document_id),
    related_document_id TEXT NOT NULL REFERENCES normalized_documents (document_id),
    relationship_type TEXT NOT NULL
        CHECK (relationship_type IN ('EXACT_TEXT_MATCH', 'POSSIBLE_SYNDICATION')),
    similarity REAL NOT NULL CHECK (similarity >= 0.0 AND similarity <= 1.0),
    created_at TEXT NOT NULL,
    CHECK (document_id < related_document_id),
    UNIQUE (document_id, related_document_id, relationship_type)
);

CREATE INDEX duplicate_relationships_documents
    ON duplicate_relationships (document_id, related_document_id);

CREATE TABLE duplicate_reviews (
    review_id TEXT PRIMARY KEY,
    relationship_id TEXT NOT NULL
        REFERENCES duplicate_relationships (relationship_id),
    decision TEXT NOT NULL CHECK (
        decision IN (
            'CONFIRMED_DEPENDENT',
            'INDEPENDENT_EVIDENCE_DOCUMENTED',
            'REJECTED',
            'UNRESOLVED'
        )
    ),
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0)
);

CREATE INDEX duplicate_reviews_history
    ON duplicate_reviews (relationship_id, reviewed_at, review_id);

CREATE TRIGGER normalized_documents_no_update
BEFORE UPDATE ON normalized_documents
BEGIN
    SELECT RAISE(ABORT, 'normalized documents are immutable');
END;

CREATE TRIGGER normalized_documents_no_delete
BEFORE DELETE ON normalized_documents
BEGIN
    SELECT RAISE(ABORT, 'normalized documents are immutable');
END;

CREATE TRIGGER translations_no_update
BEFORE UPDATE ON translations
BEGIN
    SELECT RAISE(ABORT, 'translation records are immutable');
END;

CREATE TRIGGER translations_no_delete
BEFORE DELETE ON translations
BEGIN
    SELECT RAISE(ABORT, 'translation records are immutable');
END;

CREATE TRIGGER duplicate_relationships_no_update
BEFORE UPDATE ON duplicate_relationships
BEGIN
    SELECT RAISE(ABORT, 'duplicate relationships are immutable');
END;

CREATE TRIGGER duplicate_relationships_no_delete
BEFORE DELETE ON duplicate_relationships
BEGIN
    SELECT RAISE(ABORT, 'duplicate relationships are immutable');
END;

CREATE TRIGGER duplicate_reviews_no_update
BEFORE UPDATE ON duplicate_reviews
BEGIN
    SELECT RAISE(ABORT, 'duplicate review history is immutable');
END;

CREATE TRIGGER duplicate_reviews_no_delete
BEFORE DELETE ON duplicate_reviews
BEGIN
    SELECT RAISE(ABORT, 'duplicate review history is immutable');
END;
