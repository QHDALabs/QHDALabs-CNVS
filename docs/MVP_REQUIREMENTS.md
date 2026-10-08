# CNVS MVP Requirements and Acceptance Criteria

**Version:** 0.1
**Decision date:** 2026-10-07
**Status:** Initial scope confirmed by the project owner; legal and operational
review is still required before production collection.

Stage 4 now provides the registry and local collection workflow described
below. The checked-in registry intentionally has no approved live sources;
event-specific source selection and legal/privacy approval remain prerequisites
before real-world collection.

This document closes Stage 1 of `log.log`. It records the selected MVP boundary
and defines observable acceptance criteria for later implementation. It is not
evidence that any MVP capability is already operational.

## 1. Product goal

The MVP supports CNVS analysts investigating a defined event across selected
national information environments. It collects permitted public RSS and URL
material, preserves source provenance, helps structure claims and evidence,
and produces an internally reviewed report. It does not automatically decide
truth or attribution.

## 2. Confirmed MVP scope

### Source intake

The MVP may collect from:

- RSS feeds that are publicly accessible; and
- publicly accessible web pages addressed by URL.

The MVP does not include social-media collection, authenticated or
account-restricted material, paywall circumvention, protection bypass, or
third-party API integrations. A source must not be collected or archived where
its applicable access conditions or rights do not permit the intended use.

### Initial countries

The initial catalog contains these 12 countries:

| Code | Country | Initial source languages |
| --- | --- | --- |
| PL | Poland | Polish (`pl`) |
| DE | Germany | German (`de`) |
| FR | France | French (`fr`) |
| GB | United Kingdom | English (`en`) |
| FI | Finland | Finnish (`fi`) |
| EE | Estonia | Estonian (`et`) |
| UA | Ukraine | Ukrainian (`uk`), Russian (`ru`) |
| MD | Moldova | Romanian (`ro`), Russian (`ru`) |
| GE | Georgia | Georgian (`ka`), Russian (`ru`) |
| BY | Belarus | Belarusian (`be`), Russian (`ru`) |
| RU | Russia | Russian (`ru`) |
| US | United States | English (`en`) |

These are the MVP catalog, not a claim that all 12 countries are relevant to
every event. Analysts select event-relevant coverage. If relevant coverage
falls outside the catalog, the report must disclose the gap; it must not imply
that the event has been comprehensively covered.

### Initial languages

The MVP language set is exactly: `pl`, `de`, `fr`, `en`, `fi`, `et`, `uk`,
`ro`, `ka`, `be` and `ru`. Translation or normalization must preserve the
original text, identified language and uncertainty. Translation is not
independent evidence.

### Event categories

The initial event categories are:

- `MILITARY`
- `SABOTAGE`
- `INFRASTRUCTURE`
- `CYBER`
- `MARITIME`
- `AVIATION`
- `DIPLOMATIC`
- `POLITICAL`
- `ENERGY`

`INDUSTRIAL`, `NATURAL` and `OTHER` are outside this MVP scope. The wider
canonical event schema may retain those values for forward compatibility, but
the MVP workflow must not present them as supported investigation categories.

### Users, review and audience

- **Analyst (author/reviewer):** creates and updates investigations, reviews
  sources and candidate claims, records evidence, prepares reports and records
  human review before a high-impact report is approved for internal readers.
- **Internal reader:** reads approved reports; does not change source records
  or publish conclusions.
- **Administrator:** manages access and configuration and performs authorized
  deletion of retained content.

Role implementation is an implementation requirement; these role definitions
do not imply that authentication or access control already exists. The MVP
requires recorded human review, but does not require the reviewer to be a
different person from the report author.

The first analyst interface is a command-line workflow. A REST API is not part
of the initial MVP runtime and may be reconsidered after the CLI workflow has
been exercised.

Reports are for internal use. The MVP does not automatically publish reports
to the public or external systems.

### Retention and deletion

- Source copies may be retained until an authorized administrator deletes
  them; there is no automatic time-to-live in the confirmed MVP policy.
- Retention is conditional on applicable law, source access terms, copyright
  and other rights. Where a source does not permit retaining its content, do
  not archive that content; retain only permitted metadata and a reference
  where allowed.
- An authorized deletion request must be reviewable and auditable. Deletion
  handling must consider source copies, derived content and backups, subject
  to applicable legal and preservation obligations.
- Before deployment, the organization must define who may authorize deletion,
  how deletion is executed, what minimal audit metadata may remain, and how
  backup copies expire.
- This policy is not legal advice or a determination that a particular source
  may lawfully be collected or retained.

### Collection, privacy and security limits

- Collect only publicly accessible material through the approved RSS and URL
  intake.
- Respect applicable access conditions, copyright, privacy requirements and
  deletion/takedown requests. Do not bypass authentication, paywalls or
  technical access controls.
- Do not intentionally collect personal data unrelated to the investigated
  event. Data minimization, access control and handling of sensitive material
  must be reviewed before operational use.
- Treat all source content as untrusted data, never as instructions or
  executable code.
- Complete legal/privacy review for the operating jurisdictions and source
  set before real-world collection.

## 3. Non-goals for the MVP

- A truth machine, automatic fact-verdict service or fully automated
  attribution.
- Public report publication.
- Social-media ingestion, authenticated sources, paywall circumvention or
  unapproved API integrations.
- Full EU/NATO/strategic-neighborhood country coverage beyond the initial
  catalog.
- Automated narrative clustering, advanced contradiction detection, calibrated
  numeric scoring or early warning.
- An analyst dashboard, model ensemble or specialist satellite/AIS feed
  integration.

## 4. MVP acceptance criteria

All criteria below must pass before describing CNVS as an operational MVP.
They are acceptance targets, not claims about current functionality.

| ID | Acceptance criterion | Verification |
| --- | --- | --- |
| AC-01 | Only public RSS feeds and public URLs are accepted by the MVP intake. Unsupported authenticated, social-media and API inputs are rejected or clearly marked out of scope. | Exercise one RSS fixture and one public-URL fixture; verify unsupported source types cannot silently enter the workflow. |
| AC-02 | The configured MVP country catalog contains exactly the 12 countries listed in this document. | Compare the loaded configuration to the table; fail validation on missing, duplicate or extra MVP country codes. |
| AC-03 | The configured MVP language catalog contains exactly the 11 codes listed in this document, and each retained original has its language recorded or explicitly unknown. | Validate the configured code set and process a synthetic fixture for each language. |
| AC-04 | The MVP supports exactly the nine listed event categories in its investigation workflow; three deferred schema values are not presented as supported categories. | Check workflow/configuration category choices against `config/event_types.yaml`. |
| AC-05 | Analysts can create an investigation, review source records and candidate claims, record evidence and prepare an internal report draft. | Complete the analyst workflow using a synthetic investigation and inspect linked records. |
| AC-06 | High-impact reports cannot be marked approved for internal readers until an analyst has recorded human review. | Attempt approval without review, then record review and verify the approved state is attributable to the reviewing analyst. |
| AC-07 | Internal readers can access approved reports but cannot edit source records or publish unreviewed conclusions. | Verify role permissions with analyst, internal-reader and administrator test identities. |
| AC-08 | Reports distinguish facts, source-attributed claims, assessments, confidence and information gaps; occurrence, method and attribution confidence are separate. | Check a generated synthetic report against the required sections and confidence dimensions. |
| AC-09 | Reports identify relevant country coverage and disclose when relevant countries or languages are outside the initial catalog. | Generate a report with an out-of-catalog relevant country and verify a visible coverage-gap statement. |
| AC-10 | Each source and claim remains traceable to its origin; known syndication/dependency does not count as independent confirmation. | Trace fixture claims to source records and verify dependent copies share a recorded lineage/independence group. |
| AC-11 | Original material is not altered by normalization; applicable collection time, publication time and event time remain distinct when known. | Compare stored original fixture with normalized data and inspect all available timestamps. |
| AC-12 | Retained source content has no automatic expiry under the confirmed policy, can be deleted by an authorized administrator, and is not archived when source conditions do not permit it. | Verify retention configuration, role-gated deletion, deletion audit and a source with a no-archive condition. |
| AC-13 | Collection and retention rules prohibit bypassing logins, paywalls or technical controls; deletion/takedown and privacy handling have an approved procedure. | Review source-admission rules and exercise a prohibited-access and deletion-request test case. |
| AC-14 | Retrieved text is handled as untrusted data; model-generated content, if later added, is marked as derived and cannot become primary evidence. | Run an adversarial synthetic source fixture and verify it cannot change system instructions or evidence provenance. |
| AC-15 | Source failures and partial processing are visible; they do not produce a success-shaped complete report. | Simulate unavailable feed/URL and verify explicit failure status, bounded retry behavior and visible report gap. |
| AC-16 | The documented setup and automated test suite pass from a clean environment; no credentials or machine-local secrets are required in the repository. | Follow setup instructions in a clean checkout, run CI tests and scan tracked files for secrets. |

Criteria involving behavior, storage, permissions and collection are to be
implemented and tested incrementally. Stage 11 implements reproducible report
rendering and the attributable review/export gate in AC-06. The CLI's reviewer
label is self-reported: authentication and role enforcement are not
implemented, so AC-07 remains open. Stage 1 is complete when scope, decisions
and acceptance targets are documented and reviewed—not when these criteria
pass.

## 5. Decision record and remaining prerequisites

The source types, initial country/language set, event categories, internal
audience and retention choice above reflect the project owner's decisions
recorded on 2026-10-07.

Before real-world data collection or deployment, the project still needs
organization-specific legal/privacy review, source-by-source rights and access
review, an approved deletion procedure, and a decision on backup retention.
The retention default must not override a shorter legal, contractual or
source-specific limit.
