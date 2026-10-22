from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import re
import unicodedata

from .entities import captured_entity, resolve as resolve_local, similarity
from .qcc_mcp import QccCache, QccError, QccMcpClient
from .entity_rerank import RerankError, rerank_candidates

ASR_VARIANTS = {
    "七木": ["栖木", "漆木", "齐木"],
    "栖木": ["七木", "漆木", "齐木"],
    "木里木外": ["木里木外家居"],
    "酷家乐": ["酷加乐", "酷家乐"],
}

def _city(area: str) -> str:
    return (area or "").split("省")[-1].split("市")[0].strip()


def _name_match(raw: str, name: str, area: str) -> float:
    value = similarity(raw, name)
    city = _city(area)
    if city and (city in raw or city in name):
        value = max(value, similarity(raw.replace(city, ""), name.replace(city, "")))
    return value


LEGAL_SUFFIX = re.compile(r"(?:股份有限公司|有限责任公司|集团有限公司|有限公司)$")
BRAND_SUFFIX = re.compile(r"(?:家居|定制|设计|装饰|科技|家具|集团)$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_query(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[“”‘’\"'「」『』《》〈〉（）()\[\]【】]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" ，,。.;；、")
    return value


def core_name(value: str) -> str:
    value = normalize_query(value).lower()
    value = re.sub(r"\([^)]*\)", "", value)
    return LEGAL_SUFFIX.sub("", value).strip()


def query_variants(raw: str, area: str = "") -> list[str]:
    base = normalize_query(raw)
    if not base:
        return []
    variants = [base]
    city = _city(area)
    if city:
        variants.append(f"{base} {city}")
    for key, replacements in ASR_VARIANTS.items():
        if key in base:
            for replacement in replacements:
                variants.append(base.replace(key, replacement))
    stripped = BRAND_SUFFIX.sub("", LEGAL_SUFFIX.sub("", base)).strip()
    if stripped and stripped != base:
        variants.append(stripped)
    if city and stripped and stripped != base:
        variants.append(f"{stripped} {city}")
    return list(dict.fromkeys(v for v in variants if v))[:4]


def _map_industry(value: Any) -> str:
    if isinstance(value, dict):
        text = " ".join(str(value.get(k) or "") for k in ("门类", "大类", "中类", "小类"))
    else:
        text = str(value or "")
    if "家具" in text:
        return "家具制造"
    if "建筑装饰" in text or "装修" in text:
        return "装企"
    if "批发" in text or "零售" in text or "建材" in text:
        return "建材"
    if "软件" in text or "信息技术" in text:
        return "其他"
    return text.strip()


def _alive(candidate: dict[str, Any]) -> bool:
    status = str(candidate.get("status") or "")
    return "存续" in status or "在业" in status or "开业" in status


def _score(raw: str, candidate: dict[str, Any], area: str, industry: str) -> tuple[float, float, str]:
    normalized_raw = core_name(raw)
    normalized_name = core_name(str(candidate.get("name") or ""))
    name_match = _name_match(raw, str(candidate.get("name") or ""), area)
    if normalized_raw and normalized_name and (normalized_raw in normalized_name or normalized_name in normalized_raw):
        name_match = max(name_match, .9)
    brand = str(candidate.get("brand") or "")
    brand_match = 1.0 if normalized_raw and normalized_raw in normalized_name else 1.0 if brand and (core_name(brand) in normalized_raw or normalized_raw in core_name(brand)) else 0.0
    candidate_area = str(candidate.get("area") or candidate.get("registered_area") or "")
    area_match = 1.0 if area and candidate_area and (area in candidate_area or candidate_area in area) else 0.0
    candidate_industry = str(candidate.get("industry") or "")
    industry_match = 1.0 if industry and candidate_industry and (industry in candidate_industry or candidate_industry in industry) else 0.0
    alive = 1.0 if _alive(candidate) else 0.0
    score = .5 * name_match + .3 * brand_match + .1 * area_match + .05 * industry_match + .05 * alive
    reasons = []
    if name_match >= .999:
        reasons.append("名称精确匹配")
    elif name_match >= .75:
        reasons.append("名称相似")
    if brand_match:
        reasons.append("品牌/简称匹配")
    if area_match:
        reasons.append("地区一致")
    elif area and candidate_area:
        reasons.append("地区不一致")
    if industry_match:
        reasons.append("行业一致")
    if alive:
        reasons.append("登记状态存续")
    if candidate.get("provider_position"):
        reasons.append(f"企查查返回第{candidate['provider_position']}位")
    if candidate.get("role_hint"):
        reasons.append(str(candidate["role_hint"]))
    return round(score, 4), round(name_match, 4), "；".join(reasons) or "仅名称召回"


def _enrich(candidate: dict[str, Any], client: QccMcpClient) -> None:
    credit = str(candidate.get("credit_code") or "")
    if not credit:
        return
    try:
        detail = client.company_detail(credit)
    except QccError as exc:
        candidate["detail_error"] = str(exc)[:200]
        return
    candidate["name"] = str(detail.get("企业名称") or candidate.get("name") or "")
    candidate["credit_code"] = str(detail.get("统一社会信用代码") or credit)
    candidate["id"] = candidate["credit_code"] or candidate["name"]
    short = str(detail.get("企业简称") or "")
    if short:
        candidate["brand"] = short
        candidate["aliases"] = [short]
    candidate["registered_area"] = str(detail.get("所属地区") or "")
    candidate["area"] = candidate["registered_area"]
    candidate["registered_address"] = str(detail.get("注册地址") or "")
    candidate["industry"] = _map_industry(detail.get("国标行业"))
    candidate["registry_industry"] = candidate["industry"]
    candidate["company_type"] = str(detail.get("企业类型") or "")
    candidate["legal_representative"] = str(detail.get("法定代表人") or candidate.get("legal_representative") or "")
    candidate["established_at"] = str(detail.get("成立日期") or candidate.get("established_at") or "")
    candidate["status"] = str(detail.get("登记状态") or detail.get("状态") or candidate.get("status") or "")
    candidate["source_title"] = "企查查 MCP"
    candidate["source_type"] = "qcc_mcp"
    candidate["source_ref"] = candidate["credit_code"]


def _base(fields: dict[str, Any], data_mode: str) -> dict[str, Any]:
    entity = captured_entity(fields, data_mode)
    entity.update(raw_name=entity.get("mention"), provider="off", resolution_confidence=0.0,
                  query_variants=[], fetched_at=None, error=None)
    return entity


def _credentials(settings) -> tuple[str, str]:
    if hasattr(settings, "qcc_credentials"):
        return settings.qcc_credentials()
    return (getattr(settings, "qcc_mcp_url", "") or "", getattr(settings, "qcc_mcp_token", "") or "")


def _provider(settings) -> str:
    provider = str(getattr(settings, "entity_provider", "auto") or "auto").lower()
    if provider == "auto":
        url, token = _credentials(settings)
        return "qcc" if url and token else "off"
    return provider


RERANK_ORDER = {"exact": 0, "alias": 1, "brand": 2, "related": 3, "unrelated": 4}


def _apply_rerank(candidates, rerank):
    items = rerank["items"]
    for candidate in candidates:
        code = candidate.get("credit_code") or candidate.get("id")
        item = items.get(code, {})
        candidate.update(rerank_match=item.get("match_type", "unrelated"),
                         rerank_confidence=float(item.get("confidence") or 0),
                         rerank_reason=item.get("reason", "模型未返回该候选"),
                         rerank_role=item.get("role", "unknown"),
                         ranker="llm", rerank_model=rerank.get("model"))
    candidates.sort(key=lambda item: (-float(item.get("rerank_confidence") or 0),
                                      RERANK_ORDER.get(item.get("rerank_match"), 9),
                                      item.get("provider_position") or 99))
    for index, candidate in enumerate(candidates, 1):
        candidate["rerank_rank"] = index


def _auto_guard(raw, fields, top, candidates):
    if not top:
        return False
    confidence = float(top.get("rerank_confidence") or 0)
    if top.get("rerank_match") != "exact" or confidence < .9:
        return False
    exacts = [item for item in candidates
              if item.get("rerank_match") == "exact" and float(item.get("rerank_confidence") or 0) >= .9]
    if len(exacts) != 1:
        return False
    name_match = float(top.get("name_match") or 0)
    if name_match >= .999:
        return True
    if not re.search(r"(有限公司|有限责任公司|股份有限公司|集团|工作室|事务所)$", raw.strip()):
        return False
    if name_match < .9:
        return False
    area = str(fields.get("area", {}).get("value") or "")
    candidate_area = str(top.get("area") or top.get("registered_area") or "")
    if area and candidate_area and area not in candidate_area and candidate_area not in area:
        return False
    return True


def _auto_entity(fields, data_mode, top, candidates, *, method, verification, ranker,
                 rerank_model=None, confidence=1.0, query=None, variants=None, note=None):
    entity = _base(fields, data_mode)
    selected = {**top, "verified_registry": True, "relation_supported": True,
                "verification_method": verification, "confirmed_at": _now(), "confirmed_by": "auto"}
    entity.update(status="confirmed", selected=selected, candidates=candidates, quality=1.0,
                  provider="qcc_mcp", method=method, ranker=ranker, rerank_model=rerank_model,
                  auto_confirmed=True, resolution_confidence=confidence, query=query,
                  query_variants=variants or [], fetched_at=_now(), missing=[])
    if note:
        entity["rerank_note"] = note
    return entity


def _move_top(candidates, top):
    if top in candidates:
        candidates.remove(top)
        candidates.insert(0, top)


def _qcc_resolve(fields: dict[str, Any], raw: str, turns, settings, data_mode: str) -> dict[str, Any]:
    url, token = _credentials(settings)
    if not url or not token:
        entity = _base(fields, data_mode)
        entity.update(status="lookup_failed", selected=None, provider="qcc_mcp", quality=.4,
                      error="未配置企查查 MCP 地址或 token",
                      missing=["企查查未配置，保留原话，待人工填写"])
        return entity
    cache = QccCache(getattr(settings, "qcc_cache_path"), getattr(settings, "qcc_cache_days", 30))
    client = QccMcpClient(url, token, timeout=getattr(settings, "qcc_timeout", 8.0), cache=cache)
    area = str(fields.get("area", {}).get("value") or "")
    industry = str(fields.get("industry", {}).get("value") or "")
    variants = query_variants(raw, area)
    queries = variants[:2]
    search = client.search_company(queries[0])
    used_query = queries[0]
    if search.status == "none":
        for variant in queries[1:]:
            search = client.search_company(variant)
            used_query = variant
            if search.status != "none":
                break
    if search.status == "none":
        entity = _base(fields, data_mode)
        entity.update(status="unresolved", selected=None, provider="qcc_mcp", quality=.3,
                      query=used_query, query_variants=variants, fetched_at=_now(),
                      resolution_confidence=0.0,
                      missing=["企查查未找到匹配主体，保留原话，待人工填写"])
        return entity
    candidates = []
    for candidate in search.candidates:
        score, name_match, reason = _score(raw, candidate, area, industry)
        candidate.update(name_match=name_match, rank_score=score, match_reason=reason,
                         relation_supported=False)
        candidates.append(candidate)
    enrich_top = max(0, min(len(candidates), int(getattr(settings, "qcc_enrich_top", 1))))
    for candidate in candidates[:enrich_top]:
        _enrich(candidate, client)
        score, name_match, reason = _score(raw, candidate, area, industry)
        candidate.update(name_match=name_match, rank_score=score, match_reason=reason)
    rerank = None
    rerank_error = None
    if len(candidates) > 1:
        try:
            rerank = rerank_candidates(raw, fields, turns, candidates, settings)
        except RerankError as exc:
            rerank_error = str(exc)[:300]
    if rerank:
        _apply_rerank(candidates, rerank)
        top = candidates[0]
        if not top.get("registered_area"):
            _enrich(top, client)
            score, name_match, reason = _score(raw, top, area, industry)
            top.update(name_match=name_match, rank_score=score, match_reason=reason)
        method = "qcc_mcp_rerank"
        ranker = "llm"
        model = rerank.get("model")
        note = rerank.get("note")
        if _auto_guard(raw, fields, top, candidates):
            return _auto_entity(fields, data_mode, top, candidates, method=method,
                                verification="auto_qcc_rerank", ranker=ranker, rerank_model=model,
                                confidence=float(top.get("rerank_confidence") or 1.0),
                                query=used_query, variants=variants, note=note)
        entity = _base(fields, data_mode)
        match_type = top.get("rerank_match")
        if match_type in ("exact", "alias", "brand") and float(top.get("rerank_confidence") or 0) >= .75 and len(candidates) == 1:
            entity.update(status="suggested", selected=top, candidates=candidates, quality=.85,
                          missing=["企业全称由企查查召回，客户与该主体的关系待人工确认"])
        else:
            entity.update(status="ambiguous", selected=None, candidates=candidates, quality=.5,
                          missing=["重排后仍需人工确认客户经营主体"])
        entity.update(provider="qcc_mcp", method=method, ranker=ranker, rerank_model=model,
                      auto_confirmed=False, rerank_note=note, rerank_error=rerank_error,
                      query=used_query, query_variants=variants, fetched_at=_now(),
                      resolution_confidence=float(top.get("rerank_confidence") or 0))
        return entity
    exact = [candidate for candidate in candidates if candidate.get("name_match", 0) >= .999]
    if len(exact) == 1:
        top = exact[0]
        _enrich(top, client)
        score, name_match, reason = _score(raw, top, area, industry)
        top.update(name_match=name_match, rank_score=score, match_reason=reason)
        _move_top(candidates, top)
        return _auto_entity(fields, data_mode, top, candidates, method="qcc_mcp_exact",
                            verification="auto_exact_name", ranker="qcc_order",
                            confidence=1.0, query=used_query, variants=variants)
    if search.status == "unique" and len(candidates) == 1:
        top = candidates[0]
        _enrich(top, client)
        score, name_match, reason = _score(raw, top, area, industry)
        top.update(name_match=name_match, rank_score=score, match_reason=reason)
        entity = _base(fields, data_mode)
        entity.update(status="suggested", selected=top, candidates=candidates, quality=.85,
                      provider="qcc_mcp", method="qcc_mcp_search", ranker="qcc_order",
                      rerank_model=None, auto_confirmed=False, rerank_error=rerank_error,
                      query=used_query, query_variants=variants, fetched_at=_now(),
                      resolution_confidence=score,
                      missing=["企业全称由企查查召回，客户与该主体的关系待人工确认"])
        return entity
    top_score = candidates[0]["rank_score"] if candidates else 0.0
    entity = _base(fields, data_mode)
    entity.update(status="ambiguous", selected=None, candidates=candidates, quality=.5,
                  provider="qcc_mcp", method="qcc_mcp_search", ranker="qcc_order",
                  rerank_model=None, auto_confirmed=False, rerank_error=rerank_error,
                  query=used_query, query_variants=variants,
                  fetched_at=_now(), resolution_confidence=top_score,
                  missing=["多个候选，请人工确认客户经营主体"])
    return entity


def resolve_company(fields: dict[str, Any], turns, settings, data_mode: str) -> dict[str, Any]:
    raw = fields.get("raw_company", {}).get("value") or fields.get("brand_name", {}).get("value") or ""
    raw = str(raw).strip() if isinstance(raw, str) else ""
    provider = _provider(settings)
    if not raw:
        entity = _base(fields, data_mode)
        entity.update(provider=provider if provider != "qcc" else "qcc_mcp", raw_name=None)
        return entity
    if provider == "off":
        return _base(fields, data_mode)
    if provider in ("local", "catalog"):
        try:
            entity = resolve_local(fields, turns, settings, data_mode)
            entity.update(provider="local_catalog", raw_name=raw, resolution_confidence=entity.get("quality", 0.0),
                          query=entity.get("query"), query_variants=query_variants(raw, str(fields.get("area", {}).get("value") or "")),
                          fetched_at=_now(), error=None)
            return entity
        except Exception as exc:
            entity = _base(fields, data_mode)
            entity.update(status="lookup_failed", selected=None, provider="local_catalog", quality=.4,
                          error=str(exc)[:300], missing=["本地企业目录检索失败，保留原话，待人工填写"])
            return entity
    if provider != "qcc":
        return _base(fields, data_mode)
    try:
        return _qcc_resolve(fields, raw, turns, settings, data_mode)
    except QccError as exc:
        entity = _base(fields, data_mode)
        entity.update(status="lookup_failed", selected=None, provider="qcc_mcp", quality=.4,
                      raw_name=raw, query_variants=query_variants(raw, str(fields.get("area", {}).get("value") or "")),
                      fetched_at=_now(), error=str(exc)[:300],
                      missing=["企查查检索失败，保留原话，待人工填写"])
        return entity
    except Exception as exc:
        entity = _base(fields, data_mode)
        entity.update(status="lookup_failed", selected=None, provider="qcc_mcp", quality=.4,
                      raw_name=raw, fetched_at=_now(), error=str(exc)[:300],
                      missing=["企业抬头检索异常，保留原话，待人工填写"])
        return entity
