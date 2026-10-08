# QHDALabs Cross-National Verification System (CNVS)

CNVS is a human-in-the-loop research system for investigating events across
national information environments. It keeps observable facts, evidence,
source-attributed claims, attribution and analyst assessment distinct, while
recording where information originated and whether evidence chains are
independent.

> **Status: early MVP implementation.** The CLI can validate the reviewed
> configuration, manage a local SQLite database, collect approved public RSS
> feeds and URLs, create versioned event records, review source-to-event
> matches, maintain auditable event timelines, and record source-grounded
> derived claim candidates for analyst review. The checked-in source registry
> contains only a disabled synthetic example: no live source has been reviewed
> or approved. CNVS is not a verification engine or full analyst application.

## Guiding principles

- Repetition is not independent confirmation.
- Preserve raw sources and their provenance.
- Keep observations, evidence, claims and assessments as separate records.
- Treat attribution as unresolved until evidence supports a conclusion.
- Represent uncertainty, contradictions and information gaps explicitly.
- Use automated analysis to support, not replace, human analysts.

See [MANIFEST.md](./MANIFEST.md) for the mission and analytical principles, and
[ARCHITECTURE.md](./ARCHITECTURE.md) for the proposed system design and
implementation phases.

## Repository layout

```text
.
├── .gitignore
├── README.md
├── MANIFEST.md
├── ARCHITECTURE.md
├── AGENT.md              # Working guidance for AI coding agents
├── pyproject.toml        # Python 3.12+ package metadata and CLI entry point
├── requirements.txt      # Pinned runtime dependency for local setup and CI
├── log.log               # Completed work and roadmap to the MVP
├── docs/                 # Data model, development, MVP requirements and policies
├── CONTRIBUTING.md       # Contribution and validation guidance
├── LICENSE               # MIT License
├── SECURITY.md           # Private vulnerability reporting
├── .github/              # CI, Dependabot and contribution templates
├── config/               # MVP catalogs, source policy and source registry
├── ingestion/            # Source collection boundaries
├── normalization/        # Language and record normalization boundaries
├── extraction/           # Claim and attribution extraction boundaries
├── provenance/           # Source lineage and independence relationships
├── evidence/             # Evidence records and verification workflow
├── analysis/             # Event, country and narrative analysis
├── scoring/              # Explainable analytical indicators
├── retrieval/            # Search and retrieval integrations
├── reporting/            # Analyst-reviewed reports
├── api/                  # Future REST API boundary
├── workers/              # Future asynchronous processing boundary
├── ui/                   # Future analyst interface boundary
├── src/cnvs/             # Python runtime, public RSS/URL intake and CLI
├── schemas/              # JSON Schemas for canonical records
├── tests/                # Fixture and schema consistency tests
└── examples/             # Synthetic event, source, claim, evidence and report records
```

The component directories are intentionally lightweight Python package
boundaries; no production pipeline or infrastructure dependency is implied.
Their responsibilities and the canonical record shapes are described in
[docs/DATA_MODEL.md](./docs/DATA_MODEL.md). Confirmed MVP scope and measurable
acceptance criteria are in [docs/MVP_REQUIREMENTS.md](./docs/MVP_REQUIREMENTS.md).

Completed repository work and the staged plan to reach the MVP are tracked in
[log.log](./log.log). Planned capabilities in that log are not implemented
unless their status is explicitly updated.

## Example records

The synthetic incident in `examples/` demonstrates the separation between an
event, an original source, a claim, and evidence. It is illustrative only and
does not describe a real event.

- [Event](./examples/event.json)
- [Source](./examples/source.json)
- [Claim](./examples/claim.json)
- [Evidence](./examples/evidence.json)
- [Assessment snapshot](./examples/assessment.json)
- [Analyst report](./examples/event-report.json)

The examples use stable IDs to link records. The event report keeps occurrence,
method and attribution confidence separate and states remaining gaps explicitly.

## Development setup

The project requires Python 3.12 or newer. From the repository root, create an
environment, install the package in editable mode, validate configuration and
run the tests:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --constraint requirements.txt --editable .
python -m cnvs config validate
python -m unittest discover -s tests -v
```

The `cnvs` command validates configuration, manages the local SQLite schema,
collects approved public RSS/URL sources, preserves normalized document text
and provenance, records translations, and surfaces duplicate/syndication
candidates for analyst review. It also supports creating and revising event
records, proposing and reviewing source-document matches, and auditing
chronology corrections, and recording/reviewing source-grounded derived claim
candidates. Candidate matches remain unresolved until explicitly reviewed;
events and sources are never silently merged. CNVS does not run an automatic
NLP/LLM extractor, and accepted extraction candidates are not canonical claims
or confirmed facts. Cross-national analysis and operational report generation
are not implemented. See [docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md) for
platform-specific setup and environment variable details.

## Configuration

The country, language, event-type and source-type files in `config/` define the
confirmed MVP starting scope; select country coverage according to each event.
Add sources to `config/source_registry.yaml` only after an analyst has reviewed
their event relevance, access conditions and collection constraints. The
checked-in entry is synthetic, pending review and disabled. Source classes and
scoring remain uncalibrated examples. Changes to MVP scope require documented
review. See [docs/SCORING.md](./docs/SCORING.md) and
[docs/SOURCE_POLICY.md](./docs/SOURCE_POLICY.md).

```powershell
python -m cnvs source list
python -m cnvs source collect SOURCE-ID
python -m cnvs source results
python -m cnvs source retry COLLECTION-ID
python -m cnvs source normalize COLLECTION-ID
python -m cnvs source documents --collection-id COLLECTION-ID
python -m cnvs source duplicates --status PENDING
python -m cnvs event create --file event-record.json
python -m cnvs event list
python -m cnvs event match CNVS-EVT-2026-10-08-0001 DOCUMENT-ID --proposed-by analyst --rationale "Candidate incident match"
python -m cnvs event matches --status PENDING
python -m cnvs event match-history MATCH-ID
python -m cnvs event review-match MATCH-ID --decision LINKED --reviewer reviewer --rationale "Manually corroborated"
python -m cnvs event timeline CNVS-EVT-2026-10-08-0001
python -m cnvs claim extract CNVS-EVT-2026-10-08-0001 DOCUMENT-ID --file candidates.json --method "analyst-assisted JSON import" --extractor analyst
python -m cnvs claim list --event-id CNVS-EVT-2026-10-08-0001 --status PENDING
python -m cnvs claim review CANDIDATE-ID --decision ACCEPTED --reviewer reviewer --rationale "Fields match the attributed source statement"
python -m cnvs claim history CANDIDATE-ID
```

Normalization requires a retained source snapshot, so it is unavailable when
the source registry disallows content archiving. Language is recorded from
source declarations/registry or marked unknown; automated language detection
and translation are not included. Analyst- or tool-produced translations are
stored separately with provenance.

Event matching is analyst-assisted, not automatic. A proposed source-document
match stays pending until reviewed as linked, rejected or unresolved. An
accepted link creates a timeline entry; event occurrence time can then be
recorded or corrected separately from the source's publication and collection
times. Corrections append a rationale-bearing revision rather than rewriting
history. See [docs/DATA_MODEL.md](./docs/DATA_MODEL.md) and
[docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md) for details.

Claim extraction candidates can be imported from JSON only after a source
document has an explicitly `LINKED` event match. Each candidate stores the
document/source IDs, source-text SHA-256, exact character offsets and quoted
span, structured claim fields, extraction method and extractor. `claim review`
appends an `ACCEPTED`, `REJECTED`, `CORRECTED` or `UNRESOLVED` decision with a
reviewer and rationale; a correction is checked against the retained source
text. `ACCEPTED` means the candidate structure and attribution were reviewed,
not that the proposition is true. These remain derived candidates and are not
promoted into canonical claims or evidence. There is no automatic NLP/LLM
extraction; imported source text is treated as untrusted data.

## Validation

The test runner uses Python's standard-library `unittest`. Install the project
first to make its YAML and JSON Schema runtime dependencies available:

```powershell
python -m pip install --constraint requirements.txt --editable .
python -m unittest discover -s tests -v
```

Tests check JSON Schema validity and parity, example references, the confirmed
MVP configuration, registry approval constraints, mocked RSS/URL collection,
robots and public-address restrictions, append-only collection results,
normalization and preserved text/timestamp values, language uncertainty,
translation provenance, exact and likely duplicate relationships, immutable
normalized records, event lifecycle and reviewable source matching, distinct
event/publication/collection times, timeline corrections and their history,
source-grounded claim candidate spans, corrections and append-only reviews,
checksummed migrations, record revision history, source snapshots and
assessment reconstruction. They do not validate live source availability,
source rights, analyst conclusions or source approvals.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for contribution and validation
guidance. Repository-specific instructions for AI coding agents are in
[AGENT.md](./AGENT.md). GitHub Actions runs the fixture tests on supported
Python versions for pushes and pull requests.

## Security and analytical limitations

External material is untrusted data, never executable instructions. Automated
extraction is derived data and is not primary evidence. High-impact conclusions
require human review. See [SECURITY.md](./SECURITY.md),
[docs/SECURITY.md](./docs/SECURITY.md) and
[docs/ANALYST_GUIDE.md](./docs/ANALYST_GUIDE.md).

## License

This project is licensed under the [MIT License](./LICENSE).

## Project motto

> Do not count repetition as confirmation.
