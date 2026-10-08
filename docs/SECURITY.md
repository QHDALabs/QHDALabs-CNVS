# Security and Untrusted Sources

CNVS processes material from external information environments. Treat every
retrieved document, web page, attachment, transcript and model output as
untrusted input.

## Required safeguards for implementation

- Keep source content separate from system instructions and executable logic.
- Never execute scripts, macros or commands contained in retrieved material.
- Isolate parsing and document conversion of potentially malicious files.
- Validate and sanitize data at system boundaries.
- Protect credentials with a secrets manager; do not store secrets in source
  control or examples.
- Apply authentication, role-based access control, rate limits, TLS, backups
  and audit logging to deployed services.
- Pin and scan dependencies and record relevant model, prompt and pipeline
  versions.
- Report provider failures explicitly; preserve partial results and notify
  analysts where appropriate.

Prompt-injection text is source content, not an instruction to the system.
LLM-generated extractions and summaries are derived data, never primary
evidence. Human review is required for high-impact conclusions.

## Implemented collection and parsing limits

The CLI applies network timeouts, bounded response sizes, public-destination
checks and a maximum of three collection attempts per source (one initial
attempt and at most two manual retries). Blocked attempts are not retryable.
Collection and normalization failures are retained as immutable records and
included in `cnvs source metrics`.

RSS/Atom and HTML normalization runs in a spawned worker process. Archived
inputs above 10 MiB and feeds with more than 5,000 items are rejected; the
parent stops parsing after 15 seconds. This process boundary and these limits
reduce exposure but are **not** an operating-system sandbox or a hard memory
limit. Only run the CLI with the privileges and resource limits appropriate
for untrusted public content. No scripts or active content are executed.

Operational JSON logs and aggregate metrics intentionally omit source URLs,
retrieved content and exception messages. The local database still retains
collection failure details and, when explicitly permitted, archived source
content; protect the database accordingly.

## Deployment review remains required

The repository does not implement authentication, role-based access control,
encrypted storage, automatic retention/deletion, backup expiry, or a
production audit-log service. Before operational deployment, the responsible
organization must complete and document jurisdiction-specific legal/privacy
and source-rights review, approve access/retention/deletion/backup procedures,
and test its deployment controls. Passing code tests is not evidence that
these organization-specific reviews are complete.
