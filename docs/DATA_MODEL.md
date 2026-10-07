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
