from datetime import date, timedelta

import pytest

from sdr.config import Settings
from sdr.entity_resolver import query_variants, resolve_company
from sdr.entities import has_company
from sdr.qcc_mcp import QccError, QccSearchResult


def _candidate(name="木里木外家具启东有限公司", credit="91320681MA1NCCF03D"):
    return {
        "id": credit,
        "credit_code": credit,
        "name": name,
        "brand": "",
        "aliases": [],
        "role": "unknown",
        "role_hint": "主体待确认",
        "area": "",
        "industry": "",
        "status": "存续",
        "source_url": "",
        "source_title": "企查查 MCP",
        "source_type": "qcc_mcp",
        "observed_at": date.today().isoformat(),
        "valid_until": (date.today() + timedelta(days=30)).isoformat(),
        "data_mode": "real",
        "verified_registry": True,
        "relation_supported": False,
    }


def _fields(raw="木里木外", area="浙江省杭州市", industry="全屋定制"):
    return {
        "raw_company": {"value": raw},
        "brand_name": {"value": None},
        "area": {"value": area},
        "industry": {"value": industry},
    }


def _install(monkeypatch, result, detail=None, error=None):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def search_company(self, query):
            if error:
                raise error
            return result

        def company_detail(self, credit_code):
            return detail or {}

    monkeypatch.setattr("sdr.entity_resolver.QccMcpClient", FakeClient)


@pytest.fixture
def settings(tmp_path):
    return Settings(entity_provider="qcc", qcc_mcp_url="https://qcc.invalid/mcp", qcc_mcp_token="token",
                    qcc_cache_path=tmp_path / "cache.json", qcc_timeout=1)


def test_multiple_candidates_are_ambiguous_and_keep_raw_name(monkeypatch, settings):
    result = QccSearchResult("multiple", [_candidate(), _candidate("上海木里实业发展有限公司", "913101143013514618")], "木里木外", {})
    _install(monkeypatch, result)
    entity = resolve_company(_fields(), [], settings, "real")
    assert entity["status"] == "ambiguous"
    assert entity["selected"] is None
    assert entity["raw_name"] == "木里木外"
    assert entity["mention"] == "木里木外"
    assert len(entity["candidates"]) == 2
    assert has_company(entity) is False
    assert entity["provider"] == "qcc_mcp"


def test_exact_name_is_auto_confirmed_and_enriched(monkeypatch, settings):
    result = QccSearchResult("unique", [_candidate()], "木里木外家具启东有限公司", {})
    detail = {"企业名称": "木里木外家具启东有限公司", "统一社会信用代码": "91320681MA1NCCF03D",
              "登记状态": "存续", "所属地区": "江苏省南通市启东市", "国标行业": {"大类": "家具制造业"},
              "注册地址": "启东高新技术产业开发区黄海路55号", "法定代表人": "练峰"}
    _install(monkeypatch, result, detail)
    entity = resolve_company(_fields("木里木外家具启东有限公司", "江苏省南通市", "家具制造"), [], settings, "real")
    assert entity["status"] == "confirmed"
    assert entity["selected"]["name"] == "木里木外家具启东有限公司"
    assert entity["selected"]["credit_code"] == "91320681MA1NCCF03D"
    assert entity["selected"]["area"] == "江苏省南通市启东市"
    assert entity["selected"]["industry"] == "家具制造"
    assert entity["quality"] == 1.0
    assert entity["auto_confirmed"] is True
    assert entity["method"] == "qcc_mcp_exact"
    assert entity["selected"]["verification_method"] == "auto_exact_name"
    assert has_company(entity) is True
    assert entity["missing"] == []


def test_no_match_keeps_raw_name(monkeypatch, settings):
    _install(monkeypatch, QccSearchResult("none", [], "木里木外", {}))
    entity = resolve_company(_fields(), [], settings, "real")
    assert entity["status"] == "unresolved"
    assert entity["selected"] is None
    assert entity["mention"] == "木里木外"
    assert has_company(entity) is False


def test_qcc_failure_keeps_raw_name(monkeypatch, settings):
    _install(monkeypatch, None, error=QccError("timeout", "timeout"))
    entity = resolve_company(_fields(), [], settings, "real")
    assert entity["status"] == "lookup_failed"
    assert entity["selected"] is None
    assert entity["mention"] == "木里木外"
    assert "timeout" in entity["error"]


def test_provider_off_keeps_captured(tmp_path):
    settings = Settings(entity_provider="off", qcc_cache_path=tmp_path / "cache.json")
    entity = resolve_company(_fields(), [], settings, "real")
    assert entity["status"] == "captured"
    assert entity["selected"]["name"] == "木里木外"
    assert entity["provider"] == "off"


def test_query_variants_include_suffix_and_homophone():
    variants = query_variants("七木定制", "浙江省杭州市")
    assert "七木定制" in variants
    assert any("栖木" in value for value in variants)
    assert any("杭州" in value for value in variants)


def test_rerank_exact_full_name_auto_confirms(monkeypatch, settings):
    from sdr.entity_rerank import RerankError
    settings.api_key = "test-text-model"
    first = _candidate()
    second = _candidate("木里木外家具启东有限公司上海分公司", "91310120MACUU7E08Y")
    _install(monkeypatch, QccSearchResult("multiple", [second, first], "木里木外家具启东有限公司", {}),
             {"所属地区": "江苏省南通市启东市", "国标行业": {"大类": "家具制造业"}})
    monkeypatch.setattr("sdr.entity_resolver.rerank_candidates", lambda *a, **kw: {
        "items": {
            first["credit_code"]: {"credit_code": first["credit_code"], "match_type": "exact", "confidence": .96, "reason": "客户口述全称与候选一致", "role": "brand_owner"},
            second["credit_code"]: {"credit_code": second["credit_code"], "match_type": "related", "confidence": .2, "reason": "分支机构，不是客户口述主体", "role": "branch"},
        },
        "top": first["credit_code"], "model": "test-text-model", "note": "",
    })
    entity = resolve_company(_fields("木里木外家具启东有限公司", "江苏省南通市", "家具制造"), [], settings, "real")
    assert entity["status"] == "confirmed"
    assert entity["auto_confirmed"] is True
    assert entity["method"] == "qcc_mcp_rerank"
    assert entity["ranker"] == "llm"
    assert entity["rerank_model"] == "test-text-model"
    assert entity["selected"]["credit_code"] == first["credit_code"]
    assert entity["candidates"][0]["rerank_rank"] == 1
    assert entity["candidates"][1]["rerank_match"] == "related"


def test_rerank_brand_only_does_not_auto_confirm(monkeypatch, settings):
    settings.api_key = "test-text-model"
    first = _candidate()
    second = _candidate("上海木里实业发展有限公司", "913101143013514618")
    _install(monkeypatch, QccSearchResult("multiple", [first, second], "木里木外", {}))
    monkeypatch.setattr("sdr.entity_resolver.rerank_candidates", lambda *a, **kw: {
        "items": {
            first["credit_code"]: {"credit_code": first["credit_code"], "match_type": "brand", "confidence": .9, "reason": "品牌主体，客户只说了品牌", "role": "brand_owner"},
            second["credit_code"]: {"credit_code": second["credit_code"], "match_type": "related", "confidence": .3, "reason": "同品牌其他主体", "role": "unknown"},
        },
        "top": first["credit_code"], "model": "test-text-model", "note": "",
    })
    entity = resolve_company(_fields("木里木外", "浙江省杭州市", "全屋定制"), [], settings, "real")
    assert entity["status"] == "ambiguous"
    assert entity["auto_confirmed"] is False
    assert entity["selected"] is None
    assert entity["candidates"][0]["rerank_match"] == "brand"


def test_rerank_failure_falls_back_to_qcc_order(monkeypatch, settings):
    from sdr.entity_rerank import RerankError
    settings.api_key = "test-text-model"
    first = _candidate()
    second = _candidate("上海木里实业发展有限公司", "913101143013514618")
    _install(monkeypatch, QccSearchResult("multiple", [first, second], "木里木外", {}))
    def fail(*args, **kwargs):
        raise RerankError("model unavailable")
    monkeypatch.setattr("sdr.entity_resolver.rerank_candidates", fail)
    entity = resolve_company(_fields("木里木外", "浙江省杭州市", "全屋定制"), [], settings, "real")
    assert entity["status"] == "ambiguous"
    assert entity["ranker"] == "qcc_order"
    assert "model unavailable" in entity["rerank_error"]


def test_rerank_allows_city_parenthesis_variant_auto_confirm(monkeypatch, settings):
    settings.api_key = "test-text-model"
    first = _candidate("杭州青禾空间设计有限公司", "91330102MA7HPPF138")
    second = _candidate("杭州青禾云杭科技有限公司", "91330110MAEGTYWB1B")
    _install(monkeypatch, QccSearchResult("multiple", [first, second], "青禾空间设计（杭州）有限公司", {}),
             {"所属地区": "浙江省杭州市", "国标行业": {"大类": "专业技术服务业"}})
    monkeypatch.setattr("sdr.entity_resolver.rerank_candidates", lambda *a, **kw: {
        "items": {
            first["credit_code"]: {"credit_code": first["credit_code"], "match_type": "exact", "confidence": .95, "reason": "括号与城市位置不同，仍为同一全称", "role": "brand_owner"},
            second["credit_code"]: {"credit_code": second["credit_code"], "match_type": "unrelated", "confidence": .1, "reason": "名称不同", "role": "unknown"},
        },
        "top": first["credit_code"], "model": "test-text-model", "note": "",
    })
    entity = resolve_company(_fields("青禾空间设计（杭州）有限公司", "浙江省杭州市", "全屋定制"), [], settings, "real")
    assert entity["status"] == "confirmed"
    assert entity["auto_confirmed"] is True
    assert entity["selected"]["credit_code"] == "91330102MA7HPPF138"
