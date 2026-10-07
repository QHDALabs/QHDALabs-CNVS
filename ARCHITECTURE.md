# QHDALabs Cross-National Verification System (CNVS)

**Architecture Specification**  
**Project:** QHDALabs Cross-National Verification System  
**Document:** `ARCHITECTURE.md`  
**Status:** Draft v0.1  
**Classification:** Public technical concept  
**Language:** English

> **One event. Many narratives. One evidence chain.**

---

## 1. Purpose

This document defines the technical architecture of the QHDALabs Cross-National Verification System (CNVS).

CNVS is designed to transform a real-world incident or information claim into a structured, traceable analytical object that can be compared across national information environments.

The architecture separates:

```text
EVENT
  ↓
OBSERVATIONS
  ↓
SOURCES
  ↓
CLAIMS
  ↓
EVIDENCE
  ↓
SOURCE PROVENANCE
  ↓
NATIONAL NARRATIVES
  ↓
CROSS-NATIONAL COMPARISON
  ↓
ASSESSMENT
```

The central architectural requirement is:

> **No layer may silently replace another layer.**

A news article is not an event.  
A claim is not a fact.  
A government statement is not independent confirmation.  
An LLM assessment is not evidence.

---

# 2. Architectural Goals

CNVS shall optimize for:

1. **Traceability** — every important conclusion can be traced to source material.
2. **Source independence** — repeated reporting is not automatically counted as independent confirmation.
3. **Cross-national comparison** — the same event can be analyzed across countries and languages.
4. **Temporal integrity** — the system preserves what was known and when.
5. **Uncertainty preservation** — uncertainty survives extraction, translation and summarization.
6. **Human auditability** — analysts can inspect the complete evidence chain.
7. **Reproducibility** — an analyst can reconstruct an assessment from stored inputs.
8. **Modularity** — ingestion, extraction, graphing and analysis can evolve independently.
9. **Provider neutrality** — no single media provider or LLM is a single point of analytical truth.
10. **Operational resilience** — source outages or model failures should degrade the system gracefully.

---

# 3. Non-Goals

CNVS is not intended to:

- determine political truth automatically;
- make legal attribution decisions;
- autonomously accuse a state or organization;
- replace primary-source verification;
- score ideological correctness;
- equate source popularity with reliability;
- infer intent solely from language;
- or produce a single "truth probability" without showing the underlying evidence.

---

# 4. System Context

CNVS operates as a verification layer between raw information collection and higher-level strategic analysis.

```text
┌──────────────────────────────────────────────────────────────┐
│                       EXTERNAL WORLD                          │
│                                                              │
│ incidents • governments • media • sensors • public records  │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                         INGESTION                            │
│                                                              │
│ RSS • APIs • websites • official feeds • documents • URLs   │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                     NORMALIZATION                            │
│                                                              │
│ language • metadata • timestamps • entities • deduplication │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                    CLAIM EXTRACTION                           │
│                                                              │
│ claims • qualifiers • attribution • evidence references      │
└──────────────────────────────┬───────────────────────────────┘
                               │
                 ┌─────────────┴─────────────┐
                 ▼                           ▼
┌──────────────────────────────┐   ┌──────────────────────────────┐
│       SOURCE GRAPH           │   │      EVIDENCE LAYER          │
│                              │   │                              │
│ origin • quoting • copying   │   │ primary/secondary signals    │
│ dependency • provenance      │   │ imagery • AIS • notices      │
└──────────────┬───────────────┘   └──────────────┬───────────────┘
               └──────────────────┬──────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────┐
│                 CROSS-NATIONAL ANALYSIS                      │
│                                                              │
│ country matrix • narrative divergence • attribution status  │
│ source independence • temporal drift • evidence convergence │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                       HUMAN REVIEW                            │
│                                                              │
│ analyst validation • corrections • assessment • publication │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                   DOWNSTREAM SYSTEMS                          │
│                                                              │
│ GEWS • Sentinel • dashboards • reports • alerts • research  │
└──────────────────────────────────────────────────────────────┘
```

---

# 5. Core Architectural Principle: Evidence First

CNVS uses an evidence-first architecture.

The preferred dependency direction is:

```text
source → claim → evidence assessment → interpretation
```

and not:

```text
desired conclusion → search for supporting articles
```

This means search and retrieval systems are subordinate to the event/evidence model.

---

# 6. High-Level Components

CNVS consists of the following logical components.

## 6.1 Event Intake

Receives:

- URLs,
- RSS items,
- API records,
- analyst-created incidents,
- alerts from other QHDALabs systems,
- structured incident IDs,
- and free-text claims.

Output:

```text
EventCandidate
```

---

## 6.2 Event Resolver

Determines whether incoming records describe:

- the same event,
- a related event,
- a duplicate,
- an update,
- or an unrelated event.

Functions:

- temporal matching;
- geographic matching;
- entity matching;
- semantic similarity;
- incident fingerprinting;
- analyst override.

Output:

```text
CanonicalEvent
```

---

## 6.3 Source Registry

Maintains normalized metadata for every source.

Source classes:

```text
MEDIA
GOVERNMENT
MILITARY
POLICE
INTELLIGENCE
INTERNATIONAL_ORGANIZATION
ACADEMIC
TECHNICAL
SOCIAL
PRIMARY_OBSERVATION
OTHER
```

Each source receives a stable identifier.

---

## 6.4 Ingestion Layer

Supports multiple acquisition methods:

```text
RSS
Atom
Web page retrieval
Official APIs
Public datasets
Document ingestion
Manual URL submission
Structured feeds
```

The ingestion layer stores the raw source before downstream processing.

### Rule

> **Raw source material must never be replaced by model-generated summaries.**

---

## 6.5 Normalization Pipeline

Normalizes:

- encoding;
- language;
- title;
- body;
- author;
- publication time;
- update time;
- country;
- organization;
- entities;
- source type;
- URL;
- hashes.

It also detects:

- duplicate documents;
- near-duplicates;
- syndicated articles;
- translated copies;
- quoted material.

---

## 6.6 Language Layer

CNVS is multilingual.

Responsibilities:

- language detection;
- source-language preservation;
- translation;
- uncertainty preservation;
- terminology alignment;
- cross-language semantic matching.

The original text is always retained.

Translated text is a derivative representation.

```text
ORIGINAL
   ↓
TRANSLATION
   ↓
STRUCTURED CLAIM
```

Never:

```text
TRANSLATION
   ↓
ORIGINAL
```

---

## 6.7 Claim Extraction Engine

Extracts structured claims from source material.

Example:

```json
{
  "claim_id": "CLM-2026-000123",
  "subject": "tanker_A",
  "predicate": "attacked_by",
  "object": "drone",
  "attribution": "Russian Ministry of Defence",
  "modality": "claimed",
  "confidence": 0.82,
  "source_id": "SRC-001234"
}
```

The engine must preserve qualifiers such as:

- allegedly;
- reportedly;
- according to;
- officials said;
- cannot independently verify;
- believed to be;
- confirmed;
- disputed.

---

# 7. Canonical Event Model

Every event receives a stable identifier.

Example:

```text
CNVS-EVT-2026-10-07-0001
```

Recommended fields:

```yaml
event_id:
event_type:
title:
description:
start_time:
end_time:
location:
country:
coordinates:
entities:
status:
created_at:
updated_at:
```

Event types may include:

```text
MILITARY
SABOTAGE
CYBER
MARITIME
AVIATION
INFRASTRUCTURE
POLITICAL
DIPLOMATIC
ENERGY
INDUSTRIAL
NATURAL
OTHER
```

---

# 8. Observation Model

An observation is a statement about something directly observed or recorded.

Example:

```text
OBS-001
"Explosion visible on vessel X."
```

Observation sources may include:

- satellite imagery;
- verified video;
- photographs;
- vessel tracking;
- flight tracking;
- maritime notices;
- official incident logs;
- sensor data;
- eyewitness reports.

Observations must remain separate from attribution.

---

# 9. Evidence Model

Evidence is not merely a URL.

A CNVS evidence record should describe:

```yaml
evidence_id:
evidence_type:
observation:
source_id:
collection_time:
event_time:
directness:
independence_group:
reliability_assessment:
verification_status:
analyst_notes:
```

### Example

```yaml
evidence_id: EVD-00127
evidence_type: SATELLITE_IMAGE
observation: "Damage visible on vessel deck."
source_id: SRC-SAT-001
directness: PRIMARY
independence_group: SAT_PROVIDER_A
verification_status: VERIFIED
```

---

# 10. Claim Model

A claim is a proposition made by a source.

Core fields:

```yaml
claim_id:
event_id:
subject:
predicate:
object:
claim_type:
attribution:
modality:
source_id:
publication_time:
supporting_evidence_ids:
contradicting_evidence_ids:
status:
confidence:
```

Claim status:

```text
UNVERIFIED
PARTIALLY_SUPPORTED
SUPPORTED
CONTRADICTED
DISPUTED
WITHDRAWN
```

---

# 11. Attribution Model

Attribution must be represented explicitly.

```yaml
attribution:
  actor:
  claimed_by:
  basis:
  evidence_level:
  status:
```

Possible statuses:

```text
UNKNOWN
CLAIMED
SUSPECTED
PROBABLE
HIGH_CONFIDENCE
CONFIRMED
DISPUTED
```

Important rule:

```text
CLAIMED ≠ CONFIRMED
```

---

# 12. Source Provenance Graph

The Source Provenance Graph is one of the central components of CNVS.

It models relationships such as:

```text
SOURCE_A ──quotes──> SOURCE_B
SOURCE_C ──copies──> SOURCE_A
SOURCE_D ──translates──> SOURCE_B
SOURCE_E ──references──> SOURCE_F
SOURCE_G ──publishes_statement_from──> GOVERNMENT_X
```

### Graph node types

```text
SOURCE
DOCUMENT
CLAIM
EVIDENCE
ENTITY
EVENT
ORGANIZATION
COUNTRY
```

### Graph edge types

```text
QUOTES
COPIES
SYNDICATES
TRANSLATES
REFERENCES
ATTRIBUTES_TO
SUPPORTS
CONTRADICTS
DERIVED_FROM
OBSERVES
UPDATES
```

---

# 13. Independence Groups

CNVS assigns an `independence_group` to evidence and claims.

Example:

```text
Government statement
       │
       ├── Reuters
       ├── BBC
       ├── PAP
       ├── DW
       └── local newspapers
```

If all reporting derives from the same statement, the records may belong to the same independence group:

```text
IG-0042
```

Thus:

```text
40 articles
≠
40 independent confirmations
```

---

# 14. Country Information Matrix

For each significant event, CNVS generates a country-level analytical view.

Recommended record:

```yaml
country:
media_sources:
official_sources:
primary_evidence:
dominant_claims:
dominant_frame:
attribution:
confidence:
omissions:
contradictions:
independence_score:
```

The matrix supports comparison across:

- EU;
- NATO;
- neighboring states;
- strategically relevant countries.

Country coverage should be configurable per event.

---

# 15. Narrative Model

CNVS does not define "narrative" as simply positive or negative sentiment.

Narrative dimensions include:

```text
ACTOR FRAME
CAUSE FRAME
VICTIM FRAME
RESPONSIBILITY FRAME
LEGAL FRAME
MORAL FRAME
SECURITY FRAME
TEMPORAL FRAME
STRATEGIC FRAME
```

Example:

```text
Frame A:
"Attack on civilian shipping."

Frame B:
"Retaliatory strike against Russian logistics."

Frame C:
"Unconfirmed maritime incident."

These can coexist as representations of the same event.
```

---

# 16. Narrative Divergence Engine

The divergence engine compares claims and framing across national environments.

Inputs:

- structured claims;
- attribution;
- terminology;
- omissions;
- confidence;
- source provenance;
- temporal evolution.

Outputs:

```yaml
divergence_score:
dominant_frames:
outlier_countries:
shared_elements:
disputed_elements:
evidence_asymmetry:
```

### Important constraint

A high divergence score means:

> **"Information environments disagree strongly."**

It does not mean:

> **"One side is lying."**

---

# 17. Narrative Drift Engine

CNVS maintains a temporal model of reporting.

Example:

```text
T0  Event reported
T1  Method proposed
T2  Actor alleged
T3  Government statement
T4  Independent evidence
T5  Attribution revised
T6  Correction
```

This allows analysts to identify:

- premature attribution;
- narrative hardening;
- later corrections;
- evidence-driven changes;
- coordinated terminology changes.

---

# 18. Evidence Convergence

Evidence convergence is a major analytical signal.

Possible independent evidence streams:

```text
Satellite
+
AIS
+
Official maritime notice
+
Geolocated video
+
Independent reporting
```

The system may then produce:

```text
CONVERGENCE: HIGH
```

By contrast:

```text
30 articles
      ↓
1 original government statement
      ↓
no independent evidence
```

may produce:

```text
CONVERGENCE: LOW
```

---

# 19. Scoring Framework

CNVS should avoid one universal "truth score."

Instead it uses separate dimensions.

## 19.1 Source Reliability Score (SRS)

Measures source characteristics.

Possible dimensions:

```text
provenance
transparency
historical accuracy
specialization
correction behavior
access to primary information
independence
```

---

## 19.2 Independence Score (IS)

Measures how many genuinely independent information chains support a claim.

Conceptual model:

```text
IS = independent_groups / supporting_groups
```

Implementation may become more sophisticated later.

---

## 19.3 Evidence Convergence Score (ECS)

Measures convergence across independent evidence types.

Possible inputs:

```text
primary observation
technical evidence
institutional evidence
independent reporting
cross-source agreement
```

---

## 19.4 Narrative Divergence Score (NDS)

Measures disagreement between national narratives.

Potential dimensions:

```text
attribution
causality
terminology
severity
legal characterization
strategic interpretation
```

---

## 19.5 Confidence Vector

Instead of one number:

```yaml
confidence:
  event_occurrence: HIGH
  method: MEDIUM
  actor_attribution: LOW
  intent: VERY_LOW
```

This prevents uncertainty in one dimension from contaminating another.

---

# 20. Recommended Processing Pipeline

```text
1. INGEST
2. PRESERVE RAW
3. IDENTIFY LANGUAGE
4. NORMALIZE
5. DEDUPLICATE
6. RESOLVE EVENT
7. EXTRACT CLAIMS
8. EXTRACT ATTRIBUTIONS
9. IDENTIFY EVIDENCE
10. BUILD SOURCE GRAPH
11. ASSIGN INDEPENDENCE GROUPS
12. MAP COUNTRIES
13. CLUSTER NARRATIVES
14. CALCULATE DIVERGENCE
15. CALCULATE CONVERGENCE
16. GENERATE ASSESSMENT
17. HUMAN REVIEW
18. PUBLISH
```

---

# 21. Processing Modes

CNVS should support three operating modes.

## 21.1 Batch

Useful for:

- daily reporting;
- historical datasets;
- retrospective analysis.

```text
collect → process → publish
```

## 21.2 Near Real-Time

Useful for:

- breaking incidents;
- geopolitical alerts;
- maritime/aviation incidents.

```text
event → ingest → incremental update
```

## 21.3 Analyst-Driven

An analyst submits:

```text
"Verify this claim."
```

CNVS launches targeted retrieval and comparison.

---

# 22. Data Storage Architecture

A polyglot storage model is recommended.

## 22.1 Relational Database

Recommended for:

- events;
- claims;
- sources;
- countries;
- scores;
- users;
- audit records.

Example:

```text
PostgreSQL
```

---

## 22.2 Object Storage

Recommended for:

- raw HTML;
- PDFs;
- images;
- video metadata;
- captured documents;
- normalized source snapshots.

Example:

```text
S3-compatible object storage
```

---

## 22.3 Search Index

Recommended for:

- full-text search;
- multilingual retrieval;
- semantic filtering.

Possible implementation:

```text
OpenSearch
Elasticsearch
PostgreSQL + pgvector
```

---

## 22.4 Graph Store

The Source Provenance Graph may initially be represented relationally.

At scale, a graph database may be introduced.

Candidates:

```text
Neo4j
Memgraph
Apache AGE
```

The architecture must not require a graph database from day one.

---

# 23. Event-Sourced Change Model

CNVS should preserve important state changes.

Example:

```text
EVENT_CREATED
SOURCE_ADDED
CLAIM_EXTRACTED
CLAIM_UPDATED
EVIDENCE_ADDED
ATTRIBUTION_CHANGED
SOURCE_CORRECTED
ANALYST_OVERRULED
REPORT_PUBLISHED
```

Historical records should be append-oriented.

The objective is to preserve:

> **what the system believed at time T**

rather than only the final state.

---

# 24. API Architecture

A REST API is sufficient for the initial implementation.

Recommended resources:

```text
/events
/events/{id}
/events/{id}/sources
/events/{id}/claims
/events/{id}/evidence
/events/{id}/narratives
/events/{id}/timeline
/events/{id}/countries
/sources
/claims
/search
/assessments
```

Optional later interfaces:

```text
GraphQL
WebSocket/SSE
```

for interactive analyst dashboards and near-real-time updates.

---

# 25. Example Event API Response

```json
{
  "event_id": "CNVS-EVT-2026-10-07-0001",
  "title": "Reported attack on two tankers",
  "status": "ACTIVE",
  "confidence": {
    "occurrence": "HIGH",
    "method": "MEDIUM",
    "attribution": "LOW"
  },
  "claims": 37,
  "independent_groups": 6,
  "countries": 24,
  "narrative_divergence": "HIGH",
  "evidence_convergence": "MEDIUM"
}
```

---

# 26. LLM Architecture

LLMs should be used as specialized workers rather than as a monolithic "analyst brain."

Recommended roles:

```text
LLM-EXTRACT
LLM-TRANSLATE
LLM-CLASSIFY
LLM-COMPARE
LLM-CONTRADICTION
LLM-SUMMARIZE
```

Each worker should receive structured context and produce structured output.

---

# 27. LLM Guardrails

LLM output must never directly overwrite source data.

Recommended pattern:

```text
RAW SOURCE
    ↓
LLM RESULT
    ↓
SCHEMA VALIDATION
    ↓
RULE VALIDATION
    ↓
CONFIDENCE
    ↓
STORE AS DERIVED DATA
```

LLM-generated fields should carry:

```yaml
generated_by:
model:
model_version:
prompt_version:
generated_at:
```

This makes model behavior auditable.

---

# 28. Multi-Model Strategy

CNVS should support multiple models.

Example:

```text
Fast/cheap model
    ↓
extraction / classification

Higher-quality model
    ↓
complex comparison / contradiction analysis

Human analyst
    ↓
high-impact attribution / publication
```

No single model should become a critical single point of failure.

---

# 29. Retrieval Strategy

The retrieval system should favor:

1. Primary sources
2. Official statements
3. Specialized technical sources
4. Independent journalism
5. International agencies
6. Secondary commentary
7. Social media

Social media may provide early signals but should not automatically receive high evidentiary weight.

---

# 30. Source Discovery Strategy

For each event, retrieval should search by:

```text
event name
entities
location
date/time
claimed method
claimed actor
local-language terminology
official terminology
alternate transliterations
```

For multilingual research:

```text
Polish
German
French
English
Finnish
Swedish
Danish
Norwegian
Estonian
Latvian
Lithuanian
Dutch
Spanish
Italian
Romanian
Bulgarian
Greek
Czech
Slovak
Hungarian
Croatian
Slovenian
Portuguese
Turkish
Ukrainian
Russian
```

The actual language set should be configurable.

---

# 31. Duplicate and Syndication Detection

The ingestion layer must identify:

- exact duplicates;
- near-duplicates;
- wire-service copies;
- translated copies;
- press-release republication;
- article updates.

Detection signals may include:

```text
text similarity
headline similarity
paragraph overlap
quoted block overlap
publication timing
named-source overlap
URL/reference overlap
```

---

# 32. Claim Deduplication

Separate sources may express the same underlying proposition.

Example:

```text
"Ukraine attacked the ship."

"Kyiv launched the strike."

"Ukrainian forces were responsible."

```

These should be mapped to a common semantic claim where appropriate:

```text
ACTOR(Ukraine) → RESPONSIBLE_FOR → EVENT
```

while preserving each original wording.

---

# 33. Contradiction Engine

The contradiction engine searches for incompatible claims.

Example:

```text
Claim A:
No injuries reported.

Claim B:
Three crew members injured.
```

The system produces:

```text
CONTRADICTION DETECTED
```

It must then expose:

- source A;
- source B;
- timestamps;
- source provenance;
- later corrections;
- resolution status.

The engine should not silently choose a winner.

---

# 34. Information Gap Engine

For each event CNVS should explicitly generate:

```text
KNOWN
UNKNOWN
DISPUTED
REQUIRED_TO_RESOLVE
```

Example:

```text
KNOWN:
Explosion occurred.

UNKNOWN:
Exact attack mechanism.

DISPUTED:
Responsible actor.

REQUIRED_TO_RESOLVE:
Forensic evidence / independently verified imagery / technical analysis.
```

---

# 35. Analyst Interface

The interface should expose five primary views.

## View A — Event Overview

```text
Event
Location
Time
Current confidence
Key claims
Key gaps
```

## View B — Country Matrix

```text
Country × Narrative × Source × Attribution
```

## View C — Source Graph

Interactive provenance graph.

## View D — Timeline

Event and narrative evolution.

## View E — Evidence Board

Claims linked to supporting and contradicting evidence.

---

# 36. Report Generation

CNVS should generate machine-readable and human-readable outputs.

Formats:

```text
JSON
Markdown
HTML
PDF
CSV
```

Recommended report structure:

```text
EXECUTIVE SUMMARY
FACTS
CLAIMS
SOURCE PROVENANCE
COUNTRY COMPARISON
NARRATIVE DIVERGENCE
EVIDENCE CONVERGENCE
ATTRIBUTION
CONFIDENCE
INFORMATION GAPS
TIMELINE
ASSESSMENT
SOURCE REGISTER
```

---

# 37. Integration with QHDALabs Systems

CNVS is designed as a reusable verification subsystem.

Example:

```text
                    ┌─────────────┐
                    │     RSS     │
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │    CNVS     │
                    └──────┬──────┘
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
          GEWS          Sentinel      Research
```

CNVS should expose standardized event and confidence objects so that downstream systems do not need to understand raw source material.

---

# 38. GEWS Integration

For geopolitical early warning, CNVS can provide:

```yaml
event_id:
event_type:
countries:
actors:
confidence:
evidence_convergence:
narrative_divergence:
attribution:
indicators:
information_gaps:
```

GEWS may then use these signals to evaluate whether an event represents:

- routine activity;
- anomalous activity;
- escalation;
- deception;
- strategic signaling;
- or an emerging warning indicator.

---

# 39. Security Architecture

CNVS should assume that its own information environment can be targeted.

Security controls should include:

```text
TLS
authentication
role-based access control
audit logging
immutable raw-source retention
secrets management
dependency scanning
input sanitization
malicious document isolation
rate limiting
backup
```

Potentially hostile content must be treated as data, not executable instructions.

---

# 40. Prompt Injection Defense

Because CNVS processes untrusted external content, source material may contain:

- prompt injection;
- malicious instructions;
- embedded scripts;
- adversarial text;
- fake system messages.

The processing architecture must treat retrieved content as:

```text
UNTRUSTED DATA
```

and never as trusted model instructions.

Recommended separation:

```text
SYSTEM INSTRUCTIONS
       ↓
PROCESSING LOGIC
       ↓
UNTRUSTED SOURCE CONTENT
```

Source text cannot modify system policy.

---

# 41. Reliability and Failure Handling

Every external dependency must be treated as fallible.

Examples:

```text
RSS unavailable
Website blocked
API rate limit
Model timeout
Translation failure
Search outage
Database failure
```

The system should support:

```text
retry
backoff
fallback source
queueing
partial processing
analyst notification
```

No single failed provider should invalidate the entire event.

---

# 42. Observability

CNVS should expose metrics for:

```text
ingestion latency
processing latency
source failure rate
claim extraction error rate
duplicate rate
translation failure rate
graph link confidence
LLM failure rate
human correction rate
```

Analytical quality metrics should include:

```text
false convergence
false independence
missed contradiction
incorrect attribution
post-publication correction rate
```

---

# 43. Auditability

Every published assessment should be reconstructable.

Required metadata:

```yaml
assessment_id:
event_id:
created_at:
updated_at:
analyst:
pipeline_version:
schema_version:
model_versions:
source_snapshot_ids:
claim_ids:
evidence_ids:
ruleset_version:
```

---

# 44. Versioning

CNVS should version:

```text
schema
prompts
rules
scoring
country lists
source registry
models
pipelines
reports
```

Recommended semantic versioning:

```text
MAJOR.MINOR.PATCH
```

Example:

```text
CNVS v0.1.0
```

---

# 45. Suggested Repository Structure

```text
qhdalabs-cnvs/
│
├── README.md
├── MANIFEST.md
├── ARCHITECTURE.md
├── LICENSE
├── CHANGELOG.md
│
├── docs/
│   ├── DATA_MODEL.md
│   ├── SCORING.md
│   ├── SOURCE_POLICY.md
│   ├── ANALYST_GUIDE.md
│   └── SECURITY.md
│
├── config/
│   ├── countries.yaml
│   ├── source_classes.yaml
│   ├── languages.yaml
│   └── scoring.yaml
│
├── ingestion/
├── normalization/
├── extraction/
├── provenance/
├── evidence/
├── analysis/
├── scoring/
├── retrieval/
├── reporting/
├── api/
├── workers/
├── ui/
│
├── schemas/
│   ├── event.schema.json
│   ├── claim.schema.json
│   ├── evidence.schema.json
│   └── source.schema.json
│
├── tests/
└── examples/
```

---

# 46. Initial Technology Profile

The architecture is technology-neutral, but an initial implementation may use:

```text
Python
FastAPI
PostgreSQL
pgvector
Redis
Celery / RQ / equivalent worker queue
S3-compatible object storage
OpenSearch (optional)
Neo4j / Apache AGE (optional)
Docker
```

The project should avoid premature dependence on heavyweight infrastructure.

---

# 47. MVP Architecture

The first operational version should not attempt to implement every component.

### MVP

```text
RSS / URL
   ↓
Python ingestion
   ↓
raw document store
   ↓
PostgreSQL
   ↓
claim extraction
   ↓
basic provenance
   ↓
country matrix
   ↓
Markdown / HTML report
```

Core MVP capabilities:

- 10–15 countries;
- multilingual ingestion;
- source deduplication;
- claim extraction;
- claim attribution;
- basic source graph;
- independence groups;
- timeline;
- human review.

---

# 48. Phase 2

Expand to:

- all EU states;
- all European NATO states;
- strategic neighboring states;
- automated narrative clustering;
- contradiction detection;
- improved evidence convergence;
- analyst dashboard;
- semantic search;
- automated report regeneration.

---

# 49. Phase 3

Introduce:

- near-real-time incident monitoring;
- advanced graph analysis;
- technical evidence ingestion;
- satellite/AIS integrations where legitimately available;
- model ensembles;
- uncertainty calibration;
- automated anomaly detection;
- GEWS/Sentinel integration.

---

# 50. Phase 4

Target advanced analytical capability:

```text
cross-national narrative propagation
source network analysis
temporal narrative modeling
early warning indicators
adversarial information analysis
automated evidence requests
analyst collaboration
```

---

# 51. Quality Gates

Before publication, every high-impact assessment should pass:

### Gate 1 — Provenance

Can we identify where the claim came from?

### Gate 2 — Independence

Are the supporting sources genuinely independent?

### Gate 3 — Evidence

Is there primary or independent evidence?

### Gate 4 — Attribution

Is attribution explicitly separated from occurrence?

### Gate 5 — Contradiction

Have conflicting claims been checked?

### Gate 6 — Temporal Integrity

Has the chronology been preserved?

### Gate 7 — Uncertainty

Are the unknowns explicitly stated?

### Gate 8 — Human Review

Has a human analyst reviewed high-impact conclusions?

---

# 52. Architectural Invariants

The following rules are mandatory.

```text
1. Raw sources are immutable.
2. Source provenance is preserved.
3. Claims remain attributable to sources.
4. Repetition is not independent confirmation.
5. Translation cannot remove uncertainty.
6. Attribution is a separate analytical dimension.
7. LLM output is derived data, never primary evidence.
8. Historical states remain reconstructable.
9. Unknown is a valid state.
10. Human review is required for high-impact conclusions.
```

---

# 53. Canonical Analytical Object

The long-term goal is for every CNVS event to become a portable analytical object:

```yaml
event:
  id:
  title:
  time:
  location:

facts:
  - id:

observations:
  - id:

claims:
  - id:
    source:
    attribution:
    status:

evidence:
  - id:
    type:
    independence_group:

countries:
  - country:
    narratives:
    sources:

provenance_graph:
  nodes:
  edges:

scores:
  evidence_convergence:
  narrative_divergence:
  independence:

confidence:
  occurrence:
  method:
  attribution:
  intent:

gaps:
  - description:

assessment:
  text:
```

This object should be suitable for:

- storage;
- API transport;
- report generation;
- dashboard visualization;
- GEWS input;
- future model-based analysis.

---

# 54. Design Philosophy

CNVS should remain deliberately conservative.

When evidence is weak:

```text
LOW CONFIDENCE
```

When evidence is contradictory:

```text
DISPUTED
```

When attribution cannot be established:

```text
UNKNOWN
```

When a source cannot be independently verified:

```text
UNVERIFIED
```

The system must prefer an explicit gap over an invented conclusion.

---

# 55. Final Architecture Principle

The architecture exists to enforce one distinction:

> **Information is not evidence.**

And a second:

> **Evidence is not interpretation.**

CNVS therefore treats verification as a chain:

```text
COLLECT
   ↓
PRESERVE
   ↓
TRACE
   ↓
COMPARE
   ↓
VERIFY
   ↓
ASSESS
   ↓
PUBLISH
```

The system succeeds when an analyst can move backwards from any important conclusion and answer:

> **Who said this?**

> **What did they actually say?**

> **Who did they get it from?**

> **What independent evidence exists?**

> **What contradicts it?**

> **What was known at that time?**

> **What remains unknown?**

That is the core of the QHDALabs Cross-National Verification System.

---

## Project Motto

> **One event. Many narratives. One evidence chain.**

**QHDALabs CNVS**  
*Verification before interpretation. Evidence before narrative.*
