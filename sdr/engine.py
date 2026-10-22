from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import time
import re

from .entities import captured_entity, has_company
from .entity_resolver import resolve_company
from .extractors import rule_extract, llm_extract, ProviderError
from .role_draft import auto_label_draft, draft_has_customer, draft_pending_count, has_labels, label_unlabeled
from .audio_roles import classify_roles
from .models import Extraction, Lead
from .preprocess import split_turns
from .scoring import assess, DIMENSIONS, LABELS, SCORE_VERSION
from .validation import validate_extraction

REASONS = {
    "INTENT_ABNORMAL": "得分0–5分（含5分），须人工核听录音并给出意向定性",
    "ENTITY_UNRESOLVED": "企业名称缺失，待人工填写", "ENTITY_AMBIGUOUS": "多个经营主体，客户归属不明",
    "ENTITY_UNCONFIRMED": "企业全称已召回，待人工确认客户关系",
    "ENTITY_LOOKUP_FAILED": "企业检索失败", "CONTACT_MASKED": "联系方式脱敏，无法直接联系",
    "CONTACT_MISSING": "缺少可用联系方式", "CONTACT_NAME_MISSING": "联系人尚未确认",
    "INTENT_INSUFFICIENT": "意向证据不足或存在冲突", "INTENT_BOUNDARY": "意向处于阈值复核带",
    "EVIDENCE_INVALID": "有字段未通过证据校验", "EXTRACTION_FAILED": "模型提取失败，需要人工处理",
    "SECOND_REVIEW": "等待另一名审核人复核", "QUALIFICATION_UNKNOWN": "行业或企业场景尚未确认",
    "DO_NOT_CONTACT": "客户要求停止联系", "LOW_INTENT": "当前为低意向，不作为正式 SQL",
    "NOT_SALES": "服务或无效通话，不进入新增销售线索", "MANUAL_REVIEW": "冷启动人工审核",
    "ROLE_PENDING": "有角色待确认语句，已暂不参与事实抽取",
}


def recalculate(result, request, settings):
    ex = result["extraction"]
    intent = assess(ex)
    result.setdefault("versions", {})["rules"] = SCORE_VERSION
    if result.get("human_level") and intent["status"] != "policy":
        intent.update(level=result["human_level"], status="human_override", quality=.6)
    result["intent"] = intent
    entity = result["entity"]
    fields = ex["fields"]
    value = lambda name: fields[name]["value"]
    phone = value("phone")
    phone_status = "valid" if isinstance(phone, str) and re.fullmatch(r"1[3-9]\d{9}", phone) else "masked" if phone else "missing"
    review = result.get("review", {"status": "pending", "stage": "primary", "primary_actor": None})
    approved = review["status"] == "approved"
    blockers, reasons = [], []
    abnormal = intent.get("requires_manual_intent") and not result.get("manual_intent_review")
    if abnormal:
        blockers.append("INTENT_ABNORMAL")
    sales = ex["call_type"] == "sales" or ex.get("do_not_contact") or ex.get("refusal")
    if not sales:
        blockers.append("NOT_SALES")
    if ex.get("do_not_contact"):
        blockers.append("DO_NOT_CONTACT")
    if sales and not ex.get("do_not_contact"):
        if not has_company(entity):
            code = {"ambiguous": "ENTITY_AMBIGUOUS", "lookup_failed": "ENTITY_LOOKUP_FAILED"}.get(entity["status"], "ENTITY_UNRESOLVED")
            blockers.append(code)
        elif entity.get("status") == "suggested":
            blockers.append("ENTITY_UNCONFIRMED")
        if phone_status != "valid":
            blockers.append("CONTACT_MASKED" if phone_status == "masked" else "CONTACT_MISSING")
        if not value("contact_name"):
            blockers.append("CONTACT_NAME_MISSING")
        if not value("industry"):
            blockers.append("QUALIFICATION_UNKNOWN")
    if intent["level"] is None and ex["call_type"] not in ("service", "invalid"):
        blockers.append("INTENT_INSUFFICIENT")
    if intent["level"] == "low":
        blockers.append("LOW_INTENT")
    if result.get("validation_issues") and not result.get("validation_acknowledged"):
        blockers.append("EVIDENCE_INVALID")
    if result.get("extraction_error") and not result.get("error_resolved"):
        blockers.append("EXTRACTION_FAILED")
    reasons.extend(c for c in blockers if c not in ("LOW_INTENT", "DO_NOT_CONTACT", "NOT_SALES"))
    if intent["boundary"] and not approved:
        reasons.append("INTENT_BOUNDARY")
    if review.get("stage") == "secondary" and not approved:
        reasons.append("SECOND_REVIEW")
    pending_turns = sum(1 for turn in result.get("turns", []) if turn.get("speaker") == "pending")
    if pending_turns:
        reasons.append("ROLE_PENDING")
    if not approved and not settings.auto_publish and not reasons and intent["level"] in ("high", "medium"):
        reasons.append("MANUAL_REVIEW")
    if abnormal:
        if review["status"] not in ("rejected", "deferred"):
            review["status"] = "pending"
    elif ex["call_type"] in ("invalid", "service") or ex.get("do_not_contact") or intent["level"] == "low":
        if review["status"] not in ("rejected", "deferred"):
            review["status"] = "not_required"
    elif settings.auto_publish and not blockers and not reasons and review["status"] == "pending":
        review["status"] = "not_required"
    # Synthetic fixtures may become demo-ready; qualified production export is independently gated.
    review_ok = approved or review["status"] == "not_required" and settings.auto_publish
    ready = not blockers and review_ok and review.get("stage") != "secondary"
    if review["status"] in ("deferred", "rejected"):
        ready = False
    result.update(review=review, blockers=blockers, review_reasons=list(dict.fromkeys(reasons)),
                  role_pending_count=pending_turns,
                  delivery_status="ready" if ready else "blocked", phone_status=phone_status,
                  production_eligible=request["data_mode"] == "real" and ready,
                  reason_labels=REASONS)
    qfields = 0.0 if "EVIDENCE_INVALID" in blockers else 1.0 if phone_status == "valid" and value("contact_name") else .5
    result["quality"] = {"entity": entity["quality"], "intent": intent["quality"], "fields": qfields,
                         "overall": min(entity["quality"], intent["quality"], qfields), "method": "heuristic_min_v1"}
    if intent["level"] is None or not sales:
        result["lead"] = None
        result["next_action"] = "转服务团队处理当前诉求。" if ex["call_type"] == "service" else "补充有效通话及尚缺证据，再重新判断。"
        return result
    if ex.get("do_not_contact"):
        next_action = "停止营销联系；保留拒联标记，不进入培育或自动外联。"
    elif intent["level"] == "low":
        next_action = "暂不作为正式 SQL；仅在客户同意的范围和时点安排后续沟通。"
    elif entity.get("status") in ("ambiguous", "suggested", "lookup_failed", "unresolved") or not has_company(entity) or phone_status != "valid":
        next_action = "优先确认企业抬头及可用联系方式；按已同意渠道沟通，确认试用安排、设计师人数和采购角色。"
    elif intent["level"] == "high":
        next_action = "建议一个工作日内按已同意渠道跟进，确认演示或试用日期及参与者；完成审核后交付。"
    else:
        next_action = "围绕预算、时间或决策路径的缺口补问，再安排下一步；完成审核后交付。"
    if intent["status"] == "policy":
        reason = "低意向：" + ("客户明确要求停止联系。" if ex.get("do_not_contact") else "客户明确拒绝当前需求或已撤回。")
    else:
        description = []
        for d, signal in ex["signals"].items():
            refs = ",".join(e["turn_id"] for e in signal["evidence"])
            description.append(f"{DIMENSIONS[d]}：{LABELS[signal['kind']]}" + (f"[{refs}]" if refs else ""))
        label = {"high": "高", "medium": "中", "low": "低"}[intent["level"]]
        interval = str(intent["total"])
        reason = f"{label}意向，规则分数{interval}分。" + "；".join(description) + "。采购角色与实际安排仍需确认。"
        if result.get("human_level"):
            reason += "等级经人工覆盖，原始规则结果见审计记录。"
    lead = Lead(company_name=entity["selected"]["name"] if has_company(entity) else entity.get("mention"),
                industry=value("industry"), area=value("area"), contact_name=value("contact_name"), contact_title=value("contact_title"),
                phone=phone, intent_level=intent["level"], intent_reason=reason, demand_summary=value("demand_summary"),
                mendian=value("mendian"), shejishi=value("shejishi"), call_time=request.get("call_time"), next_action=next_action,
                confidence_score=float(result["quality"]["overall"]), need_human_review=bool(reasons))
    result["lead"] = lead.model_dump()
    result["next_action"] = next_action
    return result


class Engine:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="sdr")

    def submit(self, aid):
        return self.pool.submit(self.run, aid)

    def _cancelled(self, aid):
        try:
            return self.store.get(aid)["status"] == "cancelled"
        except Exception:
            return False

    def run(self, aid):
        record = self.store.get(aid)
        if record["status"] == "cancelled":
            return record
        request = record["request"]
        started = time.monotonic()
        turns = split_turns(request["transcript"])
        result = {"turns": [t.model_dump() for t in turns], "versions": {"schema": "2.1", "rules": SCORE_VERSION, "validation": "evidence-2.2", "entity": "entity-4.0", "roles": "auto-text-v1"},
                  "review": {"status": "pending", "stage": "primary", "primary_actor": None}, "data_mode": request["data_mode"], "auto_roles": None}
        settings=self.settings
        try:
            settings=self.settings.resolved(record["owner"])
            if getattr(self.settings,"model_settings",None):
                from .workspace import require_analysis
                with self.store.connection() as db:require_analysis(db,aid,record["owner"])
            if self._cancelled(aid):
                return self.store.get(aid)
            text = request["transcript"]
            if not has_labels(text):
                if settings.api_key:
                    try:
                        role_result = classify_roles(text, settings)
                        if self._cancelled(aid):
                            return self.store.get(aid)
                        original = role_result.get("transcript") or text
                        text = auto_label_draft(original)
                        auto_roles = {"method": "text_role_model", "model": role_result.get("model"),
                                      "uncertain_count": role_result.get("uncertain_count"),
                                      "pending_count": draft_pending_count(text),
                                      "fallback": not draft_has_customer(original), "error": None}
                    except Exception as exc:
                        text = label_unlabeled(text)
                        auto_roles = {"method": "single_customer_fallback", "model": None,
                                      "uncertain_count": None, "pending_count": 0,
                                      "fallback": True, "error": str(exc)[:300]}
                else:
                    text = label_unlabeled(text)
                    auto_roles = {"method": "single_customer_fallback", "model": None,
                                  "uncertain_count": None, "pending_count": 0,
                                  "fallback": True, "error": "未配置文字分析模型"}
                turns = split_turns(text)
                result["turns"] = [t.model_dump() for t in turns]
                result["auto_roles"] = auto_roles
            business_turns = [t for t in turns if t.speaker not in ('other','excluded','pending')]
            self.store.checkpoint(aid, "原文分段与角色定位", {"turns": len(turns), "auto_roles": result["auto_roles"] is not None})
            self.store.checkpoint(aid, "模型理解与事实抽取" if request["mode"] == "llm" else "离线规则抽取")
            if self._cancelled(aid):
                return self.store.get(aid)
            extraction, provider = llm_extract(business_turns, settings) if request["mode"] == "llm" else rule_extract(business_turns)
            if self._cancelled(aid):
                return self.store.get(aid)
            if request['mode']=='llm':
                provider.update(configured_by=record['owner'],provider_id=settings.text_provider)
            self.store.checkpoint(aid, "证据与字段口径校验")
            validated, issues = validate_extraction(extraction, turns)
            result.update(extraction=validated, raw_extraction=extraction.model_dump(), validation_issues=issues, provider=provider)
            self.store.checkpoint(aid, "企业召回与主体关系核验")
            if self._cancelled(aid):
                return self.store.get(aid)
            if validated["call_type"] in ("invalid", "service") or validated["do_not_contact"]:
                empty = deepcopy(validated["fields"])
                empty["raw_company"]["value"] = empty["brand_name"]["value"] = None
                result["entity"] = captured_entity(empty, request["data_mode"])
            else:
                result["entity"] = resolve_company(validated["fields"], turns, settings, request["data_mode"])
            if self._cancelled(aid):
                return self.store.get(aid)
            self.store.checkpoint(aid, "意向评分与审核路由")
            recalculate(result, request, settings)
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            if self._cancelled(aid):
                return self.store.get(aid)
            self.store.finish(aid, result)
        except Exception as exc:
            if self._cancelled(aid):
                return self.store.get(aid)
            # A failed provider never silently falls back to a pretend AI result.
            from .model_settings import ModelConfigError
            from .workspace import Forbidden
            from .store import NotFound
            error = str(exc) if isinstance(exc, (ProviderError,ModelConfigError,Forbidden,NotFound)) else "分析发生内部错误，已保存原文和检查点。可重试或人工修正。"
            empty, _ = validate_extraction(Extraction(), turns)
            result.update(extraction=result.get("extraction", empty), validation_issues=result.get("validation_issues", []),
                          entity=result.get("entity", {"status": "unresolved", "selected": None, "candidates": [], "quality": 0.0, "mention": None, "missing": []}),
                          extraction_error=error, provider=result.get("provider", {"provider": request["mode"], "model": settings.model if request["mode"] == "llm" else "offline-rules-v1"}),
                          elapsed_ms=round((time.monotonic() - started) * 1000))
            recalculate(result, request, settings)
            self.store.finish(aid, result, error)
        return self.store.get(aid)
