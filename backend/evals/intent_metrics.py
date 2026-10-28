"""区分模型分类、规则降级与高风险错分。"""

LABELS = ("query", "refund", "exchange", "clarify")


def score_intents(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("没有可评分的意图样本")
    matrix = {expected: dict.fromkeys(LABELS, 0) for expected in LABELS}
    for row in rows:
        matrix[row["expected"]][row["actual"]] += 1
    correct = sum(row["actual"] == row["expected"] for row in rows)
    model_rows = [r for r in rows if r["source"] == "model"]
    read_rows = [r for r in rows if r["expected"] in ("query", "clarify")]
    unsafe = sum(r["actual"] in ("refund", "exchange") for r in read_rows)
    per_class = {}
    for label in LABELS:
        expected = sum(matrix[label].values())
        predicted = sum(matrix[other][label] for other in LABELS)
        per_class[label] = {"support": expected,
            "precision": matrix[label][label] / predicted if predicted else None,
            "recall": matrix[label][label] / expected if expected else None}
    return {"total": len(rows), "correct": correct, "label_accuracy": correct / len(rows),
        "model_only_accuracy": (sum((r.get("model_intent") or r["actual"]) == r["expected"] for r in model_rows) / len(model_rows)
                                if model_rows else None),
        "model_coverage": len(model_rows) / len(rows), "fallback_count": len(rows) - len(model_rows),
        "guard_count": sum(bool(r.get("guard_reason")) for r in rows),
        "raw_model_unsafe_write_count": sum((r.get("model_intent") or r["actual"]) in ("refund", "exchange")
                                             for r in read_rows if r["source"] == "model"),
        "unsafe_write_count": unsafe, "unsafe_write_rate": unsafe / len(read_rows) if read_rows else None,
        "confusion_matrix": matrix, "per_class": per_class}
