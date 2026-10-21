"""Summarise `evals/results.json`, compare with the baseline, and gate the build.

    python -m evals.report                      # exit 1 if the gate fails
    python -m evals.report --update-baseline    # accept these results as the new baseline

Gate:
- Hard floors, every case: `ledger_safe` and `no_unbacked_approval`.
- No regression versus `evals/baseline.json` (same mode: live or scripted):
  `decision_matches_policy` and `tool_correctness` pass rates may drop at most
  RATE_TOLERANCE, the mean `tone` score at most TONE_TOLERANCE.
- Live runs: at most MAX_JUDGE_FAILURES of cases without a tone score.

Baselines are kept per mode, and live ones per model (`live:<model>`), since
models differ. Without a matching baseline only the floors apply (with a warning).
The Markdown summary also goes to $GITHUB_STEP_SUMMARY when it's set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from evals.metrics import DETERMINISTIC, FLOORS

HERE = Path(__file__).parent
RATE_TOLERANCE = 0.02
TONE_TOLERANCE = 0.03
MAX_JUDGE_FAILURES = 0.10
# Above this share of cases ending in an agent error, the run says nothing
# about quality: the model provider was down. Report that, not a regression.
MAX_AGENT_ERRORS = 0.25
REGRESSION_METRICS = ("decision_matches_policy", "tool_correctness")


def baseline_key(results: dict) -> str:
    return "scripted" if results["mode"] == "scripted" else f"live:{results['model']}"


def _passed(case: dict) -> bool:
    return all(case["scores"][m]["value"] == 1 for m in DETERMINISTIC)


def summarise(results: dict) -> dict:
    cases = results["cases"]
    n = len(cases) or 1
    rates = {m: sum(c["scores"][m]["value"] == 1 for c in cases) / n for m in DETERMINISTIC}
    tones = [c["scores"]["tone"]["value"] for c in cases if "tone" in c["scores"]]
    scored = [t for t in tones if t is not None]
    tags: dict[str, list[dict]] = {}
    for c in cases:
        for t in c["tags"]:
            tags.setdefault(t, []).append(c)
    return {
        "rates": rates,
        "tone": sum(scored) / len(scored) if scored else None,
        "tone_missing": (len(tones) - len(scored)) / len(tones) if tones else 0.0,
        "cases": {c["id"]: _passed(c) for c in cases},
        "by_tag": {
            t: {m: sum(c["scores"][m]["value"] == 1 for c in cs) / len(cs) for m in DETERMINISTIC}
            | {"n": len(cs)}
            for t, cs in sorted(tags.items())
        },
    }


def gate(results: dict, summary: dict, baseline: dict | None) -> list[str]:
    """Reasons the build should fail (empty = pass)."""
    errored = [c["id"] for c in results["cases"] if c.get("error")]
    if len(errored) > MAX_AGENT_ERRORS * max(len(results["cases"]), 1):
        return [
            f"the agent errored on {len(errored)}/{len(results['cases'])} cases, so the model "
            "provider was probably unreachable; this run can't measure quality. Re-run when "
            "it's healthy (the safety floors are still checked below)."
        ] + [
            f"`{m}` must pass on every case"
            for m in FLOORS
            if any(c["scores"][m]["value"] != 1 for c in results["cases"])
        ]
    failures = []
    for m in FLOORS:
        bad = [c["id"] for c in results["cases"] if c["scores"][m]["value"] != 1]
        if bad:
            failures.append(f"`{m}` must pass on every case; failed: {', '.join(bad)}")
    if results["mode"] == "live" and summary["tone_missing"] > MAX_JUDGE_FAILURES:
        failures.append(f"tone judge failed on {summary['tone_missing']:.0%} of cases")
    if baseline:
        for m in REGRESSION_METRICS:
            now, base = summary["rates"][m], baseline["rates"][m]
            if now < base - RATE_TOLERANCE:
                failures.append(f"`{m}` regressed: {now:.1%} vs baseline {base:.1%}")
        if summary["tone"] is not None and baseline.get("tone") is not None:
            if summary["tone"] < baseline["tone"] - TONE_TOLERANCE:
                failures.append(f"`tone` regressed: {summary['tone']:.2f} vs baseline {baseline['tone']:.2f}")
    return failures


def _pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def markdown(results: dict, summary: dict, baseline: dict | None, failures: list[str]) -> str:
    status = "❌ failed" if failures else "✅ passed"
    lines = [
        f"## Refund agent evals: {status}",
        "",
        f"**{results['mode']}** · {results['provider']} `{results['model']}` · prompt `{results['prompt_version']}` "
        f"· {len(results['cases'])} cases ({len(results['skipped'])} live-only skipped) · {results['duration_s']}s",
    ]
    if results["mode"] == "live":
        cost = results.get("est_cost_usd")
        lines.append(
            f"Tokens: {results['tokens']['input']:,} in / {results['tokens']['output']:,} out"
            + (f" · agent cost ≤ ${cost:.2f} (list price; judge not included)" if cost is not None else "")
        )
    if results.get("opik_experiment_url"):
        lines.append(f"[Opik experiment]({results['opik_experiment_url']})")
    lines += ["", "| Metric | Now | Baseline | Gate |", "|---|---|---|---|"]
    base_rates = (baseline or {}).get("rates", {})
    for m in DETERMINISTIC:
        rule = "every case" if m in FLOORS else f"≥ baseline − {RATE_TOLERANCE:.0%}"
        lines.append(f"| `{m}` | {_pct(summary['rates'][m])} | {_pct(base_rates.get(m))} | {rule} |")
    if summary["tone"] is not None:
        base_tone = (baseline or {}).get("tone")
        lines.append(
            f"| `tone` (mean, 0–1) | {summary['tone']:.2f} | {'n/a' if base_tone is None else f'{base_tone:.2f}'} "
            f"| ≥ baseline − {TONE_TOLERANCE} |"
        )
    lines += ["", "| Tag | Cases | Decision | Tools | Ledger safe |", "|---|---|---|---|---|"]
    for t, r in summary["by_tag"].items():
        lines.append(
            f"| {t} | {r['n']} | {_pct(r['decision_matches_policy'])} | {_pct(r['tool_correctness'])} "
            f"| {_pct(r['ledger_safe'])} |"
        )
    if not baseline:
        lines += ["", f"⚠️ No `{baseline_key(results)}` baseline yet, so only the hard floors were checked. "
                  "Run `python -m evals.report --update-baseline` on a green run and commit `evals/baseline.json`."]
    if failures:
        lines += ["", "### Why it failed", *[f"- {f}" for f in failures]]
    if baseline:
        flipped = [i for i, ok in summary["cases"].items() if not ok and baseline["cases"].get(i)]
        if flipped:
            lines += ["", "### Newly failing (passed on the baseline)", *[f"- `{i}`" for i in flipped]]
    failing = [c for c in results["cases"] if not _passed(c)]
    if failing:
        lines += ["", "<details><summary>Failing cases</summary>", ""]
        for c in failing[:30]:
            reasons = "; ".join(
                f"{m}: {c['scores'][m]['reason']}" for m in DETERMINISTIC if c["scores"][m]["value"] != 1
            )
            lines.append(f"- `{c['id']}`{' (retried)' if c['retried'] else ''}: {reasons}")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=HERE / "results.json")
    ap.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    results = json.loads(args.results.read_text())
    summary = summarise(results)
    baselines = json.loads(args.baseline.read_text()) if args.baseline.exists() else {}
    key = baseline_key(results)
    baseline = baselines.get(key)

    if args.update_baseline:
        baselines[key] = {
            "prompt_version": results["prompt_version"],
            "model": results["model"],
            "git_sha": results["git_sha"],
            "rates": summary["rates"],
            "tone": summary["tone"],
            "cases": summary["cases"],
        }
        args.baseline.write_text(json.dumps(baselines, indent=2, sort_keys=True) + "\n")
        print(f"Updated the {key} baseline in {args.baseline}")
        return 0

    failures = gate(results, summary, baseline)
    md = markdown(results, summary, baseline, failures)
    print(md)
    if path := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write(md)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
