# CNVS MVP release-readiness record

**Status:** Internal prototype release candidate; deployment blocked pending
security, privacy, legal and operational approval.

This document records the reproducible Stage 13 acceptance exercise. It does
not authorize real-world collection or claim that CNVS verifies events
automatically.

## Verified acceptance exercise

`tests/test_stage13_e2e.py` runs a synthetic, offline end-to-end rehearsal:

1. create an event, source, claim and evidence record;
2. collect one synthetic RSS feed and one synthetic public URL;
3. normalize both retained documents without changing their original text;
4. link both documents to the event through reviewed analyst matches;
5. create and review a claim/evidence relationship;
6. simulate an unavailable source and verify `FAILED` status, visible failure
   details and a three-attempt limit;
7. record an assessment with separate occurrence, method and attribution
   confidence dimensions and an explicit information gap;
8. create a high-impact Markdown/HTML report from pinned revisions;
9. reject export before review, approve the exact snapshot with a named human
   reviewer, and export both formats without overwriting existing files.

The fixture contains no real-world source material and makes no real-world
assertion.

## Reproducible verification

From the repository root, using Python 3.12 or newer:

```powershell
python -m pip install --constraint requirements.txt --editable .
python -m unittest discover -s tests -v
python -m cnvs config validate
python -m unittest tests.test_stage13_e2e -v
```

CI runs the complete unittest suite and configuration validation on Python
3.12, 3.13 and 3.14 through `.github/workflows/tests.yml`.

## Deployment instructions and prerequisites

Do not deploy this prototype for operational collection until all of the
following are approved for the intended organization and source set:

- legal and privacy review for every operating jurisdiction;
- source-by-source access, copyright and retention review;
- an administrator-authorized deletion/takedown procedure;
- authentication, role-based access control and protected internal report
  distribution;
- encrypted storage, backup protection, backup expiry and recovery testing;
- protected audit-log handling and operational monitoring;
- review of the configured registry, country/language catalog and event
  categories.

Before any authorized deployment, use a new isolated virtual environment,
validate the configuration, initialize a dedicated database path, and confirm
that every registry entry is explicitly approved and enabled. Never place
credentials or private analyst data in the repository or source URLs.

## Known limitations

- Reviewer and analyst names are operator-supplied labels; authentication and
  role enforcement are not implemented.
- The checked-in registry is synthetic, pending and disabled.
- Collection is limited to approved public RSS and URL sources. There are no
  social-media, authenticated, paywalled, API, satellite or AIS integrations.
- Claim extraction is an analyst-assisted JSON import; no NLP/LLM extractor is
  implemented.
- Event matching, contradiction detection, language detection, translation,
  calibration, early warning and narrative clustering are not automatic.
- SQLite persistence is local. The project does not implement administrator
  deletion, encryption at rest, backup expiry or a protected audit service.
- Parser worker limits are defensive controls, not a complete OS sandbox or
  hard memory limit.
- Reports are internal artifacts. External publication and downstream system
  integrations are out of scope.

## Rollback and recovery

A report export is approval-gated and refuses to overwrite existing files. To
roll back a release, stop collection, preserve the affected database and
export directory as controlled evidence, and restore the last approved code
and database backup using the organization's change-control procedure. Do not
rewrite or delete immutable record revisions to repair a release. Re-run
configuration validation and the complete test suite before resuming work.

The current repository does not provide automated backup, restore, deletion or
migration rollback commands; those controls must be supplied and tested by the
deployment owner before operational use.
