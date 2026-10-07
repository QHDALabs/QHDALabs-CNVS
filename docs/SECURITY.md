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
