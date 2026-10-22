
SCORE_VERSION = "intent-3.1"
TIERS = {
    "N": {"need_none": 0, "need_general": 15, "need_problem": 25, "need_impact": 35},
    "T": {"time_none": 0, "time_later": 10, "time_1_3m": 20, "time_soon": 25},
    "B": {"budget_none": 0, "budget_discuss": 10, "budget_range": 15, "budget_approved": 20},
    "E": {"action_polite": 0, "action_contact": 5, "action_request": 10, "action_booked": 20},
}
WEIGHTS = {"N": 35, "T": 25, "B": 20, "E": 20}
DIMENSIONS = {"N": "需求", "T": "时间", "B": "预算", "E": "行动"}
LABELS = {
    "need_none": "明确无当前需求", "need_general": "泛泛了解", "need_problem": "明确具体问题", "need_impact": "问题及受影响工作环节明确",
    "time_none": "明确无推进计划", "time_later": "三个月以后", "time_1_3m": "1—3个月／下月试用期望", "time_soon": "明确一个月内窗口",
    "budget_none": "本项目无支出安排", "budget_discuss": "愿讨论付费", "budget_range": "有可接受价格范围", "budget_approved": "预算获批且覆盖报价",
    "action_polite": "仅礼貌回应", "action_contact": "同意具体联系渠道", "action_request": "主动要求方案并提问", "action_booked": "确认日期及参与者",
    "unknown": "证据缺失（未提及或未获得有效证据）",
}


def final_level(scores):
    n, t, b, e = scores
    total = n + t + b + e
    if total >= 60 and n >= 25 and (t >= 20 or e == 20):
        return "high"
    return "medium" if total >= 30 else "low" if total > 5 else None


def assess(extraction):
    signals = extraction["signals"]
    unknown = [d for d in TIERS if signals[d]["kind"] not in TIERS[d]]
    scores = {d: TIERS[d].get(signals[d]["kind"], 0) for d in TIERS}
    total = sum(scores.values())
    coverage = sum(WEIGHTS[d] for d in TIERS if d not in unknown) / 100
    level = final_level(tuple(scores[d] for d in TIERS))
    status = "stable" if level else "abnormal"
    policy = extraction.get("do_not_contact") or extraction.get("refusal")
    if extraction["call_type"] in ("service", "invalid", "unknown") and not policy:
        level, status = None, "not_applicable" if extraction["call_type"] != "unknown" else "insufficient"
    elif policy:
        level, status = "low", "policy"
    elif extraction.get("conflicts"):
        level, status = None, "provisional"
    quality = 1.0 if status == "policy" else .25 if level is None else 1.0 if not unknown else .6
    return {"scores": scores, "total": total, "unknown_dimensions": unknown,
            "interval": [total, total], "coverage": coverage,
            "possible_levels": [level] if level else [], "level": level, "status": status,
            "requires_manual_intent": total <= 5 and not policy,
            "boundary": False, "quality": quality,
            "version": SCORE_VERSION, "dimensions": DIMENSIONS, "labels": LABELS}
