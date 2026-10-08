# CNVS Data Model

CNVS models an investigation as linked records rather than one blended
“verified story”:

```text
event → observation → evidence → source → claim → attribution → narrative → assessment
```

Each record has its own identity and provenance. A claim links to the source
that made it; evidence links to its source and an independence group. An
assessment refers to supporting and contradicting records without replacing
them.

## Canonical records

| Record | Purpose | Schema |
| --- | --- | --- |
| Event | Stable identity and observed incident scope | `schemas/event.schema.json` |
| Source | Origin, publisher and retrieval provenance | `schemas/source.schema.json` |
| Claim | Attributed proposition and current support status | `schemas/claim.schema.json` |
| Evidence | Observation, collection context and verification state | `schemas/evidence.schema.json` |
| Assessment | Versioned analysis linked to exact evidence and claim revisions | `schemas/assessment.schema.json` |

The JSON examples in `examples/` are synthetic fixtures. The schemas are initial
contracts and will evolve with explicit schema versioning.

Runtime validation uses the packaged schema copies under `src/cnvs/schemas/`;
they must remain semantically equivalent to the public contracts
under `schemas/`. Each persisted record contains an explicit `schema_version`.
Schema changes require a deliberate versioning decision and migration plan.

## Record boundaries

- **Event** describes what is being investigated, not who caused it.
- **Observation** describes what was directly seen or recorded.
- **Evidence** records an observation and its collection and verification
  context; a URL alone is not an evidence record.
- **Source** records where material came from and how it was obtained.
- **Claim** records what a source asserts. Its status does not establish truth.
- **Attribution** identifies who a source says is responsible; it is distinct
  from occurrence and method.
- **Assessment** is an analyst conclusion with supporting records, uncertainty,
  contradictions and information gaps.

Assessment snapshots additionally preserve the exact revisions of their event,
claims, evidence and sources so a previous report can be reconstructed after
records receive updates.

## Identity and time

Use stable, human-readable IDs in examples and durable opaque identifiers in a
production system. Preserve event time, publication time, collection time and
record update time separately when available. Never overwrite raw source
material to reflect later corrections; add versioned or append-only records.

## Example workflow

1. Create an event record without assuming attribution.
2. Record each source and its origin before extracting claims.
3. Store claims with a link to the source and publication time.
4. Store observations as evidence with collection time, directness and an
   independence group.
5. Link supporting and contradicting evidence to the appropriate claims.
6. Publish an assessment that distinguishes established facts from claims,
   uncertainty and unresolved gaps.

## Persistence and revisions

Stage 3 uses SQLite for the local CLI workflow. Checksummed SQL migrations are
applied automatically when the database is opened. Canonical records have a
current view and append-only revision history. Updating a record creates a new
revision; it does not rewrite earlier revision payloads.

Raw source snapshots are addressed by their SHA-256 digest. The store verifies
the digest on insertion and retrieval and rejects updates or deletes at the
database level. Snapshot immutability is a data-integrity control, not an
authorization or retention policy; administrator deletion workflows and
production backup handling remain unimplemented.

## Source registry and collection attempts

The analyst-managed `config/source_registry.yaml` records source identity,
publisher, class, country, language, access method, event relevance, review
state and collection constraints. Only enabled entries with explicit review
approval can make network requests. The checked-in entry is synthetic and
disabled; no live source is pre-approved.

Every collection execution appends a `CollectionResult` with a unique attempt
ID, registry source ID, attempt number, retry link, start/completion times, a
snapshot of the source registration and constraints, requested/final URL, HTTP
status, media type, content digest, origin identifiers, result state and error
details. Results and their linked raw snapshot references are immutable.
Retries are explicit, sequential and
allowed only for a failed latest attempt. `BLOCKED` captures access denials,
including `robots.txt` restrictions; blocked work is not silently retried.

RSS collection retains the feed snapshot only when explicitly permitted and
records item GUID/ID/link values as origin identifiers. URL collection records
the requested and final URL; document parsing and normalization run separately
after successful collection, as described below.

## Normalized documents, translations and duplicate review

Stage 5 parses retained RSS/Atom entries and retained HTML pages into immutable
`NormalizedDocument` records. Each document keeps extracted original title
and text alongside conservative NFC/whitespace-normalized forms. The raw
collection snapshot remains the byte-for-byte source representation. Original
and canonical URL, publisher, publication timestamp and timezone knowledge are
stored separately. Timestamp parsing never infers a timezone for an unzoned
value; the original string is kept and the normalized timezone flag remains
false.

Language is recorded from an item/page declaration where supported, otherwise
from the registry, or explicitly as `unknown`. Conflicts between a declared
language and the registry, and unsupported document language declarations,
are flagged `REVIEW_REQUIRED`. Stage 5 does not run a statistical language
detector or generate translations. An analyst or external tool can append a
translation record with the source text digest, source/target languages,
method, translator and timestamp; it never replaces or becomes independent
evidence from the original text.

Duplicate detection creates links, never merges or deletes source documents.
Normalized exact text hashes create `EXACT_TEXT_MATCH` candidates. Likely
syndication candidates use shared canonical URLs/non-URL origin identifiers,
or overlap in five-word shingles (Jaccard similarity at least 0.82; a similar
title can lower the text threshold to 0.65). Every relationship begins
`PENDING`: similarity is a review signal, not a factual conclusion or
independence determination. Append-only analyst reviews record whether copies
are dependent, separate evidence chains are documented, the match is rejected,
or the relationship remains unresolved. Until review, a candidate must not
be treated as independent confirmation.

Normalization requires a successful collection whose source terms permitted
retaining the source body. If archiving was disabled, text and derived content
are not retained and normalization reports that content is unavailable.

## Event matching and chronology

Stage 6 stores event records through the existing versioned canonical-record
store. `cnvs event create` validates a complete event record; subsequent
corrections create revisions, so prior event descriptions and time bounds
remain auditable.

An analyst proposes a match between an event and a normalized source document
with a rationale. Proposals start `PENDING`. A reviewer must explicitly mark a
candidate `LINKED`, `REJECTED` or `UNRESOLVED`; each review records reviewer,
timestamp and rationale as an append-only entry. The same document may remain
a candidate for more than one event until ambiguity is resolved. A rejected
or unresolved match is not included in that event's timeline. CNVS neither
automatically matches documents nor merges events.
The CLI can inspect the full proposal/review history and any prior event
revision.

Confirming a source link creates a timeline entry with no assumed occurrence
time. Analysts can record or correct that value later; each correction stores
the rationale and editor in an immutable timeline revision. Timeline output
keeps these values independent:

- **Event time**: analyst-recorded occurrence time, nullable until established.
- **Publication time**: original source timestamp and its normalized form, when
  a timezone was supplied.
- **Collection time**: completion time of the source collection attempt.

The timeline orders entries using event time when available, otherwise the
best available publication time, then collection time. It does not substitute
publication or collection timestamps for an unknown occurrence time.

## Derived claim extraction and review

Stage 7 stores claim extraction candidates separately from canonical `Claim`
records. A candidate is allowed only for an existing event and normalized
document whose event/source match has been explicitly reviewed as `LINKED`.
Each immutable candidate records its event, document and source IDs, SHA-256
of the document's original text, Python-character offsets into that text,
the exact quoted span, subject, predicate, object, claim type, attribution,
modality, extraction method, extractor and creation time. The digest and span
are checked against the retained document at insertion; invalid batches fail
atomically.

Candidate reviews are append-only and include reviewer, time and rationale.
`ACCEPTED`, `REJECTED`, `CORRECTED` and `UNRESOLVED` describe the extraction
review state, not whether a proposition is true. A correction is a complete
structured candidate payload whose span is validated against the same
source-text snapshot; it is retained in the review history without rewriting
the original candidate. A later review can supersede the displayed current
decision while preserving the full history.

The CLI imports analyst- or tool-produced JSON candidates; there is no
automatic NLP/LLM extraction. Source text is untrusted data and is not sent to
a model by this workflow. An accepted or corrected candidate is still derived
material: this stage does not promote it to a canonical claim, create
evidence, verify its truth, or determine source independence. Those records
and decisions require subsequent workflow stages.
