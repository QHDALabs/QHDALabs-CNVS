# QHDALabs Cross-National Verification System (CNVS)

**Cross-National Verification System**

> **One event. Many narratives. One evidence chain.**

---

## 1. Manifest

QHDALabs Cross-National Verification System (CNVS) is an analytical system for the **cross-national verification of information about real-world events**.

CNVS exists because a single news report rarely represents the event itself.

It represents a combination of:

- available evidence,
- source selection,
- editorial decisions,
- institutional communication,
- national context,
- language,
- assumptions,
- and sometimes deliberate influence.

Therefore:

> **The fact that many sources repeat the same claim does not mean that the claim has been independently verified.**

CNVS is designed to separate:

**EVENT → EVIDENCE → SOURCE → CLAIM → NARRATIVE → INTERPRETATION**

and to identify where these layers agree, diverge, or become disconnected.

---

# 2. Mission

The mission of CNVS is to answer five questions:

### 1. What actually happened?

Identify the observable event independently of how it is described.

### 2. What is being claimed?

Extract explicit claims made by media, governments, institutions, experts and other sources.

### 3. Where did the claim originate?

Trace information backwards through the source chain to identify the earliest identifiable source and transmission path.

### 4. How does the narrative differ between countries?

Compare how the same event is framed across national information environments.

### 5. What remains unknown?

Explicitly identify information gaps, contradictions and unresolved attribution.

---

# 3. Core Principle

CNVS follows one fundamental rule:

> **Do not count repetition as confirmation.**

Ten articles repeating Reuters are not ten independent confirmations.

Twenty websites quoting one government statement are not twenty independent sources.

A claim repeated across ten countries may still originate from a single source.

Therefore CNVS distinguishes between:

- **volume of reporting**
- **number of sources**
- **number of independent evidence chains**

These are not equivalent.

---

# 4. What CNVS Is

CNVS is:

- a cross-national information verification layer,
- a source-provenance system,
- a narrative comparison engine,
- a claim extraction and classification system,
- an evidence-gap detector,
- a media independence analyzer,
- a structured OSINT research tool,
- and an analytical component for early-warning systems.

CNVS is intended to support human analysts rather than replace them.

---

# 5. What CNVS Is Not

CNVS is not:

- a truth machine,
- a propaganda detector based solely on language,
- an automatic attribution engine,
- a popularity/ranking system for news,
- a replacement for primary evidence,
- an algorithm deciding political legitimacy,
- or a system that treats government statements as inherently truthful or false.

CNVS does not determine truth merely from **who says something**.

It evaluates:

> **what is being claimed, what evidence supports it, where the information originated, and whether independent evidence exists.**

---

# 6. Analytical Model

Every investigated event should be represented through the following chain:

```text
EVENT
  ↓
OBSERVATION
  ↓
EVIDENCE
  ↓
SOURCE
  ↓
CLAIM
  ↓
ATTRIBUTION
  ↓
NARRATIVE
  ↓
INTERPRETATION
```

These layers must not be silently merged.

For example:

```text
EVENT:
Explosion occurred on vessel X.

EVIDENCE:
Video / satellite image / crew statement / maritime authority notice.

CLAIM:
The vessel was attacked by a drone.

SOURCE:
Russian Ministry of Defence.

ATTRIBUTION:
Russia claims Ukraine was responsible.

NARRATIVE:
"Ukrainian terrorist attack."

ASSESSMENT:
Attribution not independently verified.
```

The system must preserve these distinctions.

---

# 7. Evidence Hierarchy

CNVS uses an explicit evidence hierarchy.

### Tier 0 — Narrative only

Statements without independently verifiable supporting evidence.

Examples:

- anonymous social media post,
- unsourced commentary,
- repeated allegations.

### Tier 1 — Secondary reporting

Journalistic reporting based on identifiable sources.

Examples:

- national media,
- international media,
- wire services.

### Tier 2 — Institutional statements

Official statements from:

- governments,
- ministries,
- armed forces,
- law-enforcement agencies,
- international organizations,
- regulatory authorities.

### Tier 3 — Primary observable evidence

Examples:

- satellite imagery,
- AIS data,
- NOTAMs,
- maritime notices,
- official incident logs,
- geolocated imagery,
- verified video,
- photographs,
- sensor observations.

### Tier 4 — Independent convergence

Multiple technically or institutionally independent evidence streams supporting the same conclusion.

This is the strongest category.

---

# 8. Source Independence

CNVS must explicitly model source dependency.

A source should not be counted as independent merely because it is a different publication.

Example:

```text
Government statement
        ↓
Reuters
        ↓
BBC
        ↓
Deutsche Welle
        ↓
PAP
        ↓
National newspapers
        ↓
Social media accounts
```

This is not six independent confirmations.

It may be:

> **one information event propagated through six distribution channels.**

CNVS therefore maintains a **Source Provenance Graph**.

---

# 9. Source Provenance Graph

Every significant claim should contain, where possible:

```text
claim_id
source_id
publication_time
original_source
quoted_source
referred_source
evidence_type
country
language
independence_group
confidence
```

The objective is to reconstruct:

> **Who knew what, from whom, and when?**

---

# 10. Cross-National Coverage

CNVS is designed to compare information environments across:

### NATO

European NATO members plus:

- United States
- Canada
- Türkiye

### European Union

All EU member states.

### Strategic neighborhood

Depending on the event:

- Ukraine
- Moldova
- Georgia
- Armenia
- Azerbaijan
- Belarus
- Russia
- other relevant states.

Country selection should be dynamically determined by the event.

A Baltic incident should not be analyzed using the same geographic source set as an incident in the Mediterranean.

---

# 11. National Information Matrix

Each event should produce a national matrix similar to:

| Country | Media | State | Primary Evidence | Dominant Frame | Attribution | Confidence |
|---|---|---|---|---|---|---|
| PL | … | … | … | … | … | … |
| DE | … | … | … | … | … | … |
| FR | … | … | … | … | … | … |
| UK | … | … | … | … | … | … |
| FI | … | … | … | … | … | … |
| EE | … | … | … | … | … | … |

The purpose is not to identify a "correct country".

The purpose is to identify:

- convergence,
- divergence,
- omissions,
- attribution differences,
- terminology differences,
- source dependency,
- and evidence asymmetry.

---

# 12. Narrative Divergence

CNVS must identify when the same event is framed differently.

Example:

```text
COUNTRY A:
"Russian missile attack."

COUNTRY B:
"Explosion reported; cause unclear."

COUNTRY C:
"Ukraine claims Russian attack."

COUNTRY D:
"Russia accuses Ukraine of attack."
```

CNVS should not automatically choose one.

Instead it should produce:

```text
FACT:
Explosion confirmed.

ATTRIBUTION:
Disputed.

PRIMARY ATTRIBUTION SOURCE:
Government statement from Country D.

INDEPENDENT CONFIRMATION:
Not established.

NARRATIVE DIVERGENCE:
HIGH.
```

---

# 13. Fact / Assessment Separation

CNVS must enforce a strict separation between:

### FACT

Directly supported by evidence.

### CLAIM

Statement made by a source.

### ASSESSMENT

Analytical interpretation based on available information.

### CONFIDENCE

Confidence in the assessment.

### GAP

Information required to resolve uncertainty.

Example:

```text
FACT:
Two vessels reported damage.

CLAIM:
Russian authorities attribute the attack to Ukraine.

ASSESSMENT:
Attribution remains unresolved.

CONFIDENCE:
High that the incident occurred.
Low that the responsible actor has been independently identified.

GAP:
No independent forensic attribution available.
```

---

# 14. Attribution Discipline

Attribution is one of the most dangerous areas of information analysis.

CNVS must therefore distinguish between:

```text
INCIDENT CONFIRMED
        ≠
CAUSE CONFIRMED
        ≠
ACTOR IDENTIFIED
        ≠
ACTOR RESPONSIBILITY CONFIRMED
```

The system must never silently convert:

> "Country X says Country Y did it"

into:

> "Country Y did it."

---

# 15. Language and Translation

CNVS operates across multiple languages.

Translations must preserve:

- uncertainty,
- modality,
- attribution,
- source ownership,
- qualifiers,
- temporal context.

For example:

```text
"according to"
"allegedly"
"reportedly"
"officials said"
"cannot independently verify"
"believed to be"
"confirmed"
```

must not disappear during translation.

A translation that removes uncertainty is treated as an analytical corruption.

---

# 16. Temporal Integrity

CNVS must preserve the chronology of information.

Every significant record should include:

```text
event_time
observation_time
publication_time
update_time
source_time
```

This allows the system to distinguish:

- what was known at the time,
- what became known later,
- what was added retrospectively,
- and when a narrative changed.

A later correction must not overwrite the historical state.

---

# 17. Narrative Drift

CNVS should detect when reporting changes over time.

Example:

```text
10:00
"Explosion reported."

12:30
"Possible drone attack."

15:00
"Russian officials blame Ukraine."

18:00
"Western officials investigate."

next day
"Satellite imagery suggests..."
```

This creates a **Narrative Evolution Timeline**.

The system should record:

> what changed, when it changed, and what caused the change.

---

# 18. Independent Evidence Score

CNVS may calculate an **Independent Evidence Score (IES)**.

Conceptually:

```text
IES =
independent evidence
──────────────────────
total supporting signals
```

The exact mathematical implementation may evolve.

The principle must remain constant:

> **independence matters more than volume.**

A claim with ten independent observations should score higher than a claim repeated by one hundred dependent sources.

---

# 19. Narrative Divergence Score

CNVS may calculate a **Narrative Divergence Score (NDS)** based on:

- attribution differences,
- omission differences,
- terminology differences,
- confidence differences,
- causal interpretation,
- temporal framing.

The score must not mean:

> "higher = propaganda."

It means:

> **higher = greater disagreement between information environments.**

Interpretation remains a human analytical task.

---

# 20. Source Quality

Source quality should consider:

- provenance,
- historical reliability,
- transparency,
- access to primary information,
- correction record,
- independence,
- specialization,
- conflicts of interest,
- and whether the source is reporting or merely repeating.

CNVS must never equate:

```text
famous source
=
reliable source
```

nor:

```text
official source
=
true source
```

---

# 21. AI Role

Large language models may be used for:

- extraction,
- classification,
- translation,
- clustering,
- summarization,
- contradiction detection,
- semantic comparison,
- narrative analysis.

LLMs must not be treated as primary evidence.

The system must preserve:

```text
RAW SOURCE
        ↓
STRUCTURED CLAIM
        ↓
MODEL INTERPRETATION
```

rather than:

```text
SOURCE
        ↓
LLM
        ↓
TRUTH
```

---

# 22. Human-in-the-Loop

High-impact assessments require human review.

Human analysts should be able to inspect:

- original source,
- source chain,
- extracted claim,
- supporting evidence,
- conflicting evidence,
- model reasoning artifacts,
- confidence score,
- and unresolved gaps.

The system must make disagreement visible rather than hide it.

---

# 23. Adversarial Environment

CNVS assumes that information environments may contain:

- deliberate disinformation,
- misinformation,
- selective disclosure,
- deception,
- manipulated imagery,
- recycled claims,
- fake sources,
- bot amplification,
- coordinated narratives,
- legitimate uncertainty,
- and honest reporting errors.

The existence of disagreement is not itself evidence of deception.

---

# 24. Core Output

A CNVS event report should produce:

```text
EVENT
FACTS
CLAIMS
SOURCE GRAPH
COUNTRY MATRIX
PRIMARY EVIDENCE
NARRATIVE COMPARISON
ATTRIBUTION STATUS
CONFIDENCE
CONTRADICTIONS
INFORMATION GAPS
TIMELINE
ASSESSMENT
```

---

# 25. Example Output

```text
EVENT:
Attack on two tankers in the Black Sea.

FACT:
Damage to two vessels is independently reported.

CONFIRMED:
Incident occurred.

CLAIMS:
Russia attributes incident to Ukraine.
Ukraine disputes responsibility.

SOURCE DEPENDENCY:
Majority of early reports trace to Russian official statement.

INDEPENDENT EVIDENCE:
Partial.

ATTRIBUTION:
UNRESOLVED.

NARRATIVE DIVERGENCE:
HIGH.

KEY GAP:
No independently verified attribution evidence.

ASSESSMENT:
The event is highly likely genuine.
Responsibility remains insufficiently established.

CONFIDENCE:
Event: HIGH
Method: MEDIUM
Attribution: LOW
```

---

# 26. Integration

CNVS is designed as a verification layer that may operate independently or integrate with other QHDALabs systems.

Potential integrations include:

```text
RSS / News APIs
        ↓
CNVS
        ↓
Source Graph
        ↓
Evidence / Narrative Analysis
        ↓
GEWS
        ↓
Strategic Early Warning
```

CNVS should also be capable of feeding structured evidence into:

- QHDALabs Sentinel,
- geopolitical monitoring,
- incident databases,
- research workflows,
- analytical dashboards,
- and human decision-support systems.

---

# 27. Governance

CNVS must be governed by the following principles:

### Transparency

The system must make clear why an assessment was produced.

### Reproducibility

Another analyst should be able to recreate the result from the recorded evidence.

### Traceability

Every important conclusion must be traceable to its sources.

### Correction

Incorrect assessments must be corrected without destroying the historical record.

### Uncertainty

Unknown must remain unknown.

### Independence

The system must not be optimized toward a predetermined political conclusion.

---

# 28. Political Neutrality

CNVS does not defend:

- NATO,
- Russia,
- Ukraine,
- the European Union,
- the United States,
- Poland,
- or any other political actor.

CNVS defends:

> **the integrity of the evidence chain.**

The objective is not to protect one narrative.

The objective is to protect the ability of analysts and citizens to independently determine:

- what happened,
- what is known,
- what is claimed,
- what is uncertain,
- and what remains to be established.

---

# 29. Failure Conditions

CNVS considers the following to be analytical failures:

```text
Repeated claim treated as independent confirmation.
Official statement treated as fact without qualification.
Attribution inferred from narrative alone.
Translation removes uncertainty.
Late information overwrites historical information state.
LLM output treated as evidence.
Missing evidence treated as evidence of absence.
Political preference influences confidence score.
```

---

# 30. Success Criteria

CNVS is successful when it allows an analyst to answer:

> **"What do different countries say?"**

but also, more importantly:

> **"Why do they say it?"**

and:

> **"Which parts can actually be independently demonstrated?"**

---

# 31. Final Principle

The system is built around a simple proposition:

> ### **Reality does not become more true because more people repeat the same story.**

CNVS therefore does not measure reality by volume.

It measures:

**provenance, independence, evidence, divergence, uncertainty and convergence.**

---

# 32. Project Motto

> **One event. Many narratives. One evidence chain.**

**QHDALabs Cross-National Verification System (CNVS)**  
*Verification before interpretation. Evidence before narrative.*
