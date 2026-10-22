from datetime import date, datetime
from zoneinfo import ZoneInfo
import json
import re
import unicodedata

import httpx
from pypinyin import lazy_pinyin
from rapidfuzz.fuzz import ratio
from pydantic import Field

from .models import Model


class Entity(Model):
    id: str
    name: str
    brand: str = ""
    aliases: list[str] = Field(default_factory=list)
    role: str = "unknown"
    area: str = ""
    industry: str = ""
    status: str = "未知"
    source_url: str = ""
    source_title: str = ""
    observed_at: str = ""
    valid_until: str = ""
    data_mode: str = "real"
    verified_registry: bool = False


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).lower()


def similarity(a, b):
    a, b = normalized(a), normalized(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Legal suffixes and parenthesized locations inflate unrelated-company scores.
    def core(s):
        return re.sub(r"(?:股份有限公司|有限责任公司|有限公司)$", "", re.sub(r"\([^)]*\)", "", s))
    a, b = core(a), core(b)
    if not a or not b:
        return 0.0
    if sorted(a) == sorted(b):
        return .98
    char = ratio(a, b) / 100
    sound = ratio(lazy_pinyin(a), lazy_pinyin(b)) / 100
    return round(max(char, sound * .96), 4)


def source_valid(entity, data_mode):
    if entity.get("source_type") == "qcc_mcp":
        if not entity.get("credit_code") or not entity.get("verified_registry"):
            return False
        try:
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            return date.fromisoformat(entity["observed_at"][:10]) <= today <= date.fromisoformat(entity["valid_until"][:10])
        except (ValueError, KeyError, TypeError):
            return False
    if not entity.get("verified_registry") or not entity.get("id") or not entity.get("source_url"):
        return False
    if data_mode == "real" and entity.get("data_mode") != "real":
        return False
    if data_mode == "real" and not entity["source_url"].startswith("https://"):
        return False
    try:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        return date.fromisoformat(entity["observed_at"][:10]) <= today <= date.fromisoformat(entity["valid_until"][:10])
    except (ValueError, KeyError, TypeError):
        return False


def resolve(fields, turns, settings, data_mode, learned=None):
    mention = fields["raw_company"]["value"] or fields["brand_name"]["value"]
    base = {"status": "unresolved", "selected": None, "candidates": [], "mention": mention,
            "quality": .25 if mention else 0.0, "search_status": "not_needed", "query": mention,
            "missing": ["客户经营主体全称及与该主体的关系"], "catalog_version": "demo-catalog-v1"}
    if not mention:
        return base
    entities = [Entity.model_validate(e).model_dump() for e in json.loads(settings.entity_catalog.read_text())]
    # Demonstration facts can never leak into real entity confirmation.
    entities = [e for e in entities if data_mode != "real" or e["data_mode"] == "real"]
    search_status = "local_only"
    if settings.entity_url:
        try:
            response = httpx.get(settings.entity_url, params={"name": mention, "limit": 10},
                                 headers={"Authorization": "Bearer " + settings.entity_key} if settings.entity_key else {},
                                 timeout=5, follow_redirects=False)
            response.raise_for_status()
            external = response.json()["entities"]
            entities.extend(Entity.model_validate(e).model_dump() for e in external[:10])
            search_status = "ok" if external else "ok_empty"
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            search_status = "lookup_failed"
    customer = [t.text for t in turns if t.speaker == "customer"]
    candidates, seen = [], set()
    for entity in entities:
        if entity["id"] in seen:
            continue
        seen.add(entity["id"])
        aliases = entity["aliases"] + [entity["name"], entity["brand"]]
        for feedback in learned or []:
            if feedback["entity_id"] == entity["id"] and feedback.get("area") == fields["area"]["value"] and feedback.get("data_mode") == data_mode:
                aliases.append(feedback["mention"])
        name_match = max(similarity(mention, a) for a in aliases)
        if name_match < .58:
            continue
        # Only an explicit self-identification can establish automatic call-to-entity relation.
        relation_quotes = [s for s in customer if normalized(entity["name"]) in normalized(s) and
                           re.search(r"我们公司|公司全称|我们是|我公司", s)]
        relation = bool(relation_quotes) and normalized(mention) == normalized(entity["name"])
        area = fields["area"]["value"] or ""
        city_match = bool(area and entity["area"] and (area in entity["area"] or entity["area"] in area))
        industry_match = bool(fields["industry"]["value"] and fields["industry"]["value"] == entity["industry"])
        score = .5 * name_match + .3 * relation + .15 * city_match + .05 * industry_match
        candidates.append({**entity, "rank_score": round(score, 3), "name_match": name_match,
                           "relation_supported": relation, "relation_quotes": relation_quotes,
                           "source_valid": source_valid(entity, data_mode),
                           "missing_links": [] if relation else ["尚未证明通话客户属于该经营主体"]})
    candidates.sort(key=lambda c: (-c["rank_score"], c["id"]))
    candidates = candidates[:10]
    verified = [c for c in candidates if c["relation_supported"] and c["source_valid"]]
    base.update(candidates=candidates, search_status=search_status)
    if len(candidates) > 1:
        base.update(status="ambiguous", quality=.25, missing=["检索到多个相似主体，请人工选择并确认客户归属"])
    elif len(verified) == 1:
        base.update(status="verified", selected=verified[0], quality=1.0, missing=[])
    elif candidates:
        base.update(status="unresolved", quality=.6 if candidates[0]["source_valid"] else .25)
    elif search_status == "lookup_failed":
        base.update(status="lookup_failed", quality=0.0, missing=["企业检索失败，请重试或补充可信来源"])
    return base


def captured_entity(fields, data_mode, name=None, manual=False):
    """Keep the customer-supplied name; this is not a registry verification."""
    value = name if manual else (fields['raw_company']['value'] or fields['brand_name']['value'])
    value = value.strip() if isinstance(value, str) else None
    return {'status': ('manual' if manual else 'captured') if value else 'missing',
            'selected': {'name': value, 'data_mode': data_mode, 'verified_registry': False,
                         'source_title': '人工填写' if manual else '通话提取'} if value else None,
            'mention': value or None, 'candidates': [], 'quality': .6 if value else 0.0,
            'search_status': 'not_requested', 'query': None,
            'raw_name': value or None, 'provider': 'off', 'resolution_confidence': 0.0,
            'query_variants': [], 'fetched_at': None, 'error': None,
            'missing': [] if value else ['企业名称待人工填写'], 'method': 'transcript_or_manual'}


def has_company(entity):
    return entity.get('status') in ('captured','manual','verified','suggested','confirmed') and bool((entity.get('selected') or {}).get('name'))
