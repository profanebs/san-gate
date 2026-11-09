# san-gate 2.0 · working plan

Status: experiment in progress (2026-11-09)

## Goal

Build a local, dependency-light gate for requirement documents that:

1. separates visible text from hidden/auxiliary layers (PDF, DOCX, HTML, TXT);
2. detects compliance canaries: instructions that ask the reader to do X and hide that X was done;
3. reports findings in one batch, with evidence and location, before any writing starts;
4. keeps false positives low enough that normal requirements stay green.

## Non-goals

- Not an AI-text detector.
- Not a full prompt-injection classifier for every possible payload.
- Not a sandbox for executing untrusted code.
- Not a replacement for the existing san-gate artifact scanner; this is the semantic/requirements layer.

## Acceptance criteria (pre-defined, do not move after seeing results)

| Category | Metric | Target |
|---|---|---|
| classic injection | RED rate | 100% |
| visible canary | RED rate | >= 70% |
| visible canary | flagged (RED or YELLOW) | >= 95% |
| hidden canary | RED rate | >= 90% |
| hidden canary | flagged | >= 95% |
| held-out canary | flagged | >= 80% |
| benign visible requirements | RED rate | <= 5% |
| benign visible requirements | flagged | <= 10% |
| benign legitimate hidden text | RED rate | <= 5% |
| red-team canary | flagged | >= 95% |
| red-team benign | RED rate | <= 10% |
| latency | mean per document | < 2 s |

## Rounds

- R1: baseline rules + fixtures.
- R2: fix misses and false positives found in R1; same fixed eval set.
- R3: held-out canary family, not used for tuning; measure generalization.
- R4: optional hardening if a category is still below target.

## Deliverables

- `sangate2/` package and CLI.
- `fixtures/` generated corpus.
- `eval/` runner.
- `reports/eval-r2.md` / `reports/eval-r3-holdout.md` / `reports/real-benign.md`.
- `docs/design.md`, `docs/experiments.md`.
- demo report for a canary-style requirement document.
