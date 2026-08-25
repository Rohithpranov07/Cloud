# Semantic Autoscaling Prototype — Claude Code Operating Contract (READ EVERY SESSION)

## What this project is
A local Python simulation that tests one claim: conditioning cloud autoscaling
and cost-governance decisions on the SEMANTIC ARCHETYPE of incoming AI
inference requests (not just aggregate request rate) reduces simulated
latency-SLA violations and/or improves cost-budget adherence, versus an
archetype-agnostic baseline modeled on the STAR base paper's formulation.
It is the reduction-to-practice evidence for a course project (BCSE355L)
and a companion patent disclosure. It runs entirely on a laptop — no AWS
account is required until the explicitly separate Stretch phase.

## GROUND TRUTH (never change without explicit human instruction)
- Data models, module contracts, repo structure, and acceptance gates are
  FIXED by `TRD.md`. Do not redesign them.
- The four archetypes are FIXED: short_conversational, long_context_rag,
  agentic_tool_using, batch_offline. Do not rename, add, or remove.
- Boundaries: trace/label/forecast/decide/govern/evaluate are separate
  modules under packages `src/trace_gen`, `src/classifier`,
  `src/forecaster`, `src/controller`, `src/cost_governance`,
  `src/feedback`, `src/evaluation`. A module may only import from modules
  earlier in that list, never sideways or from evaluation backward.

## ANTI-HALLUCINATION RULES (hard rules)
1. Never invent method names or return shapes for numpy, pandas,
   scikit-learn, or statsmodels. If unsure of an API's exact signature
   (e.g. `SimpleExpSmoothing`, `DecisionTreeClassifier.predict_proba`),
   check it in this session (docs or `help()`) before using it — do not guess.
2. Never assume a package or version exists. Check `requirements.txt`
   before importing something new; if you need a new dependency, propose
   it explicitly and pin its version before using it.
3. If a data model, function signature, or constant already exists in
   `TRD.md` §1–§2, IMPORT/REUSE it. Never create a second definition of
   the same concept (e.g. a second `ARCHETYPES` list, a second cost table).
4. If a requirement is ambiguous or underspecified, ask ONE clarifying
   question instead of assuming. State any unavoidable assumption
   explicitly in the commit message or PR description.
5. Do not fabricate test results, file contents, or command output. Run
   things and paste the real output.

## ANTI-DRIFT RULES (hard rules)
6. Only create/modify the files listed in the task's "Files you may touch".
   If you must touch another file, list it and explain BEFORE editing.
7. Do not refactor unrelated code. Do not add features not in the task.
   Do not "improve" the architecture beyond the task's scope.
8. Keep every data model byte-aligned with TRD §1. Field names must match
   exactly (e.g. `predicted_archetype`, not `predictedArchetype` or
   `archetype_pred`).

## QUALITY GATES (must hold at end of every task)
- Full type hints on every function in `src/`; `mypy src/` clean (or an
  inline comment explaining a specific, narrow exception).
- No bare `except:` blocks anywhere. Errors are raised or explicitly and
  visibly handled, never silently swallowed.
- Every stochastic function accepts and respects a `seed` parameter
  (NFR-2 in the TRD) — two runs with the same seed must be identical.
- New behaviour ships with a test in `tests/`.
- `pytest` passes clean for the whole `tests/` directory, not just the
  new test.

## WORKING METHOD (every task)
A. First output a SHORT PLAN: the files you'll create/modify and the
   approach. For any task touching >1 file, WAIT for "go" before writing
   (unless told to run autonomously).
B. Implement only what the task asks.
C. Run the task's VERIFY commands. Paste real output. If anything fails,
   fix it before claiming done. Do NOT mark done on unverified work.
D. Extend tests. New behaviour ships with a test.
E. Commit once, message: `feat(<TASKID>): <summary>` (or fix/chore as apt).

## DEFINITION OF DONE
A task is done only when: the code runs (no import/runtime errors), the
task's VERIFY block passes with pasted real output, `pytest` passes
repo-wide, `mypy src/` is clean (or has a documented narrow exception),
the data model matches TRD §1 exactly, and only the permitted files
changed.

---

# Project map

Repo structure (fixed by `TRD.md` §3):

```
semantic-autoscaling/
├── README.md
├── CLAUDE.md
├── requirements.txt
├── configs/
│   └── default.yaml
├── src/
│   ├── trace_gen/generator.py
│   ├── classifier/archetype_classifier.py
│   ├── forecaster/per_archetype_forecaster.py
│   ├── controller/baseline_policy.py
│   ├── controller/archetype_aware_policy.py
│   ├── cost_governance/budget_projector.py
│   ├── feedback/recalibration.py
│   └── evaluation/run_comparison.py
├── data/                      # generated at runtime, gitignored except .gitkeep
├── notebooks/exploration.ipynb
└── tests/
    ├── test_trace_gen.py
    ├── test_classifier.py
    ├── test_forecaster.py
    ├── test_controller.py
    ├── test_cost_governance.py
    └── test_evaluation_end_to_end.py
```

Fixed archetype enum (`TRD.md` §1.1):

```python
ARCHETYPES = ["short_conversational", "long_context_rag", "agentic_tool_using", "batch_offline"]
```
