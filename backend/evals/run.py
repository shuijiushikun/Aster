"""Run the golden set and write `evals/results.json`.

    python -m evals.run                     # live if the provider key is set, else scripted
    python -m evals.run --mode scripted     # LLM_PROVIDER=fake, no cost, no judge
    python -m evals.run --tags edge --parallel 4

- live: the configured provider (`LLM_PROVIDER`, default anthropic). Every case,
  plus the LLM-judged `tone` score (`EVAL_JUDGE_MODEL`; defaults to
  claude-haiku-5-5, or gemini-3.8-flash when LLM_PROVIDER=gemini).
  A case that fails a deterministic check is retried once with fresh orders, to
  absorb sampling noise.
- scripted: the deterministic fake model. Skips `live_only` cases and `tone`.

Uses a throwaway SQLite database and an in-process Redis, so it touches nothing
else. When Opik is configured (OPIK_*), agent turns are traced as usual and the
run is logged as an experiment on the `refund-golden` dataset. The gate in
`evals.report` reads only the local results file, so it never depends on Opik.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_OUT = Path(__file__).parent / "results.json"


def _provider_key_present() -> bool:
    provider = os.getenv("LLM_PROVIDER", "anthropic")
    return bool(os.getenv(f"{provider.upper()}_API_KEY"))


def _prepare_env(mode: str, db_dir: str) -> None:
    """Must run before anything imports `app` (settings are cached)."""
    os.environ["DATABASE_URL"] = f"sqlite:///{db_dir}/evals.db"
    os.environ["REDIS_URL"] = ""  # in-process fakeredis
    os.environ["SEED_ON_STARTUP"] = "false"
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    os.environ["LLM_FALLBACK_PROVIDER"] = ""  # measure the primary model only
    if mode == "scripted":
        os.environ["LLM_PROVIDER"] = "fake"
        os.environ["FAKE_LLM_LATENCY_MS"] = "0"
        os.environ["FAKE_LLM_ERROR_RATE"] = "0"
    if not (os.getenv("OPIK_URL_OVERRIDE") or os.getenv("OPIK_API_KEY")):
        # The judge goes through Opik's metric code; keep it fully offline.
        os.environ.setdefault("OPIK_TRACK_DISABLE", "true")


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return os.getenv("GITHUB_SHA", "unknown")


async def _run_all(cases, mode: str, parallel: int) -> list[dict]:
    from app.db import session as db
    from app.db.models import Base
    from evals.harness import run_case
    from evals.metrics import deterministic_scores, tone_input, tone_metric

    engine = db.make_engine()
    db.configure(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    judge = tone_metric() if mode == "live" else None
    sem = asyncio.Semaphore(parallel)
    done = 0

    async def one(case) -> dict:
        nonlocal done
        async with sem:
            run = await run_case(case)
            scores = deterministic_scores(run)
            retried = False
            if mode == "live" and any(s.value < 1 for s in scores.values()):
                retried = True
                run = await run_case(case)
                scores = deterministic_scores(run)
            out = {name: {"value": s.value, "reason": s.reason} for name, s in scores.items()}
            if judge is not None:
                try:
                    r = await judge.ascore(output=tone_input(run))
                    out["tone"] = {"value": round(float(r.value), 3), "reason": r.reason}
                except Exception as exc:
                    out["tone"] = {"value": None, "reason": f"judge failed: {type(exc).__name__}: {exc}"[:300]}
        done += 1
        failed = [n for n, s in out.items() if s["value"] is not None and s["value"] < 1 and n != "tone"]
        print(f"[{done}/{len(cases)}] {case.id}: {'FAIL ' + ','.join(failed) if failed else 'ok'}", flush=True)
        tokens_in, tokens_out = run.tokens()
        return {
            "id": case.id,
            "tags": case.tags,
            "targets": case.targets,
            "expected": {k: sorted(case.expected(k)) for k in case.targets},
            "outcomes": {k: run.outcome(k) for k in case.targets},
            "turns": [{"user": t["user"], "assistant": t["assistant"]} for t in run.turns],
            "tool_calls": [
                {"tool": c["tool"], "order_id": (c.get("args") or {}).get("order_id")}
                for c in run.tool_calls()
            ],
            "ledger": run.ledger,
            "error": run.error,
            "scores": out,
            "retried": retried,
            "trace_ids": run.trace_ids(),
            "tokens": {"input": tokens_in, "output": tokens_out},
        }

    try:
        return await asyncio.gather(*(one(c) for c in cases))
    finally:
        from app import redis as shared

        await shared.close_redis()
        await engine.dispose()


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """List-price estimate for the agent's tokens (cache reads billed at full
    price, judge excluded), so an upper bound."""
    try:
        import litellm

        if model not in litellm.model_cost:
            return None
        cin, cout = litellm.cost_per_token(
            model=model, prompt_tokens=tokens_in, completion_tokens=tokens_out
        )
        return round(cin + cout, 4)
    except Exception:
        return None


def _log_to_opik(results: dict, cases) -> str | None:
    """Record the run as an Opik experiment over the `refund-golden` dataset."""
    from opik.evaluation import evaluate
    from opik.evaluation.metrics import base_metric, score_result

    from app import tracing
    from app.config import get_settings

    client = tracing._get_client()
    if client is None:
        return None

    class Precomputed(base_metric.BaseMetric):
        """Reports a score the harness already computed."""

        def __init__(self, name: str):
            super().__init__(name=name, track=False)

        # evaluate() passes the task output's keys as keyword arguments.
        def score(self, scores, **_ignored):
            s = scores.get(self.name) or {}
            if s.get("value") is None:
                return score_result.ScoreResult(
                    name=self.name, value=0.0, reason=s.get("reason", "not scored"), scoring_failed=True
                )
            return score_result.ScoreResult(name=self.name, value=s["value"], reason=s["reason"])

    by_id = {r["id"]: r for r in results["cases"]}
    dataset = client.get_or_create_dataset(
        "refund-golden", description="Golden conversations for the refund agent (backend/evals)."
    )
    rows = [{"case_id": c.id, "tags": c.tags, "turns": c.turns, "targets": c.targets} for c in cases]
    dataset.insert(rows)  # identical rows are de-duplicated by Opik
    current = {json.dumps(r, sort_keys=True) for r in rows}
    item_ids = [
        item["id"]
        for item in dataset.get_items()
        if json.dumps({k: item.get(k) for k in ("case_id", "tags", "turns", "targets")}, sort_keys=True)
        in current
    ]
    names = sorted({n for r in results["cases"] for n in r["scores"]})
    result = evaluate(
        dataset=dataset,
        task=lambda item: {
            "transcript": by_id[item["case_id"]]["turns"],
            "outcomes": by_id[item["case_id"]]["outcomes"],
            "scores": by_id[item["case_id"]]["scores"],
            "agent_trace_ids": by_id[item["case_id"]]["trace_ids"],
        },
        scoring_metrics=[Precomputed(n) for n in names],
        experiment_name=results["experiment"],
        experiment_config={
            k: results[k] for k in ("mode", "provider", "model", "prompt_version", "git_sha")
        },
        project_name=f"{get_settings().opik_project_name}-evals",
        dataset_item_ids=item_ids,
        task_threads=4,
        verbose=0,
    )
    client.flush()
    url = result.experiment_url
    override = get_settings().opik_url_override
    if url and override and "comet.com" not in override:
        # The SDK builds Comet-cloud paths (/opik/<workspace>/...); a self-hosted
        # UI serves them from the root.
        url = url.replace("/opik/", "/", 1)
    return url


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["auto", "live", "scripted"], default="auto")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--tags", help="comma-separated: only cases with any of these tags")
    ap.add_argument("--ids", help="comma-separated case ids")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--experiment", default=os.getenv("EVAL_EXPERIMENT"))
    ap.add_argument("--no-opik", action="store_true", help="don't log an Opik experiment")
    args = ap.parse_args()

    mode = args.mode
    if mode == "auto":
        mode = "live" if _provider_key_present() else "scripted"
    if mode == "live" and not _provider_key_present():
        print("--mode live needs the provider's API key.", file=sys.stderr)
        return 2

    db_dir = tempfile.mkdtemp(prefix="evals-")
    _prepare_env(mode, db_dir)

    from app.agent.llm import primary_model_name
    from app.agent.prompts import prompt_version
    from app.config import get_settings
    from evals.harness import load_cases
    from evals.metrics import judge_model

    cases = load_cases()
    if args.tags:
        wanted = set(args.tags.split(","))
        cases = [c for c in cases if wanted & set(c.tags)]
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c.id in wanted]
    skipped = [c.id for c in cases if mode == "scripted" and c.live_only]
    cases = [c for c in cases if c.id not in skipped]

    settings = get_settings()
    sha = _git_sha()
    print(f"Running {len(cases)} cases ({mode}, {settings.llm_provider}), skipping {len(skipped)} live-only")
    started = time.time()
    case_results = asyncio.run(_run_all(cases, mode, args.parallel))

    tokens_in = sum(r["tokens"]["input"] for r in case_results)
    tokens_out = sum(r["tokens"]["output"] for r in case_results)
    model = primary_model_name()
    results = {
        "mode": mode,
        "provider": settings.llm_provider,
        "model": model,
        "judge_model": judge_model() if mode == "live" else None,
        "prompt_version": prompt_version(),
        "git_sha": sha,
        "experiment": args.experiment or f"{mode}-{sha[:7]}-{int(started)}",
        "duration_s": round(time.time() - started, 1),
        "tokens": {"input": tokens_in, "output": tokens_out},
        "est_cost_usd": _estimate_cost(model, tokens_in, tokens_out) if mode == "live" else 0.0,
        "skipped": skipped,
        "cases": sorted(case_results, key=lambda r: r["id"]),
        "opik_experiment_url": None,
    }
    if settings.opik_enabled and not args.no_opik:
        try:
            results["opik_experiment_url"] = _log_to_opik(results, cases)
        except Exception as exc:  # reporting must not break the gate
            print(f"warning: Opik experiment logging failed: {exc}", file=sys.stderr)

    args.out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"Wrote {args.out} in {results['duration_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
