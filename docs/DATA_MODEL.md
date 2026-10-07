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

The JSON examples in `examples/` are synthetic fixtures. The schemas are initial
contracts and will evolve with explicit schema versioning.

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
