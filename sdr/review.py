from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import uuid

from .entities import source_valid, captured_entity
from .engine import recalculate
from .models import ReviewRequest
from .scoring import TIERS
from .store import Conflict, NotFound, dumps, now


class ReviewError(Exception):
    pass


def revise(record, request: ReviewRequest, actor, settings):
    result = deepcopy(record["result"])
    if not result:
        raise ReviewError("当前没有可编辑的分析结果，请重新分析。")
    before = deepcopy(result)
    turns = {t["id"]: t for t in result["turns"]}
    if any(t not in turns for t in request.evidence_turn_ids):
        raise ReviewError("证据轮次不存在，请从原文选择。")
    evidence = [{"turn_id": tid, "quote": turns[tid]["text"]} for tid in request.evidence_turn_ids]
    customer_text = " ".join(turns[t]["text"] for t in request.evidence_turn_ids if turns[t]["speaker"] == "customer")
    external = bool(request.source_reference.strip())
    if external and not re.match(r"^(https://|demo://|crm:)", request.source_reference):
        raise ReviewError("外部证据请使用 https://、crm: 或 demo:// 来源引用。")
    if record["request"]["data_mode"] == "real" and request.source_reference.startswith("demo://"):
        raise ReviewError("真实线索不能使用演示证据。")
    changed = bool(request.fields or request.signal_kinds or request.entity_id or request.call_type or request.intent_level)
    qcc_entity = bool(request.entity_id) and any(
        candidate.get("source_type") == "qcc_mcp" and candidate.get("id") == request.entity_id
        for candidate in result["entity"].get("candidates", []))
    qcc_entity_only = qcc_entity and not request.fields and not request.signal_kinds and not request.call_type and not request.intent_level
    if changed and not (customer_text or external or qcc_entity_only):
        raise ReviewError("修正必须附客户原文轮次或可追溯的外部来源。")
    for name, value in request.fields.items():
        if name not in result["extraction"]["fields"]:
            raise ReviewError("不允许修改这个字段：" + name)
        if name in ("mendian", "shejishi", "team_count"):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000):
                raise ReviewError("人数和门店数必须是非负整数或空值。")
        elif value is not None and (not isinstance(value, str) or len(value) > 2000):
            raise ReviewError("字段必须是长度不超过2000的文本或空值。")
        if name == "phone" and value and not re.fullmatch(r"1[3-9][\dxX*]{9}", value):
            raise ReviewError("手机号需为11位号码或保留x的脱敏形式。")
        if name in ("contact_name", "contact_title", "phone") and value and not external and str(value) not in customer_text:
            raise ReviewError("联系人字段的新值不在所选客户原文中，请补充外部证据。")
        if name == "shejishi" and value is not None and not external and "设计师" not in customer_text:
            raise ReviewError("设计部人数不足以证明设计师人数，请补充口径证据。")
        result["extraction"]["fields"][name] = {"value": value, "evidence": evidence,
                    "qualifier": "exact", "validation_status": "human_attested", "source_reference": request.source_reference,
                    "reviewer": actor, "reason": request.reason}
    for dimension, kind in request.signal_kinds.items():
        if dimension not in TIERS or kind != "unknown" and kind not in TIERS[dimension]:
            raise ReviewError("评分信号不属于该维度。")
        result["extraction"]["signals"][dimension] = {"kind": kind, "evidence": evidence, "note": request.reason,
                    "validation_status": "human_attested", "source_reference": request.source_reference}
    if request.call_type:
        result["extraction"]["call_type"] = request.call_type
        result["extraction"]["classification_evidence"] = evidence
    if request.intent_level:
        if result["extraction"].get("do_not_contact") or result["extraction"].get("refusal"):
            raise ReviewError("拒联或明确拒绝记录不能覆盖意向。")
        if not request.recording_reviewed:
            raise ReviewError("请先核听录音（文字记录核对原文），再确认人工定性。")
        result["human_level"] = request.intent_level
        result["manual_intent_review"] = {"actor": actor, "at": now(), "reason": request.reason,
                                         "recording_reviewed": True, "level": request.intent_level}
    high_impact = bool(external and changed) or bool(request.intent_level)
    if request.entity_id:
        entity = next((c for c in result["entity"]["candidates"] if c["id"] == request.entity_id), None)
        if not entity:
            raise ReviewError("请选择当前检索结果中的主体；没有正确候选时应暂缓并补企业资料。")
        if not source_valid(entity, record["request"]["data_mode"]):
            raise ReviewError("该主体来源未核验、已过期或属于不匹配的数据模式。")
        qcc_attested = entity.get("source_type") == "qcc_mcp"
        if not qcc_attested and (len(request.relation_note.strip()) < 5 or not (external or entity.get("relation_supported"))):
            raise ReviewError("确认主体需要说明客户归属，并提供关联证据来源。")
        selected_before = (result["entity"].get("selected") or {}).get("id")
        high_impact |= result["entity"].get("status") == "suggested" or selected_before != request.entity_id
        result["entity"].update(status="confirmed", selected={**entity, "relation_supported": True,
                                "relation_note": request.relation_note, "source_reference": request.source_reference,
                                "verification_method": "human_confirmed_qcc" if qcc_attested else "human_attestation",
                                "confirmed_by": actor, "confirmed_at": now()},
                                quality=1.0, missing=[])
    if "raw_company" in request.fields and not request.entity_id:
        # A changed name invalidates the old entity relation; never keep an unrelated verified title.
        result["entity"].update(status="unresolved", selected=None, quality=.25, mention=request.fields["raw_company"])
    if 'company_name' in request.model_fields_set:
        if request.entity_id:
            raise ReviewError('企业名称填写与旧候选选择不能同时提交。')
        result['entity'] = captured_entity(result['extraction']['fields'], record['request']['data_mode'],
                                           name=request.company_name, manual=True)
        result['entity']['entered_by'] = actor
        result['entity']['entered_at'] = now()
        changed = True
    review = result["review"]
    if request.action == "second_review":
        if changed:
            raise ReviewError("独立复核只能确认当前修订；需要修改请先另存草稿。")
        if review.get("stage") != "secondary" or review.get("primary_actor") == actor:
            raise ReviewError("需要另一名审核人对当前修订进行独立复核。")
        review.update(status="approved", stage="complete", secondary_actor=actor)
        result["validation_acknowledged"] = True
        result["error_resolved"] = True
    elif request.action == "approve":
        if review.get("stage") == "secondary" and not changed:
            raise ReviewError("此记录等待独立复核，请使用另一审核身份提交复核。")
        result["validation_acknowledged"] = True
        result["error_resolved"] = True
        if high_impact or changed and review.get("stage") == "secondary":
            review.update(status="pending", stage="secondary", primary_actor=actor)
        else:
            review.update(status="approved", stage="complete", primary_actor=actor)
    elif request.action == "save":
        review.update(status="in_review", stage="secondary" if high_impact or review.get("stage") == "secondary" else "primary", primary_actor=actor)
        result["validation_acknowledged"] = False if changed else result.get("validation_acknowledged", False)
    elif request.action == "defer":
        review.update(status="deferred", primary_actor=actor)
    elif request.action == "reject":
        review.update(status="rejected", primary_actor=actor)
    result["review"] = review
    result = recalculate(result, record["request"], settings)
    if request.action in ("approve", "second_review") and result["blockers"]:
        raise ReviewError("尚不能批准：" + "、".join(result["reason_labels"][c] for c in result["blockers"]))
    result["human_notes"] = {"reason": request.reason, "source_reference": request.source_reference,
                             "evidence_turn_ids": request.evidence_turn_ids, "actor": actor, "at": now()}
    feedback = None
    if request.entity_id and (before["entity"].get("selected") or {}).get("id") != request.entity_id:
        feedback = {"kind": "entity_alias", "payload": {"mention": before["entity"].get("mention"),
                    "entity_id": request.entity_id, "area": result["extraction"]["fields"]["area"]["value"],
                    "scope": record["id"], "data_mode": record["request"]["data_mode"],
                    "source_reference": request.source_reference, "relation_note": request.relation_note}}
    elif request.signal_kinds or request.fields:
        feedback = {"kind": "field_correction", "payload": {"fields": request.fields, "signals": request.signal_kinds,
                    "reason": request.reason, "scope": record["id"], "data_mode": record["request"]["data_mode"]}}
    return result, feedback


class ReviewService:
    def __init__(self, store, settings, guarded=False):
        self.store, self.settings = store, settings
        self.guarded = guarded

    def apply(self, aid, request, actor):
        return self.store.mutate(aid, request.expected_revision, actor, request.action, request.reason,
                                 lambda record: revise(record, request, actor, self.settings), authorize=self.guarded)

    def batch_transform(self, record, payload, actor):
        if payload["action"] == "set_intent":
            result = deepcopy(record["result"])
            if not result or result["extraction"]["call_type"] != "sales" or result["extraction"]["do_not_contact"] or result["extraction"]["refusal"]:
                raise ReviewError("无效、服务、拒联或明确拒绝记录不能批量覆盖意向。")
            if not payload.get("intent_level"):
                raise ReviewError("请选择要订正的意向等级。")
            if result["intent"].get("requires_manual_intent") and not result.get("manual_intent_review"):
                raise ReviewError("异常分数必须逐条核听并人工定性，不能批量覆盖。")
            result["human_level"] = payload["intent_level"]
            result["review"].update(status="pending", stage="secondary", primary_actor=actor)
            result["human_notes"] = {"actor": actor, "reason": payload["reason"], "at": now()}
            recalculate(result, record["request"], self.settings)
            return result, {"kind": "intent_override", "payload": {"level": payload["intent_level"], "reason": payload["reason"], "scope": record["id"]}}
        request = ReviewRequest(expected_revision=record["revision"], action=payload["action"], reason=payload["reason"])
        return revise(record, request, actor, self.settings)

    def preview(self, payload, actor):
        if len({i["id"] for i in payload["items"]}) != len(payload["items"]):
            raise ReviewError("批次中有重复记录。")
        impact = []
        for item in payload["items"]:
            if self.guarded:
                from .workspace import require_analysis
                with self.store.connection() as db:
                    require_analysis(db, item['id'], actor)
            record = self.store.get(item["id"])
            try:
                if record["revision"] != item["revision"]:
                    raise Conflict("版本已变化，请刷新")
                if record["status"] not in ("completed", "failed"):
                    raise Conflict("分析尚未完成")
                result, _ = self.batch_transform(record, payload, actor)
                impact.append({"id": item["id"], "revision": item["revision"], "title": record["request"]["title"],
                               "before": record["result"]["review"]["status"], "after": result["review"]["status"], "eligible": True})
            except (ReviewError, Conflict) as exc:
                impact.append({"id": item["id"], "title": record["request"]["title"], "eligible": False, "reason": str(exc)})
        preview_id = "preview_" + uuid.uuid4().hex[:16]
        with self.store.connection() as db:
            db.execute("INSERT INTO previews(id,actor,payload,created_at) VALUES(?,?,?,?)", (preview_id, actor, dumps(payload), now()))
        return {"id": preview_id, "impact": impact, "can_commit": all(i["eligible"] for i in impact), "expires_in": 300}

    def commit(self, preview_id, actor):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            preview = db.execute("SELECT * FROM previews WHERE id=?", (preview_id,)).fetchone()
            if not preview:
                raise NotFound("批量预览不存在")
            if preview["actor"] != actor or preview["used"]:
                raise Conflict("此预览不属于当前审核人或已经提交。")
            if (datetime.now(timezone.utc) - datetime.fromisoformat(preview["created_at"])).total_seconds() > 300:
                raise Conflict("预览已过期，请重新预览。")
            payload = json.loads(preview["payload"])
            records = []
            for item in payload["items"]:
                records.append(self.store.mutate(item["id"], item["revision"], actor, "batch_" + payload["action"], payload["reason"],
                               lambda record: self.batch_transform(record, payload, actor), batch_id=preview_id, db=db, authorize=self.guarded))
            db.execute("UPDATE previews SET used=1 WHERE id=?", (preview_id,))
            return records

    def publish_feedback(self, fid, actor):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.guarded:
                from .workspace import require_admin
                require_admin(db, actor)
            row = db.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
            if not row:
                raise NotFound("反馈不存在")
            if row["status"] != "pending":
                raise Conflict("反馈状态已变化，请刷新。")
            record = self.store.decode(db.execute("SELECT * FROM analyses WHERE id=?", (row["analysis_id"],)).fetchone())
            if record["result"]["review"]["status"] != "approved":
                raise ReviewError("来源记录尚未完成审核，不能发布反馈。")
            if row["actor"] == actor:
                raise ReviewError("反馈需由另一名审核人验证后发布。")
            payload = json.loads(row["payload"])
            if row["kind"] == "entity_alias":
                selected = record["result"]["entity"].get("selected")
                if not selected or selected["id"] != payload["entity_id"] or not payload.get("mention") or not payload.get("area"):
                    raise ReviewError("主体、别名或地区范围不完整，或当前审核结果已变化。")
                if not source_valid(selected, record["request"]["data_mode"]):
                    raise ReviewError("主体证据已失效，不能发布。")
            validation = {"source_revision": record["revision"], "checks": ["approved_source", "independent_reviewer", "scope_bound"],
                          "usage": "retrieval_candidates_only" if row["kind"] == "entity_alias" else "curated_development_sample",
                          "never_auto_verifies_relation": True}
            db.execute("UPDATE feedback SET status='published',published_by=?,published_at=?,validation=? WHERE id=?",
                       (actor, now(), dumps(validation), fid))
            return {"id": fid, "status": "published", "validation": validation}

    def revoke_feedback(self, fid, actor):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.guarded:
                from .workspace import require_admin
                require_admin(db, actor)
            row = db.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
            if not row:
                raise NotFound("反馈不存在")
            if row["status"] != "published":
                raise Conflict("只能撤回已发布反馈。")
            validation = json.loads(row["validation"] or "{}")
            validation.update(revoked_by=actor, revoked_at=now())
            db.execute("UPDATE feedback SET status='revoked',validation=? WHERE id=?", (dumps(validation), fid))
            return {"id": fid, "status": "revoked"}
