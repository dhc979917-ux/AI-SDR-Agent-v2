import re
import unicodedata
from .scoring import TIERS

COUNTS = {"mendian", "shejishi", "team_count"}

# Explicit language gates for frequently overclaimed signals. These are guards,
# not a general Chinese entailment solver: ambiguous signals abstain for review.
SEMANTIC_GATES = {
    "need_none": r"不需要|没(?:有)?(?:当前|采购)?需求|无需求|不考虑(?:购买|采购)",
    "time_none": r"没(?:有)?(?:明确的|任何|采购|推进|上线|试用)?计划|无(?:近期|推进)?计划|不着急|近期不考虑|没有时间表",
    "budget_none": r"没(?:有)?预算|没有(?:支出|采购资金)|无预算|不付费|不打算花钱|不会付费",
    "budget_approved": r"(?:预算|经费).{0,12}(?:已批|获批|批准|批下)|(?:批准|批下).{0,12}(?:预算|经费)",
    "action_polite": r"随便|先看看|再看看|不用了.{0,4}谢|没兴趣|只是问问|就问问|不想推进|不考虑",
}


def validate_extraction(extraction, turns):
    """A quote is checked against the original turn, never normalized/model text."""
    output = extraction.model_dump()
    by_id = {t.id: t for t in turns}
    issues = []

    def validate_evidence(evs, path, customer=True):
        if not evs:
            issues.append({"code": "EVIDENCE_MISSING", "field": path, "message": "缺少原文证据"})
            return False
        valid = True
        has_customer = False
        for ev in evs:
            turn = by_id.get(ev["turn_id"])
            if not turn or ev["quote"] not in turn.text:
                issues.append({"code": "QUOTE_MISMATCH", "field": path, "message": "引用无法在指定原文轮次定位"})
                valid = False
            elif turn.speaker in ('other','excluded','pending'):
                issues.append({"code":"EXCLUDED_SPEAKER","field":path,"message":"其他/系统播报及明确保留存疑的语句不能作为业务事实或意向证据"})
                valid = False
            elif turn.speaker == "customer":
                has_customer = True
        if customer and not has_customer:
            issues.append({"code": "SPEAKER_MISMATCH", "field": path, "message": "缺少客户侧证据"})
            valid = False
        return valid

    for name, fact in output["fields"].items():
        fact["validation_status"] = "valid"
        if fact["value"] is None:
            continue
        ok = validate_evidence(fact["evidence"], name)
        value = fact["value"]
        if name in COUNTS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 100000 or fact["qualifier"] == "range":
                ok = False
        elif not isinstance(value, str):
            ok = False
        if name == "contact_title" and isinstance(value, str):
            quotes = " ".join(e["quote"] for e in fact["evidence"])
            if not any(word in quotes for word in ["经理", "老板", "负责人", "主管", "总监", "设计师", "采购", "助理", "负责", "董事", "创始人"]):
                ok = False
        if name in ("raw_company", "brand_name", "contact_name", "contact_title", "phone") and isinstance(value, str):
            normalized = lambda s: re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))
            if not any(normalized(value) in normalized(e["quote"]) for e in fact["evidence"]):
                ok = False
        if name == "team_count":
            customer_quotes = " ".join(e["quote"] for e in fact["evidence"] if by_id.get(e["turn_id"]) and by_id[e["turn_id"]].speaker == "customer")
            if not re.search(r"设计部|设计团队|设计组", customer_quotes):
                ok = False
        if name == "shejishi":
            customer_quotes = " ".join(e["quote"] for e in fact["evidence"] if by_id.get(e["turn_id"]) and by_id[e["turn_id"]].speaker == "customer")
            if "设计师" not in customer_quotes:
                ok = False
        if name == "phone" and isinstance(value, str):
            if not re.fullmatch(r"1[3-9\dxX*][\dxX*]{9}", value.replace(" ", "")):
                ok = False
        if name in COUNTS and isinstance(value, int):
            quotes = " ".join(e["quote"] for e in fact["evidence"])
            digits = re.findall(r"\d+", quotes)
            # If the quote contains Arabic counts, the proposed number must appear.
            if digits and str(value) not in digits:
                ok = False
        if not ok:
            fact.update(value=None, validation_status="unsupported")
            issues.append({"code": "FIELD_UNSUPPORTED", "field": name, "message": "字段缺证、类型或业务口径不成立"})
    for dimension, signal in output["signals"].items():
        signal["validation_status"] = "valid"
        if signal["kind"] == "unknown":
            continue
        if signal["kind"] not in TIERS[dimension] or not validate_evidence(signal["evidence"], f"signals.{dimension}"):
            signal.update(kind="unknown", validation_status="unsupported")
            continue
        customer_quotes = " ".join(e["quote"] for e in signal["evidence"] if by_id[e["turn_id"]].speaker == "customer")
        gate = SEMANTIC_GATES.get(signal["kind"])
        valid_semantics = not gate or bool(re.search(gate, customer_quotes))
        if signal["kind"] == "budget_approved" and not re.search(r"报价|价格|费用", customer_quotes):
            valid_semantics = False
        if signal["kind"] == "time_soon" and re.search(r"下个月|下月", customer_quotes) and not re.search(r"一个月内|本月|本周|下周|月底", customer_quotes):
            valid_semantics = False
        if signal["kind"] in ("time_soon", "action_booked", "budget_approved") and re.search(r"如果|假如|尚未|还没|未批|取消", customer_quotes):
            valid_semantics = False
        if not valid_semantics:
            issues.append({"code": "SIGNAL_UNSUPPORTED", "field": f"signals.{dimension}", "message": "引用未明确支持该信号；未知或条件表达不能当作肯定/否定承诺"})
            signal.update(kind="unknown", validation_status="unsupported")
    if output["call_type"] != "unknown" and not validate_evidence(output["classification_evidence"], "call_type", customer=False):
        output["call_type"] = "unknown"
    if output["do_not_contact"] or output["refusal"]:
        if not validate_evidence(output["policy_evidence"], "contact_policy"):
            output["do_not_contact"] = output["refusal"] = False
        else:
            policy_quotes = " ".join(e["quote"] for e in output["policy_evidence"] if by_id[e["turn_id"]].speaker == "customer")
            if output["refusal"] and not re.search(r"不需要|没(?:有)?(?:当前|采购)?需求|不买了|取消试用|先不试了|没有更换计划|不考虑(?:购买|采购)|别再|不要再|别打电话", policy_quotes):
                output["refusal"] = False
                issues.append({"code": "POLICY_UNSUPPORTED", "field": "refusal", "message": "缺少明确拒绝当前机会的证据"})
            if output["do_not_contact"] and not re.search(r"不要再联系|别再(打|联系)|别打电话|不要再打|不想接到.*电话|别发.*营销|不要.*营销", policy_quotes):
                output["do_not_contact"] = False
                issues.append({"code": "POLICY_UNSUPPORTED", "field": "do_not_contact", "message": "缺少明确拒联证据"})
    # Do not let a model's mistaken classification override an explicit stop request.
    for turn in turns:
        if turn.speaker == "customer" and re.search(r"不要再联系|别再(打|联系)|别打电话|不要再打|不想接到.*电话", turn.text):
            output["do_not_contact"] = True
            output["refusal"] = True
            output["policy_evidence"] = [{"turn_id": turn.id, "quote": turn.text}]
    return output, issues
