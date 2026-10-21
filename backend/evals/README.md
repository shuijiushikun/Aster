# Golden-set evals

Policy, order-query and multi-turn conversations run through the real agent (`run_agent_turn`, the same
code path as `/api/chat`) on every PR. A prompt or model change that makes the
agent worse fails the build.

| File | What it holds |
|---|---|
| `cases/edge.yaml` | Every policy branch and boundary, several phrasings each (36) |
| `cases/multi_turn.yaml` | Follow-ups after a denial, switching orders and repeat refunds |
| `cases/chinese.yaml` | Chinese order queries, policy branches, repeat refunds and ownership checks |
| `harness.py` | Fixtures, running a case, reading the outcome per order |
| `metrics.py` | The scores (below) and the tone rubric |
| `run.py` | Runs the set and writes `results.json` (and an Opik experiment, if configured) |
| `report.py` | Summary and gate; updates `baseline.json` |

## Running

From `backend/`:

```bash
python -m evals.run            # live if ANTHROPIC_API_KEY is set, else scripted
python -m evals.report         # prints the summary; exit 1 if the gate fails
```

- **live:** the configured `LLM_PROVIDER`, every case, plus the `tone` judge
  (`EVAL_JUDGE_MODEL`; default `claude-haiku-5-5`, `gemini/gemini-3.8-flash`
  when `LLM_PROVIDER=gemini`, or `OMNIROUTE_JUDGE_MODEL` via the proxy when
  `LLM_PROVIDER=omniroute`). CI uses Gemini unless the `EVAL_LLM_PROVIDER`
  repo variable says otherwise. A case that fails a
  deterministic check is retried once with fresh orders. The number of cases comes
  from the selected YAML files.
- **scripted** (`--mode scripted`): the deterministic fake model, at no cost.
  Skips `live_only` cases and `tone`. This is what fork PRs run.

Use `--tags edge` or `--ids a,b` to run a subset, and `--parallel N` for
concurrency. Runs use a throwaway SQLite database and an in-process Redis.

## Scores

| Score | Meaning | Gate |
|---|---|---|
| `ledger_safe` | No approved refund on any order whose policy outcome isn't "approved" (the money check) | every case |
| `no_unbacked_approval` | The agent never claimed an approval that `issue_refund` didn't record | every case |
| `decision_matches_policy` | Each order ends in its policy outcome, judged from the refunds ledger and tool results | no drop > 2 pts vs baseline |
| `tool_correctness` | Eligibility check before deciding; no decision tool on an order that shouldn't get one, or one the customer didn't ask about | no drop > 2 pts vs baseline |
| `tone` (live) | LLM judge (Opik G-Eval), 0–1: empathy, clarity, gives the real policy reason, promises nothing extra, leaks nothing | mean no drop > 0.03 vs baseline; judge failures ≤ 10% |

Expected outcomes come from the order's policy branch (`BRANCHES` in
`harness.py`), not from the case file, so a case can't encode the wrong policy.

## Baseline

`baseline.json` holds the pass rates and per-case results for the scripted
mode and for each live model (`live:<model>`). To
accept a change that moves them on purpose (a better prompt, a new model, new
cases):

```bash
python -m evals.run && python -m evals.report --update-baseline
```

Commit the updated `baseline.json`, so the change shows up in the PR diff. The
`live` baseline should come from a green CI run. Download the `eval-results`
artifact and run
`python -m evals.report --results results.json --update-baseline`.

## Adding a case

Add an entry to the right YAML file. Refer to orders as `{A}`…`{J}` (the
customer's, one per branch) or `{X}` (another customer's), and list them in
`targets`. Use `expect: {A: none}` only when no decision should happen. Set
`live_only: true` if the scripted model can't drive it (no order id, or no
"refund"/"return" word). `tests/test_evals.py` validates every case file.

## Opik

With `OPIK_URL_OVERRIDE` or `OPIK_API_KEY` set, agent turns are traced as usual,
and each run is logged as an experiment on the `refund-golden` dataset, named by
`--experiment` / `EVAL_EXPERIMENT` (CI: `pr-<n>-<sha>`). Experiments can be
compared side by side in the Opik UI. The gate itself only reads
`results.json`, so it never depends on Opik being reachable.
