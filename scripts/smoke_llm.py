"""Opt-in real-model integration check. Uses only disclosed synthetic/sample data."""
from pathlib import Path
import argparse
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdr.config import ROOT, Settings
from sdr.engine import Engine
from sdr.models import AnalyzeRequest
from sdr.store import Store, now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", nargs="+", default=["verified", "conditional", "dnc"])
    parser.add_argument("--output", default="eval/artifacts/live-smoke.json")
    parser.add_argument("--persist", action="store_true", help="Store sample calls in the app database for demonstration")
    args = parser.parse_args()
    settings = Settings()
    if not settings.api_key:
        parser.error("Configure SDR_LLM_API_KEY or DASHSCOPE_API_KEY first.")
    if not args.persist:
        settings.db_path = Path(tempfile.mkdtemp(prefix="sdr-live-check-")) / "smoke.sqlite"
    store = Store(settings.db_path)
    engine = Engine(store, settings)
    samples = {s["id"]: s for s in json.loads((ROOT / "data/samples.json").read_text())}
    report = {"kind": "real_llm_integration_check_on_demo_data", "created_at": now(), "results": []}
    for sid in args.samples:
        sample = samples[sid]
        req = AnalyzeRequest(transcript=sample["transcript"], title=sample["title"] + " · 模型实测",
                             mode="llm", data_mode=sample["data_mode"], idempotency_key="live-" + sid + "-" + now()).model_dump()
        record, _ = store.create(req, sid, "integration-check")
        record = engine.run(record["id"])
        r = record["result"]
        checks = {"processing": record["status"] == "completed", "level": r["intent"]["level"] == sample.get("expected_level")}
        if "expected_score" in sample:
            checks["score"] = r["intent"]["interval"] == [sample["expected_score"]] * 2
        if "expected_entity" in sample:
            checks["entity"] = r["entity"]["status"] == sample["expected_entity"]
        if "expected_dnc" in sample:
            checks["do_not_contact"] = r["extraction"]["do_not_contact"] == sample["expected_dnc"]
        item = {"sample": sid, "status": record["status"], "error": record["error"], "intent": r["intent"],
                "provider": r["provider"], "entity_status": r["entity"]["status"], "lead": r["lead"],
                "validation_issues": r["validation_issues"], "elapsed_ms": r["elapsed_ms"], "analysis_id": record["id"],
                "checks": checks, "passed": all(checks.values()), "versions": r["versions"]}
        report["results"].append(item)
        print(json.dumps({"sample": sid, "status": record["status"], "error": record["error"], "level": r["intent"]["level"],
                          "scores": r["intent"]["scores"], "issues": len(r["validation_issues"]), "elapsed_ms": r["elapsed_ms"]}, ensure_ascii=False), flush=True)
    engine.pool.shutdown(wait=True)
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(i["passed"] for i in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
