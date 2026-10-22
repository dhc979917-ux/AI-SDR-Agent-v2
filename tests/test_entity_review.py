from datetime import date, timedelta

import pytest

from sdr.models import ReviewRequest
from sdr.review import ReviewError, ReviewService


def _entity():
    return {
        "status": "ambiguous",
        "selected": None,
        "mention": "青禾空间设计",
        "raw_name": "青禾空间设计",
        "candidates": [{
            "id": "91330102MA7HPPF138",
            "credit_code": "91330102MA7HPPF138",
            "name": "杭州青禾空间设计有限公司",
            "brand": "青禾空间",
            "aliases": ["青禾空间"],
            "role": "unknown",
            "role_hint": "品牌主体候选",
            "area": "浙江省杭州市",
            "industry": "全屋定制",
            "status": "存续",
            "source_url": "",
            "source_title": "企查查 MCP",
            "source_type": "qcc_mcp",
            "observed_at": date.today().isoformat(),
            "valid_until": (date.today() + timedelta(days=30)).isoformat(),
            "data_mode": "real",
            "verified_registry": True,
            "relation_supported": False,
            "name_match": .9,
            "rank_score": .8,
            "match_reason": "名称相似；品牌/简称匹配；登记状态存续",
        }],
        "quality": .5,
        "provider": "qcc_mcp",
        "resolution_confidence": .8,
        "query": "青禾空间设计",
        "query_variants": ["青禾空间设计"],
        "fetched_at": None,
        "error": None,
        "missing": ["多个候选，请人工确认客户经营主体"],
        "method": "qcc_mcp_search",
    }


def _install(monkeypatch):
    monkeypatch.setattr("sdr.engine.resolve_company", lambda fields, turns, settings, data_mode: _entity())


def test_select_qcc_candidate_confirms_full_name_and_requires_second_review(system, settings, monkeypatch):
    _install(monkeypatch)
    store, _, analyze = system
    row = analyze("verified")
    assert row["result"]["entity"]["status"] == "ambiguous"
    assert row["result"]["lead"]["company_name"] == "青禾空间设计"
    assert "ENTITY_AMBIGUOUS" in row["result"]["blockers"]
    service = ReviewService(store, settings)
    out = service.apply(row["id"], ReviewRequest(expected_revision=1, action="save",
                         entity_id="91330102MA7HPPF138"), "A")
    assert out["result"]["entity"]["status"] == "confirmed"
    assert out["result"]["lead"]["company_name"] == "杭州青禾空间设计有限公司"
    assert "ENTITY_AMBIGUOUS" not in out["result"]["blockers"]
    assert out["result"]["review"]["stage"] == "secondary"
    with pytest.raises(ReviewError):
        service.apply(row["id"], ReviewRequest(expected_revision=2, action="approve", reason="同一人批准"), "A")
    final = service.apply(row["id"], ReviewRequest(expected_revision=2, action="second_review",
                          reason="独立核验企业抬头与客户关系"), "B")
    assert final["result"]["review"]["status"] == "approved"
    assert final["result"]["delivery_status"] == "ready"
    assert final["result"]["entity"]["selected"]["credit_code"] == "91330102MA7HPPF138"


def test_qcc_selection_does_not_require_relation_note(system, settings, monkeypatch):
    _install(monkeypatch)
    store, _, analyze = system
    row = analyze("verified")
    service = ReviewService(store, settings)
    out = service.apply(row["id"], ReviewRequest(expected_revision=1, action="save",
                         entity_id="91330102MA7HPPF138"), "A")
    assert out["result"]["entity"]["status"] == "confirmed"
    assert out["result"]["review"]["stage"] == "secondary"


def _suggested_entity():
    entity = _entity()
    entity.update(status="suggested", selected=entity["candidates"][0], quality=.85,
                  missing=["企业全称由企查查召回，客户与该主体的关系待人工确认"])
    return entity


def test_confirm_suggested_candidate_still_requires_second_review(system, settings, monkeypatch):
    monkeypatch.setattr("sdr.engine.resolve_company", lambda fields, turns, settings, data_mode: _suggested_entity())
    store, _, analyze = system
    row = analyze("verified")
    assert row["result"]["entity"]["status"] == "suggested"
    assert "ENTITY_UNCONFIRMED" in row["result"]["blockers"]
    assert row["result"]["lead"]["company_name"] == "杭州青禾空间设计有限公司"
    service = ReviewService(store, settings)
    out = service.apply(row["id"], ReviewRequest(expected_revision=1, action="save",
                         entity_id="91330102MA7HPPF138"), "A")
    assert out["result"]["entity"]["status"] == "confirmed"
    assert out["result"]["review"]["stage"] == "secondary"
