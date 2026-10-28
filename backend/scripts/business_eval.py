"""在自动创建的临时 PostgreSQL 库中回放业务评测，结束后删除该临时库。"""

import argparse
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.config import BACKEND_ROOT, get_settings
from evals.datasets import BUSINESS_PATH, fingerprint, load_business_cases


@contextmanager
def disposable_database(database_url):
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("业务评测需要已配置的 PostgreSQL；不会使用现有业务库")
    name = "aster_eval_" + uuid4().hex[:16]
    admin_url = url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        print("临时评测数据库：" + name, flush=True)
        try:
            yield url.set(database=name).render_as_string(hide_password=False)
        finally:
            # 只删除本次上下文刚创建、名称由程序生成的库。
            assert re.fullmatch(r"aster_eval_[0-9a-f]{16}", name)
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            print("已删除本次临时评测数据库：" + name, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--out", type=Path, default=BACKEND_ROOT / "var/business-evaluation.json")
    parser.add_argument("--case", action="append", help="只回放指定样本 ID，可重复传入")
    args = parser.parse_args()
    if not args.live:
        parser.error("需要 --live：调用真实模型并创建一个隔离测试库，不操作已有业务库")
    cases = load_business_cases()
    ids = {case.id for case in cases if case.suite == "current"}
    if args.case and not set(args.case) <= ids:
        parser.error("--case 必须为 current 场景 ID")
    args.out = args.out.resolve()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "dataset_sha256": fingerprint(BUSINESS_PATH),
        "selected_cases": args.case or sorted(ids),
        "dependencies": {"database": "temporary PostgreSQL", "checkpoint": "real PostgreSQL Saver",
                         "mcp": "real SDK over in-process ASGI", "redis": "fakeredis",
                         "generation": "configured live model", "intent": "configured live Qwen",
                         "retrieval": "configured live Qdrant and Bailian"},
        "known_gaps": [{"id": c.id, "gap": c.known_gap} for c in cases if c.suite == "target"],
        "rows": [],
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with disposable_database(get_settings().database_url) as test_url:
        os.environ.update(TEST_DATABASE_URL=test_url, TEST_REDIS_URL="", ASTER_LIVE_BUSINESS_EVAL="1",
                          ASTER_BUSINESS_REPORT=str(args.out), ASTER_BUSINESS_CASES=",".join(args.case or []))
        import pytest

        result = pytest.main([str(BACKEND_ROOT / "tests/test_business_live.py"), "-q", "-s", "--tb=short"])
    report = json.loads(args.out.read_text(encoding="utf-8"))
    report["summary"] = {"executed": len(report["rows"]),
                         "passed": sum(row["passed"] for row in report["rows"]),
                         "failed": sum(not row["passed"] for row in report["rows"]),
                         "not_executed": len(report["selected_cases"]) - len(report["rows"]),
                         "known_gaps": len(report["known_gaps"]), "pytest_exit_code": int(result)}
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
