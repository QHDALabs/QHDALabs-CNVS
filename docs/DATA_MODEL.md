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

## Provenance graph and independence groups

Stage 8 records directed relationships between normalized documents, each of
which retains its source ID. The direction is from the document being
investigated to an upstream document (`document → upstream document`), with
relationship types `CITES`, `QUOTES`, `SYNDICATED` and `DERIVED_FROM`. Thus the
graph preserves article-level lineage while exposing the source-to-source
chain. A link starts pending and stores its proposer and rationale. An
append-only review marks it `CONFIRMED`, `REJECTED` or `UNRESOLVED`; only
currently confirmed edges participate in graph traversal. Confirmed cycles
are rejected by both the API and a database trigger. Rejected or unresolved
edges remain auditable and are not traversed as established lineage.

`provenance graph DOCUMENT-ID` displays the confirmed upstream closure.
`provenance claim CANDIDATE-ID` resolves a Stage 7 extraction candidate to
its source document and displays the same graph. Links preserve both document
and source IDs so analysts can see syndicated or cited source chains without
merging records.

Origin knowledge is recorded separately as append-only analyst assessments:
`IDENTIFIED`, `UNCERTAIN` or `UNKNOWN`. An identified earliest origin must be
another normalized document reachable from the assessed document through the
provided, currently confirmed supporting links. `UNKNOWN` cannot name an
origin; `UNCERTAIN` can record a possible earliest origin while stating why
the chain remains incomplete. Each assessment retains its rationale,
analyst/time and supporting link IDs. No origin is inferred from missing
records.

Independence memberships can be proposed for a normalized `DOCUMENT`,
registered/collected `SOURCE`, or canonical `EVIDENCE` record. A proposal is
not an active group membership until a separate reviewer records `ACCEPTED`;
`REJECTED` and `UNRESOLVED` are also append-only review outcomes. A member
cannot have two simultaneously accepted groups. To reconsider an accepted
membership, append an `UNRESOLVED` review before proposing a replacement.
Review decisions store the reviewer, rationale and any confirmed provenance
links that support the grouping. Those links must involve the assigned
member. `provenance assignments --status ACCEPTED` lists only accepted
memberships; the full history remains available.

Group IDs are analyst-defined labels, not calibrated evidence counts.
Membership does not establish factual truth or source reliability, and the
software does not automatically infer independence, transfer Stage 5
duplicate decisions into this graph, merge sources, or compute confirmation
counts. A confirmed syndication link is important context; existing accepted
group assignments in its connected chain must agree. If a proposed link would
connect different accepted groups, resolve and re-review those assignments
before confirming the link. Analysts remain responsible for the rationale.
Until a dependency is resolved, represent it as `UNRESOLVED` or record origin
uncertainty rather than treating the reporting as independent.

## Evidence, claim relations and explicit gaps

Stage 9 stores evidence as a canonical, versioned `Evidence` record with an
event ID, source ID, observation, collection time, nullable event time,
directness and initial verification status. The source ID resolves to the
canonical source record and its collection/snapshot provenance when retained;
the event and source times remain distinct. `PRIMARY`, `SECONDARY` and
`UNKNOWN` describe directness, not truth or reliability.

`cnvs source record --file source.json` and `cnvs claim record --file
claim.json` store validated canonical records needed by this workflow. A
canonical claim is entered/reviewed material; this command does not accept or
promote a Stage 7 extraction candidate.

`cnvs evidence create --file evidence.json` validates and records the evidence
once; corrections must be recorded as a new canonical revision through the
storage API rather than rewriting history. `evidence list` displays its
source, event/collection times, directness and effective verification state.
Verification decisions (`IN_REVIEW`, `VERIFIED`, `REJECTED`) append a reviewer,
timestamp and rationale. The current effective verification state is the most
recent review decision, or the canonical record's initial status when there is
no review; review does not rewrite the evidence payload. Analyst notes can be
added separately with their author, timestamp and rationale, and notes added
during a verification review are likewise attributed.

Claim/evidence relationships are proposed separately from claim records and
start `PENDING`. The only relationship types are `SUPPORTS`, `CONTRADICTS` and
`NOT_DIRECTLY_RELEVANT`. A reviewer must append `LINKED`, `REJECTED` or
`UNRESOLVED` with a rationale before a relation is treated as reviewed.
Claim and evidence must belong to the same event, and a claim/evidence pair
cannot have multiple active relationships. To revise one, resolve its current
relationship before proposing another. These relation/review records retain
their own history; the claim's legacy evidence ID arrays are not a substitute
for a reviewed relation.

Evidence gaps are explicit, append-only records scoped to an event and
optionally a claim: `MISSING` means the needed evidence is absent,
`INSUFFICIENT` means the available evidence does not settle the question, and
`CONFLICTING` records incompatible reviewed evidence. A conflicting gap must
reference a `LINKED` `SUPPORTS` relation and a distinct `LINKED` `CONTRADICTS`
relation for the same claim. Gap reviews can resolve, dismiss or reopen the
gap; they preserve who decided, when and why. A gap records an information
state, not proof that a claim is false. No claim status, verification state,
source volume or independence count is inferred from these records.

`cnvs evidence links --status LINKED` inspects reviewed relations, while
`evidence link-history LINK-ID`, `evidence history EVIDENCE-ID` and
`evidence gap-history GAP-ID` expose the append-only decision history.
`evidence gaps` lists open or reviewed gaps; `evidence gap` records missing,
insufficient or conflicting evidence. These operations do not perform
automated contradiction detection or verify the truth of an observation.

## National information matrix

Stage 10 records an event-specific country/language coverage plan against the
approved country and language catalogs. A plan stores the selected country
codes, their selected subset of catalog languages, any explicitly justified
out-of-catalog countries, the analyst rationale and a SHA-256 snapshot of the
country/language configuration. Proposals and review decisions are append-only;
only an accepted plan is used for matrix cells. Accepting a replacement plan
supersedes the prior accepted plan while retaining its review history. A
changed catalog digest is shown as a warning and blocks new assessments until
coverage is proposed and reviewed against the current catalog.

`cnvs matrix plan EVENT-ID --file coverage.json --proposed-by analyst
--rationale "..."` expects a JSON object with a non-empty `countries` array.
Each entry has a catalog `code` and an optional non-empty `languages` array;
omitting languages selects all languages configured for that country. For
example:

```json
{
  "countries": [
    {"code": "PL", "languages": ["pl"]},
    {"code": "UA", "languages": ["uk", "ru"]}
  ],
  "outside_catalog": [
    {"country": "Exampleland", "reason": "Relevant transit location"}
  ]
}
```

The CLI derives country names and validates codes/languages from the
configuration rather than trusting caller-supplied labels. Out-of-catalog
entries require a reason and cannot duplicate a catalog country. Review with
`cnvs matrix review PLAN-ID --decision ACCEPTED --reviewer reviewer --rationale
"..."`; inspect proposal history with `cnvs matrix plans --event-id EVENT-ID`.

Matrix cells use only documents whose latest event/source-link review is
`LINKED`. Reporting document and source counts come from immutable collection
metadata snapshots, not mutable current registry data. A source whose snapshots
conflict on country or source class remains visible through its linked
documents but is excluded from source, claim and evidence counts; the affected
cell reports this limitation. Language coverage counts only documents whose
language is recorded without a review-required conflict. Institutional source
counts use the recorded `INSTITUTIONAL_STATEMENT` class; sources recorded as
`PRIMARY_OBSERVATION` are counted separately. These source classes are
independent of Evidence `directness`. Claims are canonical event claims
attached to the linked country sources; support and contradiction totals
include only currently `LINKED` claim/evidence relationships.

The primary-evidence count is limited to evidence with `PRIMARY` directness
whose effective verification status is `VERIFIED`. It is not a count of
independent confirmations. Accepted source, document and evidence
independence-group assignments are shown as labels, never summed into a truth
score. Coverage gaps call out missing confirmed reporting/languages,
institutional statements and verified primary-directness evidence.

Analysts can add a country assessment with
`cnvs matrix assess PLAN-ID COUNTRY-CODE --file assessment.json --analyst
analyst --rationale "..."`. The JSON records a dominant frame, attribution
summary, separate `occurrence_confidence`, `method_confidence` and
`attribution_confidence` values (`LOW`, `MEDIUM`, `HIGH` or `UNKNOWN`),
omissions, contradictions and optional source/claim/evidence ID arrays.
Confidence is explicitly analyst judgment and is never derived from report
volume. References must belong to the event and selected country. A reviewer
must accept, reject or mark the assessment unresolved with
`cnvs matrix review-assessment ASSESSMENT-ID --decision ACCEPTED --reviewer
reviewer --rationale "..."`. The matrix displays the current accepted
assessment in preference to pending proposals; `cnvs matrix assessments
--plan-id PLAN-ID` exposes every proposal and its current review state.
`cnvs matrix show EVENT-ID` prints the selected matrix and makes coverage gaps,
configuration drift and assessment states visible. Country comparisons are
coverage summaries, not claims that any country is inherently correct.

## Analyst report snapshots

`cnvs report create ASSESSMENT-ID` reconstructs the selected immutable
assessment revision and its pinned event, source, claim and evidence
revisions. The report also captures the linked source-document matches,
confirmed provenance links, accepted independence assignments, reviewed
claim/evidence relations, current timeline and evidence gaps, plus the
country-coverage plan/matrix available at generation time. This complete input
snapshot is stored as deterministic JSON with a SHA-256 digest; the rendered
Markdown and escaped HTML are stored with a second digest and cannot be
updated or deleted. Re-rendering the stored structured sections produces the
same report bytes.

The rendered report keeps FACT, CLAIM, ASSESSMENT, CONFIDENCE and GAP distinct,
prints source/claim/evidence revision identifiers, labels contradictions,
and preserves UNKNOWN confidence and unresolved attribution. Occurrence,
method and attribution confidence remain separate dimensions. The assessment's
`human_reviewed` field is not treated as approval: report approval or rejection
is a separate append-only entry tied to the exact immutable report ID, with a
reviewer label, timestamp and rationale. Markdown and HTML export is refused
unless the latest report review is `APPROVED`; a later rejection revokes that
report's export eligibility.

The current CLI accepts a reviewer label supplied by its operator; it does not
authenticate that identity or enforce analyst/internal-reader roles. Access
control, secure distribution and external publication are not implemented.
Reports are internal workflow artifacts, and the export command writes local
files only.
