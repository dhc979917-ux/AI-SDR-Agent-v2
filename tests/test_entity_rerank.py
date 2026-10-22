import json

import pytest

from sdr.config import Settings
from sdr.entity_rerank import RerankError, rerank_candidates


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return {"choices": [{"message": {"content": json.dumps(self.payload, ensure_ascii=False)}}]}


class FakeClient:
    calls = 0
    payload = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        FakeClient.calls += 1
        return FakeResponse(FakeClient.payload)


def _settings(tmp_path):
    return Settings(api_key="test-key", model="test-model", qcc_cache_path=tmp_path / "cache.json", qcc_cache_days=30, qcc_timeout=1)


def _candidates():
    return [
        {"id": "A", "credit_code": "A", "name": "木里木外家具启东有限公司", "brand": "木里木外"},
        {"id": "B", "credit_code": "B", "name": "上海木里实业发展有限公司", "brand": "木里"},
    ]


def _fields():
    return {"area": {"value": "江苏省南通市"}, "industry": {"value": "家具制造"}}


def test_rerank_parses_and_caches(monkeypatch, tmp_path):
    FakeClient.calls = 0
    FakeClient.payload = {"candidates": [
        {"credit_code": "A", "match_type": "exact", "confidence": 0.96, "reason": "全称一致", "role": "brand_owner"},
        {"credit_code": "B", "match_type": "related", "confidence": 0.2, "reason": "其他主体", "role": "unknown"},
    ], "top": "A", "note": ""}
    monkeypatch.setattr("sdr.entity_rerank.httpx.Client", FakeClient)
    settings = _settings(tmp_path)
    first = rerank_candidates("木里木外家具启东有限公司", _fields(), [], _candidates(), settings)
    second = rerank_candidates("木里木外家具启东有限公司", _fields(), [], _candidates(), settings)
    assert first["items"]["A"]["match_type"] == "exact"
    assert first["top"] == "A"
    assert first["model"] == "test-model"
    assert second["cached"] is True
    assert FakeClient.calls == 1


def test_rerank_rejects_unknown_candidate(monkeypatch, tmp_path):
    FakeClient.payload = {"candidates": [
        {"credit_code": "A", "match_type": "exact", "confidence": 0.9, "reason": "全称一致", "role": "brand_owner"},
        {"credit_code": "C", "match_type": "related", "confidence": 0.2, "reason": "虚构主体", "role": "unknown"},
    ], "top": "A"}
    monkeypatch.setattr("sdr.entity_rerank.httpx.Client", FakeClient)
    with pytest.raises(RerankError):
        rerank_candidates("木里木外家具启东有限公司", _fields(), [], _candidates(), _settings(tmp_path))


def test_rerank_skips_without_text_model(tmp_path):
    settings = _settings(tmp_path)
    settings.api_key = ""
    assert rerank_candidates("木里木外", _fields(), [], _candidates(), settings) is None
