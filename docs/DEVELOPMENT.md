# Development setup

The Python CLI validates the reviewed MVP configuration, manages local SQLite
persistence, supports explicitly approved public RSS/URL collection, and
records source-grounded claim extraction candidates for analyst review.
Automatic NLP/LLM extraction, automated event analysis, report generation,
authentication and an API are not implemented.

## Requirements

- Python 3.12 or newer
- Internet access to install the declared dependencies (unless already
  available to pip)

Live collection requires outbound DNS and HTTP(S) access to the configured
public source and its `robots.txt`. No API credentials, service containers or
`.env` file are required.

## Windows PowerShell

From the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --constraint requirements.txt --editable .
python -m cnvs config validate
python -m unittest discover -s tests -v
```

If Python is installed at a known path, use that interpreter to create the
virtual environment, for example:

```powershell
C:\Python312\python.exe -m venv .venv
```

## Linux and macOS

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --constraint requirements.txt --editable .
python -m cnvs config validate
python -m unittest discover -s tests -v
```

The checked-in `requirements.txt` pins the runtime dependency used for local
development and CI. Editable installation registers the `cnvs` command and
installs the project dependencies declared in `pyproject.toml`. The test suite
uses Python's standard-library `unittest`; no separate test package is needed.

## CLI and configuration

```text
cnvs --version
cnvs config validate
cnvs config validate --config-dir path/to/config
cnvs source list
cnvs source collect SOURCE-ID
cnvs source results
cnvs source results --source-id SOURCE-ID
cnvs source retry COLLECTION-ID
cnvs source normalize COLLECTION-ID
cnvs source documents --collection-id COLLECTION-ID
cnvs source duplicates --status PENDING
cnvs source review-duplicate RELATIONSHIP-ID --decision CONFIRMED_DEPENDENT --reviewer analyst --rationale "Shared wire copy"
cnvs source translation-add DOCUMENT-ID --target-language pl --method "analyst translation" --translator analyst --text-file translation.txt
cnvs source translations DOCUMENT-ID
cnvs event create --file event-record.json
cnvs event list
cnvs event show CNVS-EVT-2026-10-08-0001
cnvs event show CNVS-EVT-2026-10-08-0001 --revision 1
cnvs event update --file corrected-event-record.json
cnvs event match CNVS-EVT-2026-10-08-0001 DOCUMENT-ID --proposed-by analyst --rationale "Same incident location and time window"
cnvs event matches --status PENDING
cnvs event match-history MATCH-ID
cnvs event review-match MATCH-ID --decision LINKED --reviewer reviewer --rationale "Manually corroborated"
cnvs event timeline CNVS-EVT-2026-10-08-0001
cnvs event timeline-edit ENTRY-ID --event-time 2026-10-07T08:15:00Z --analyst analyst --rationale "Primary notice confirms occurrence time"
cnvs event timeline-edit ENTRY-ID --clear-event-time --analyst analyst --rationale "Previously recorded time was publication time"
cnvs event timeline-history ENTRY-ID
cnvs claim extract CNVS-EVT-2026-10-08-0001 DOCUMENT-ID --file candidates.json --method "analyst-assisted JSON import" --extractor analyst
cnvs claim list --event-id CNVS-EVT-2026-10-08-0001 --status PENDING
cnvs claim review CANDIDATE-ID --decision CORRECTED --reviewer analyst --rationale "Refined extraction" --correction-file corrected-candidate.json
cnvs claim history CANDIDATE-ID
```

Configuration directory resolution is:

1. `--config-dir`, when provided;
2. `CNVS_CONFIG_DIR`, when set;
3. `./config` relative to the current working directory.

The CLI reports an actionable error and non-zero exit status if configuration
is missing or inconsistent. Validation checks the approved MVP country,
language, event-type and source-type catalogs, the registry schema and
country/language relationships, unique IDs, source review state and collection
constraints. A registry entry must be explicitly analyst-approved and enabled
before collection; the checked-in synthetic entry is pending and disabled.
Scope changes require updating the requirements, configuration and validator
together.

### Source registry and collection

Configure sources in `config/source_registry.yaml`. Each entry records its
publisher, source class, MVP country and language, public access method (`RSS`
or `URL`), event relevance, review metadata and explicit collection limits.
`review.status: approved`, a named reviewer, a timezone-aware `reviewed_at`,
and `enabled: true` are required before `source collect` will make a network
request. Source approval is an analyst attestation, not a legal determination.
Never put credentials or tokens in source URLs.

The collector accepts only HTTP(S), checks that each destination and redirect
resolves to public IP addresses, limits redirects, download size and time, and
honors `robots.txt`. It does not bypass access controls, retry automatically,
or fetch individual RSS articles. A URL collection stores its original and
final URL as origin identifiers; RSS collection also records feed item IDs
and links. Each attempt has an immutable database result with status,
timestamps, HTTP/media metadata, digest, failure details and optional snapshot
reference, together with a snapshot of the registry metadata used for that
attempt. `source retry` is manual and only accepts a failed latest attempt;
blocked attempts require source/access review rather than retrying. Results
remain inspectable even if the registry later changes.

Set `archive_content: true` only when applicable access terms permit retaining
the content and provide a `retention_basis`. When archiving is disabled, CNVS
stores provenance metadata and a digest but not the fetched body. These
controls do not provide legal advice, retention/deletion administration or
protection against a source's terms changing after review.

## Source documents and events

### Normalization and duplicate review

Run `cnvs source normalize COLLECTION-ID` only for successful collections
whose registry permits archiving. The command extracts RSS/Atom item text or
visible HTML text and stores both the extracted original and a normalized
copy. It retains the original title, URL, publisher and publication timestamp;
the normalized timestamp is UTC only when the source included a timezone.
Unzoned timestamps are kept without inventing a timezone.

Language comes from an item/page declaration, then the registry, or is stored
as `unknown`; conflicts and unsupported declarations are explicitly marked for
review. This release does not run automatic statistical language detection
and does not translate content. Record a supplied translation with
`source translation-add`; its source-text hash, language pair, method,
translator and timestamp are retained separately.

`source duplicates` shows exact normalized-text matches and likely
syndication candidates. Exact matches are based on a SHA-256 hash of the
normalized text. Possible syndication is signalled by shared canonical URL or
origin identifiers, or by five-word-shingle Jaccard similarity of at least
0.82 (0.65 when title similarity is at least 0.90). These are review
candidates, not automatic merges or independent-evidence decisions. Candidates
start pending; `source review-duplicate` appends a rationale-bearing analyst
decision. Until then, do not count matching material as independent
confirmation.

The CLI displays extracted source text; use it only where the source terms
permit retaining content. When `archive_content` is false, normalization is
intentionally unavailable and no derived text is stored.

### Events and timelines

Create or revise an event using a JSON record that satisfies
`schemas/event.schema.json`. `event create` only creates a new ID;
`event update` requires an existing ID and stores a new immutable revision.
`event list` and `event show` inspect current event records; `event show
--revision N` reads a specific historical version.

Use `event match EVENT-ID DOCUMENT-ID` to propose that a normalized source
document concerns an event. The analyst's proposal rationale is retained and
the candidate starts `PENDING`; it is not an event link until an explicit
review records `LINKED`. `REJECTED` and `UNRESOLVED` decisions are also
append-only, rationale-bearing reviews. Multiple events may remain candidate
matches for one document until ambiguity is resolved. CNVS does not
automatically match documents or merge event records. `event match-history`
shows the original proposal and every decision in append order.

Confirming a match creates a timeline entry with unknown occurrence time.
`event timeline EVENT-ID` displays confirmed links in chronological order,
showing event occurrence time, original and normalized publication time,
timezone knowledge, and collection completion time as separate values.
`event timeline-edit` records or clears an analyst's event-time correction
with editor identity and rationale. Each correction appends an immutable
revision; `event timeline-history` displays the audit trail. The system does
not infer an occurrence time from publication or collection time.

## Local database
Stages 3-7 use SQLite and apply checksummed migrations automatically when the
database is opened. The default database path is `.data/cnvs.sqlite3`; override
it with `CNVS_DATABASE_PATH` or `--database` on a database command:

```powershell
python -m cnvs db migrate
python -m cnvs db status
python -m cnvs db status --database .data/research.sqlite3
```

Database files are local runtime data and are excluded from Git. Back up and
protect them according to the sensitivity and retention of the records they
contain. This storage layer does not implement authentication, role-based
permissions, administrator deletion workflows, encryption at rest or a
production backup policy.

Canonical JSON Schema records are validated before storage, and database
revisions preserve prior record payloads. Raw snapshots are content-addressed;
collection attempts, normalized documents, translations, duplicate matches
and duplicate-review decisions, event/source match proposals and reviews, and
event timeline revisions, claim extraction candidates and claim reviews are
append-only. `cnvs db status` shows the applied schema migrations; changed
migration files are rejected and require a new migration instead.

### Claim extraction candidates

First create and explicitly link the event to the normalized document using
the event workflow above. `claim extract` imports a JSON array; each object
must have exactly these fields:

```json
{
  "span_start": 15,
  "span_end": 39,
  "subject": "the port",
  "predicate": "remained",
  "object": "closed",
  "claim_type": "INCIDENT",
  "attribution": "Officials",
  "modality": "REPORTED"
}
```

The offsets are zero-based Python string character offsets into the original
extracted document text (end-exclusive), not byte offsets; the example span
quotes `the port remained closed` from `Officials said the port remained
closed.` The importer stores that exact quote and the source-text digest, and
rejects out-of-range/empty spans or malformed fields. The entire batch is
rolled back if a candidate is invalid. Provide `--method` and `--extractor`
to preserve who/what generated the derived candidate.

Review decisions are `ACCEPTED`, `REJECTED`, `CORRECTED` and `UNRESOLVED`;
each requires a reviewer and rationale. `CORRECTED` requires a JSON object in
the same field format, checked against the archived original text. Reviews
append to an immutable history. `ACCEPTED` means the candidate's structure
and attribution have been reviewed, not that its proposition is true.
Corrections remain review payloads; candidates are not promoted to canonical
claims or evidence. The CLI does not invoke a model, and imported text remains
untrusted data.

`CNVS_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`;
the default is `WARNING`. Environment values are read from the process
environment. CNVS does not implicitly load `.env` files. Do not put credentials
or private analyst data in tracked configuration.

The configuration is a reviewed MVP catalog; extend or change it only through
a documented scope change in `docs/MVP_REQUIREMENTS.md`.
