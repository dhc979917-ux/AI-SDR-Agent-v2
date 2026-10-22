import pytest

from sdr.models import AnalyzeRequest, Extraction
from sdr.queue import review_display
from sdr.store import Conflict


def _request(key):
    return AnalyzeRequest(transcript="销售：您好\n客户：我们想了解一下企业版软件。", title="停止测试",
                          mode="rules", data_mode="synthetic", idempotency_key=key).model_dump()


def test_cancelled_before_run_is_not_overwritten(system):
    store, engine, _ = system
    record, _ = store.create(_request("cancel-before-run"), "cancel-before-run", "A")
    store.cancel(record["id"], "A")
    engine.run(record["id"])
    row = store.get(record["id"])
    assert row["status"] == "cancelled"
    assert row["result"] is None
    assert review_display(row)["state"] == "cancelled"


def test_cancel_mid_run_keeps_cancelled_status(system, monkeypatch):
    store, engine, _ = system
    record, _ = store.create(_request("cancel-mid-run"), "cancel-mid-run", "A")

    def cancel_then_extract(turns):
        store.cancel(record["id"], "A")
        return Extraction(call_type="sales"), {"provider": "rules", "model": "test"}

    monkeypatch.setattr("sdr.engine.rule_extract", cancel_then_extract)
    engine.run(record["id"])
    row = store.get(record["id"])
    assert row["status"] == "cancelled"
    assert row["result"] is None
    assert "停止" in row["error"]


def test_completed_analysis_cannot_be_cancelled(system):
    store, _, analyze = system
    row = analyze("verified")
    with pytest.raises(Conflict):
        store.cancel(row["id"], "A")
