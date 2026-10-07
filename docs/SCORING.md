# Scoring and Confidence

CNVS scores are analytical aids, not truth labels. The manifest distinguishes
independent evidence from reporting volume and defines narrative divergence as
disagreement between information environments, not as a propaganda score.

## Indicators

- **Source reliability** considers provenance, transparency, correction
  history, expertise, access to primary information and conflicts of interest.
- **Independence** describes whether evidence chains have genuinely separate
  origins.
- **Evidence convergence** describes how strongly independent evidence supports
  an observation or claim.
- **Narrative divergence** compares attribution, omissions, terminology,
  confidence, causal framing and temporal framing.
- **Confidence vector** keeps confidence in occurrence, method and attribution
  separate.

## Current status

The values in `config/scoring.yaml` are categorical labels and policy reminders,
not calibrated weights or computed scores. No numeric formula is enabled in
this scaffold. Any future formula must document its inputs, assumptions,
version, limitations and validation results before operational use.

Do not infer reliability from a source's fame or official status. Do not treat
high divergence as proof of manipulation, or a large number of dependent
reports as independent corroboration. Allow “unknown” and “unresolved” as valid
analytical outcomes.
