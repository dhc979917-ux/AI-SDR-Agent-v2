import json

import httpx
import pytest

from sdr.qcc_mcp import QccCache, QccError, QccMcpClient


def _tool_payload(payload):
    return {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}}


def _handler(search_payload, detail_payload=None, calls=None):
    def handler(request):
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body.get("method"))
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "qcc-mcp"}}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            name = body["params"]["name"]
            if name == "get_company_by_query":
                return httpx.Response(200, json=_tool_payload(search_payload))
            if name == "get_company_registration_info":
                return httpx.Response(200, json=_tool_payload(detail_payload or {}))
        return httpx.Response(500, json={"error": "unexpected"})
    return handler


SEARCH = {
    "匹配结果": "多候选",
    "检索关键字": "木里木外",
    "企业信息": [
        {"企业名称": "木里木外家具启东有限公司", "统一社会信用代码": "91320681MA1NCCF03D", "成立日期": "2017-02-03", "法定代表人名称": ["练峰"], "状态": "存续"},
        {"企业名称": "上海木里实业发展有限公司", "统一社会信用代码": "913101143013514618", "成立日期": "2014-04-17", "法定代表人名称": ["张贺"], "状态": "存续"},
    ],
}

DETAIL = {
    "企业名称": "木里木外家具启东有限公司",
    "统一社会信用代码": "91320681MA1NCCF03D",
    "法定代表人": "练峰",
    "登记状态": "存续（在营、开业、在册）",
    "所属地区": "江苏省南通市启东市",
    "国标行业": {"门类": "制造业", "大类": "家具制造业"},
    "注册地址": "启东高新技术产业开发区黄海路55号",
    "企业类型": "有限责任公司",
}


def test_search_parses_candidates_and_marks_multiple():
    client = QccMcpClient("https://qcc.invalid/mcp", "token", transport=httpx.MockTransport(_handler(SEARCH)), min_interval=0)
    result = client.search_company("木里木外")
    assert result.status == "multiple"
    assert [c["name"] for c in result.candidates] == ["木里木外家具启东有限公司", "上海木里实业发展有限公司"]
    assert result.candidates[0]["credit_code"] == "91320681MA1NCCF03D"
    assert result.candidates[0]["source_type"] == "qcc_mcp"
    assert result.candidates[0]["verified_registry"] is True


def test_detail_maps_registry_fields():
    client = QccMcpClient("https://qcc.invalid/mcp", "token", transport=httpx.MockTransport(_handler(SEARCH, DETAIL)), min_interval=0)
    detail = client.company_detail("91320681MA1NCCF03D")
    assert detail["企业名称"] == "木里木外家具启东有限公司"
    assert detail["所属地区"] == "江苏省南通市启东市"


def test_sse_response_is_supported():
    def handler(request):
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        payload = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps(SEARCH, ensure_ascii=False)}]}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n")
    client = QccMcpClient("https://qcc.invalid/mcp", "token", transport=httpx.MockTransport(handler), min_interval=0)
    assert client.search_company("木里木外").status == "multiple"


def test_auth_failure_is_reported():
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})
    client = QccMcpClient("https://qcc.invalid/mcp", "bad", transport=httpx.MockTransport(handler), min_interval=0)
    with pytest.raises(QccError) as exc:
        client.search_company("木里木外")
    assert exc.value.kind == "auth"


def test_malformed_payload_is_reported():
    def handler(request):
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "not-json"}]}})
    client = QccMcpClient("https://qcc.invalid/mcp", "token", transport=httpx.MockTransport(handler), min_interval=0)
    with pytest.raises(QccError) as exc:
        client.search_company("木里木外")
    assert exc.value.kind == "protocol"


def test_cache_avoids_duplicate_search_calls(tmp_path):
    calls = []
    cache = QccCache(tmp_path / "cache.json", ttl_days=30)
    client = QccMcpClient("https://qcc.invalid/mcp", "token", transport=httpx.MockTransport(_handler(SEARCH, calls=calls)), cache=cache, min_interval=0)
    client.search_company("木里木外")
    client.search_company("木里木外")
    assert calls.count("tools/call") == 1


def test_zero_ttl_disables_cache(tmp_path):
    cache = QccCache(tmp_path / "cache.json", ttl_days=0)
    cache.set("key", {"value": 1})
    assert cache.get("key") is None
