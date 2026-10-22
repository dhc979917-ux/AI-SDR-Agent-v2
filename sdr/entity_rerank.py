from __future__ import annotations

from typing import Literal
import hashlib
import json
import re

import httpx
from pydantic import ConfigDict, Field

from .config import ROOT
from .model_transport import json_chat_body, provider_error
from .models import Model
from .preprocess import redact_for_model
from .qcc_mcp import QccCache


class RerankError(Exception):
    pass


class RerankItem(Model):
    model_config = ConfigDict(extra="forbid", strict=False)

    credit_code: str = Field(min_length=1, max_length=64)
    match_type: Literal["exact", "alias", "brand", "related", "unrelated"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=300)
    role: Literal["brand_owner", "dealer", "branch", "unknown"] = "unknown"


class RerankResponse(Model):
    model_config = ConfigDict(extra="forbid", strict=False)

    candidates: list[RerankItem] = Field(min_length=1, max_length=10)
    top: str | None = None
    note: str = Field(default="", max_length=300)


def _compact(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def rerank_candidates(raw, fields, turns, candidates, settings):
    if not settings.api_key or len(candidates) < 2:
        return None
    area = str(fields.get("area", {}).get("value") or "")
    industry = str(fields.get("industry", {}).get("value") or "")
    safe_turns, _ = redact_for_model(turns)
    context = "\n".join(item["text"] for item in safe_turns if item["speaker"] == "customer")[-1600:]
    payload = {
        "customer_mention": raw,
        "area": area,
        "industry": industry,
        "customer_context": context,
        "candidates": [{
            "credit_code": item.get("credit_code") or item.get("id"),
            "name": item.get("name"),
            "brand": item.get("brand") or "",
            "area": item.get("area") or item.get("registered_area") or "",
            "industry": item.get("industry") or "",
            "status": item.get("status") or "",
            "legal_representative": item.get("legal_representative") or "",
            "provider_position": item.get("provider_position") or 0,
            "role_hint": item.get("role_hint") or "",
        } for item in candidates],
    }
    cache = QccCache(settings.qcc_cache_path, settings.qcc_cache_days)
    key = "rerank:" + hashlib.sha256(_compact({
        "version": "entity-rerank-v1",
        "model": settings.model,
        "payload": payload,
    }).encode()).hexdigest()
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("items"):
        return {**cached, "cached": True}
    system = (ROOT / "prompts/entity_rerank_v1.md").read_text()
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    try:
        with httpx.Client(timeout=httpx.Timeout(min(max(settings.timeout, 20), 45), connect=10),
                          trust_env=False, follow_redirects=False) as client:
            response = client.post(settings.base_url.rstrip("/") + "/chat/completions",
                                   headers={"Authorization": "Bearer " + settings.api_key},
                                   json=json_chat_body(settings.model, messages, 3000))
        if response.status_code != 200:
            raise RerankError(provider_error(response, "企业重排模型"))
        content = response.json()["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
        parsed = RerankResponse.model_validate_json(content)
    except RerankError:
        raise
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise RerankError("企业重排模型响应无法解析") from exc
    expected = {item.get("credit_code") or item.get("id") for item in candidates}
    got = [item.credit_code for item in parsed.candidates]
    if len(got) != len(set(got)) or set(got) != expected:
        raise RerankError("企业重排结果未完整覆盖候选主体")
    result = {
        "items": {item.credit_code: item.model_dump() for item in parsed.candidates},
        "top": parsed.top if parsed.top in expected else None,
        "model": settings.model,
        "note": parsed.note,
        "cached": False,
    }
    try:
        cache.set(key, result)
    except OSError:
        pass
    return result
