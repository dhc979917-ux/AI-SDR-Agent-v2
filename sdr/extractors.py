"""Two explicit modes: real LLM extraction and a limited, deterministic offline demo."""
import hashlib
import json
import re
import time

import httpx
from pydantic import ValidationError

from .config import ROOT
from .models import Evidence, Extraction, Fact, Signal
from .preprocess import redact_for_model, restore_placeholders
from .model_transport import json_chat_body,provider_error


def rule_extract(turns):
    result = Extraction()
    customers = [t for t in turns if t.speaker == "customer"]
    if not customers:
        result.call_type = "unknown"
        return result, {"provider": "rules", "model": "offline-rules-v1", "calls": 0, "tokens": 0}
    result.call_type = "sales"
    result.classification_evidence = [Evidence(turn_id=customers[0].id, quote=customers[0].text)]

    def evidence(t):
        return [Evidence(turn_id=t.id, quote=t.text)]

    def put(name, value, t, qualifier="exact"):
        setattr(result.fields, name, Fact(value=value, evidence=evidence(t), qualifier=qualifier))

    def signal(d, kind, t):
        setattr(result.signals, d, Signal(kind=kind, evidence=evidence(t)))

    pain_quotes = []
    for t in customers:
        s = t.text
        if re.search(r"不要再联系|别再(打|联系)|别打电话|不要再打", s):
            result.do_not_contact = result.refusal = True
            result.policy_evidence = evidence(t)
        if re.search(r"(?:暂时|现在|完全)?不需要|没有(?:采购)?需求|没需求|没有更换计划", s) and not re.search(r"不是(?:不|没)|并非", s):
            result.refusal = True
            result.policy_evidence = evidence(t)
            signal("N", "need_none", t)
        legal = re.search(r"(?:我们公司(?:全称)?(?:是|叫)|公司全称是|我们是)([\u4e00-\u9fff（）()A-Za-z]{2,40}(?:有限公司|有限责任公司|工作室))", s)
        mention = re.search(r"(?:叫|品牌是|做的品牌是)[‘“\"「]?([\u4e00-\u9fffA-Za-z]{2,16})[’”\"」]", s)
        if legal:
            put("raw_company", legal[1], t)
        elif mention:
            put("raw_company", mention[1], t)
            put("brand_name", mention[1], t)
        for area, full in {"杭州": "浙江省杭州市", "上海": "上海市", "北京": "北京市", "深圳": "广东省深圳市", "广州": "广东省广州市", "苏州": "江苏省苏州市", "成都": "四川省成都市"}.items():
            if area in s:
                put("area", full, t)
                break
        for word in ["全屋定制", "家具制造", "建材", "整装", "装修", "设计公司"]:
            if word in s:
                put("industry", "装企" if word in ("装修", "设计公司") else word, t)
                break
        phone = re.search(r"(?<!\d)(1[3-9][\dxX*]{9})(?!\d)", s)
        if phone:
            put("phone", phone[1], t)
        person = re.search(r"(?:我叫|我是)([\u4e00-\u9fff]{2,4})(?:[，。]|，?负责)", s)
        if person and person[1] not in ("设计师", "负责人", "采购", "老板", "助理"):
            put("contact_name", person[1], t)
        role = re.search(r"(?:我是|担任|职位是)(设计师|采购经理|设计总监|总经理|老板|助理|负责人)", s)
        if role:
            put("contact_title", role[1], t)
        for name, pattern in [
            ("shejishi", r"(?:设计师(?:有|是)?|其中设计师)(?:大概|约)?\s*(\d+)\s*(?:个|位|人|名)"),
            ("team_count", r"设计部(?:有)?(?:大概|约)?\s*(\d+)\s*(?:个|位|人|名)"),
            ("mendian", r"(?:门店(?:有)?\s*(\d+)\s*家|有\s*(\d+)\s*家门店)")]:
            m = re.search(pattern, s)
            if m:
                put(name, int(next(g for g in m.groups() if g)), t, "approximate" if re.search("大概|约", m[0]) else "exact")
        if re.search(r"出图慢|出图太慢|对接.*出错|生产.*出错|返工|效率低|图纸.*错误", s):
            signal("N", "need_impact", t)
            pain_quotes.append(t)
        elif re.search(r"软件不好用|系统太慢|操作复杂|功能不够", s):
            signal("N", "need_problem", t)
            pain_quotes.append(t)
        elif result.signals.N.kind == "unknown" and re.search("了解|看看|问问|试用|企业版", s):
            signal("N", "need_general", t)
        if re.search("没有计划|没计划|不着急|近期不考虑", s):
            signal("T", "time_none", t)
        elif re.search("明年|半年后|六个月后", s):
            signal("T", "time_later", t)
        elif re.search("下个月|下月|两个月|三个月", s) and not re.search("不想|不打算|如果", s):
            signal("T", "time_1_3m", t)
        elif re.search("本月|这周|本周|下周|月底|一个月内", s) and re.search("试用|上线|实施|演示", s) and not re.search("如果|不想|不打算", s):
            signal("T", "time_soon", t)
        if re.search("没有预算|没预算|不付费|不打算花钱", s):
            signal("B", "budget_none", t)
        elif re.search("预算.*已批|预算.*批下|预算.*获批", s) and re.search("报价.*(?:范围|覆盖|没问题)|覆盖.*报价", s):
            signal("B", "budget_approved", t)
        elif re.search(r"(?:预算|费用|一年|每年|年费).{0,18}(?:[一二三四五六七八九十几\d]+万|[\d]+元|几千)", s) and not re.search("如果|没批|未批|不接受|不能接受", s):
            signal("B", "budget_range", t)
        elif re.search("可以付费|愿意付费|考虑付费", s):
            signal("B", "budget_discuss", t)
        if re.search("加.*(?:微信|手机号)|发.*邮箱|微信.*联系", s) and not re.search("别加|不要加|不用加", s):
            signal("E", "action_contact", t)
        if re.search("请.*(?:方案|报价|演示)|安排.*演示", s) and re.search("怎么|如何|能否|能不能|多少钱|具体", s):
            signal("E", "action_request", t)
        if re.search("确认|就定|约好了", s) and re.search(r"周[一二三四五六日天]|\d+月\d+日|明天", s) and re.search("我和|一起|同事|负责人|经理|团队", s) and re.search("演示|试用", s):
            signal("E", "action_booked", t)
        if result.signals.E.kind == "unknown" and re.search("随便问问|随便看看|先看看吧|不用了谢谢", s):
            signal("E", "action_polite", t)

    # Names in sales questions need explicit customer confirmation and both quotes.
    if result.fields.contact_name.value is None:
        for prev, cur in zip(turns, turns[1:]):
            match = re.search(r"(?:请问(?:您)?是|请问|是)([\u4e00-\u9fff]{1,3}(?:总|先生|女士))", prev.text)
            if prev.speaker == "sales" and cur.speaker == "customer" and match and re.match(r"(?:是的|对[，。]|是[，。])", cur.text):
                result.fields.contact_name = Fact(value=match[1], evidence=evidence(prev) + evidence(cur))
    if pain_quotes:
        # Extractive summary is deliberately conservative in offline mode.
        result.fields.demand_summary = Fact(value="；".join(t.text for t in pain_quotes), evidence=[evidence(t)[0] for t in pain_quotes])
    all_text = " ".join(t.text for t in customers)
    if re.search("售后|无法登录|退款|报修|投诉", all_text) and not re.search("增购|再买|新部门|换软件|采购", all_text):
        result.call_type = "service"
    elif re.search("空号|打错了|不是这个人", all_text) or (len(all_text) < 8 and not result.do_not_contact):
        result.call_type = "invalid"
    if re.search("先不试了|取消试用|不买了|撤回", all_text):
        t = next(t for t in reversed(customers) if re.search("先不试了|取消试用|不买了|撤回", t.text))
        result.refusal = True
        result.policy_evidence = evidence(t)
    return result, {"provider": "rules", "model": "offline-rules-v1", "calls": 0, "tokens": 0}


class ProviderError(Exception):
    pass


def llm_extract(turns, settings):
    if not settings.api_key:
        raise ProviderError("未配置模型密钥。请配置 SDR_LLM_API_KEY 或切换离线规则模式。")
    system = (ROOT / "prompts/extract_v2.md").read_text()
    safe_turns, substitutions = redact_for_model(turns)
    payload = {"turns": safe_turns, "schema": Extraction.model_json_schema()}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    usage, calls = 0, 0
    started = time.monotonic()
    last_error = ""
    with httpx.Client(timeout=httpx.Timeout(settings.timeout, connect=10), follow_redirects=False,trust_env=False) as client:
        for attempt in range(2):
            remaining = 58 - (time.monotonic() - started)
            if remaining < 3:
                raise ProviderError("模型处理已达到时间上限，请重试或进入人工审核。")
            body = json_chat_body(settings.model,messages,6000)
            calls += 1
            try:
                response = client.post(settings.base_url.rstrip("/") + "/chat/completions",
                                       headers={"Authorization": f"Bearer {settings.api_key}"}, json=body,
                                       timeout=min(settings.timeout, remaining))
            except httpx.TimeoutException as exc:
                raise ProviderError("模型服务超时，已保存通话，可人工处理或重试。") from exc
            except httpx.HTTPError as exc:
                raise ProviderError("模型网络连接失败，请检查服务地址和网络。") from exc
            if response.status_code != 200:
                # Never expose the upstream body, request headers or credentials.
                raise ProviderError(provider_error(response,'文字模型'))
            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                usage += data.get("usage", {}).get("total_tokens", 0)
                parsed = restore_placeholders(json.loads(content), substitutions)
                extracted = Extraction.model_validate(parsed)
                return extracted, {"provider": "llm", "model": data.get("model", settings.model), "calls": calls,
                                   "tokens": usage, "prompt_version": "extract-v2.1",
                                   "prompt_hash": hashlib.sha256(system.encode()).hexdigest()[:12]}
            except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
                last_error = type(exc).__name__
                # Redacted error paths suffice; do not echo data or secrets in a repair prompt.
                messages.append({"role": "user", "content": "上次结果未通过 JSON/Schema 验证（" + last_error + "）。请根据原始输入重新返回完整合法 JSON，严格匹配 schema 的键、枚举和类型。"})
    raise ProviderError("模型连续两次输出未通过结构校验，已转人工处理。")
