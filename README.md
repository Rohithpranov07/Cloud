# Semantic Autoscaling Prototype — Core Prototype

> **Status: scaffold only (Phase 0 / T0.1 complete).** This README is a stub; it is
> filled in fully at task **T8.2**, once the end-to-end run produces real results.

A local Python simulation testing one claim: conditioning cloud autoscaling and
cost-governance decisions on the **semantic archetype** of incoming AI inference requests
(not just aggregate request rate) reduces simulated latency-SLA violations and/or improves
cost-budget adherence, versus an archetype-agnostic baseline modeled on the STAR base
paper's formulation.

Course: BCSE355L — Cloud Architecture Design.
Specs: [`PRD.md`](PRD.md) (what/why), [`TRD.md`](TRD.md) (exact contracts — **the TRD wins**
any disagreement), [`Build-Instructions.md`](Build-Instructions.md) (task-by-task build).

## Setup

Requires Python 3.11+ (verified on 3.14.7).

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

In VS Code: `Ctrl+Shift+P` → "Python: Select Interpreter" → pick the `.venv` one.

## Running

Nothing is implemented yet beyond the scaffold. Once Phases 1–7 land, the single
end-to-end entry point is:

```bash
python -m src.evaluation.run_comparison
```

which writes `data/evaluation_results.csv` and `data/capacity_comparison.png`.

## Tests

```bash
pytest -v
mypy src/
```

## Layout

See `CLAUDE.md` → "Project map", or `TRD.md` §3. Configuration constants live in
`configs/default.yaml`; generated artifacts land in `data/` (gitignored).
