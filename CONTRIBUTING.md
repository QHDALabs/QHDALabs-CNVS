# Contributing

Thank you for helping improve CNVS. The project is currently an architecture
scaffold; contributions should make that status clear and should preserve the
analytical safeguards in `MANIFEST.md` and `ARCHITECTURE.md`.

## Before submitting

1. Check existing issues and pull requests for related work.
2. For material design changes, open an issue or discussion before investing
   in an implementation.
3. Keep changes focused and explain their impact on data schemas, provenance,
   uncertainty or analyst review.
4. Use synthetic data in tests and examples. Do not submit credentials,
   sensitive source material or private analyst data.

## Validation

Install the package in an isolated environment first (see
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)). For Python code and example
record changes, run:

```powershell
python -m pip install --constraint requirements.txt --editable .
python -m cnvs config validate
python -m unittest discover -s tests -v
```

Also ensure JSON examples remain valid and configuration changes preserve the
documented semantics. The current test suite checks fixture records,
configuration scope and runtime behavior; it does not validate analytical
conclusions.

## Pull requests

Describe the problem, the approach, relevant tests and any limitations. Call
out user-visible behavior or schema changes and update related documentation.
Do not describe unimplemented components as operational.
