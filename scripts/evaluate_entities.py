"""Live QCC candidate-recall smoke evaluation. Not a blind business accuracy benchmark."""
from pathlib import Path
import argparse
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdr.config import ROOT, Settings
from sdr.entity_resolver import resolve_company
from sdr.store import now


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="eval/entity_cases.json")
    parser.add_argument("--output", default="eval/artifacts/entity-eval.json")
    args = parser.parse_args()
    cases = json.loads((ROOT / args.dataset).read_text())
    settings = Settings(entity_provider="qcc", qcc_timeout=15)
    url, token = settings.qcc_credentials()
    if not all((url, token)):
        print("QCC MCP 未配置，跳过真实企业评测。", file=sys.stderr)
        return 2
    rows = []
    for case in cases:
        fields = {
            "raw_company": {"value": case["query"]},
            "brand_name": {"value": None},
            "area": {"value": case.get("area", "")},
            "industry": {"value": case.get("industry", "")},
        }
        entity = resolve_company(fields, [], settings, "real")
        names = [item["name"] for item in entity.get("candidates", [])]
        selected = (entity.get("selected") or {}).get("name")
        top1 = selected or (names[0] if names else None)
        expected = case.get("expected")
        time.sleep(1.0)
        rows.append({
            "id": case["id"], "query": case["query"], "type": case["type"],
            "expected": expected, "status": entity.get("status"), "top1": top1,
            "top3": names[:3], "candidate_count": len(names),
            "hit1": bool(expected and top1 == expected),
            "hit3": bool(expected and expected in names[:3]),
            "recall": bool(expected and expected in names),
            "abstained": entity.get("status") in ("ambiguous", "unresolved", "lookup_failed"),
            "error": entity.get("error"),
        })
    expected_rows = [row for row in rows if row["expected"]]
    auto_rows = [row for row in rows if row["status"] in ("suggested", "confirmed")]
    report = {
        "kind": "live_entity_candidate_recall_smoke_not_blind_benchmark",
        "created_at": now(),
        "count": len(rows),
        "expected_count": len(expected_rows),
        "top1_accuracy": round(sum(row["hit1"] for row in expected_rows) / len(expected_rows), 4) if expected_rows else None,
        "top3_recall": round(sum(row["hit3"] for row in expected_rows) / len(expected_rows), 4) if expected_rows else None,
        "candidate_recall": round(sum(row["recall"] for row in expected_rows) / len(expected_rows), 4) if expected_rows else None,
        "abstention_rate": round(sum(row["abstained"] for row in rows) / len(rows), 4) if rows else None,
        "auto_confirmed_count": len(auto_rows),
        "auto_confirmed_accuracy": round(sum(row["hit1"] for row in auto_rows) / len(auto_rows), 4) if auto_rows else None,
        "rows": rows,
        "limitations": "小型真实企查查冒烟集，用例由开发时观察结果整理，不是冻结盲测集，不能当作真实业务准确率。",
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: report[key] for key in ("count", "expected_count", "top1_accuracy", "top3_recall",
                                                   "candidate_recall", "abstention_rate", "auto_confirmed_count",
                                                   "auto_confirmed_accuracy")}, ensure_ascii=False))
    for row in rows:
        if row["expected"] and not row["recall"]:
            print("MISS", row["id"], row["query"], "expected", row["expected"], "top3", row["top3"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
