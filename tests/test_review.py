import pytest
from sdr.models import ReviewRequest
from sdr.review import ReviewService, ReviewError
from sdr.store import Conflict


def test_approval_preserves_original_and_revision(system,settings):
    store,_,analyze=system
    row=analyze();original=row["original"]
    service=ReviewService(store,settings)
    out=service.apply(row["id"],ReviewRequest(expected_revision=1,action="approve",reason="逐项核对客户证据，原始事实完整"),"A")
    assert out["result"]["review"]["status"]=="approved"
    assert out["result"]["delivery_status"]=="ready"
    assert out["result"]["production_eligible"] is False
    assert out["revision"]==2 and out["original"]==original
    assert len(store.events(row["id"]))==2
    with pytest.raises(Conflict):service.apply(row["id"],ReviewRequest(expected_revision=1,action="defer",reason="旧版本提交"),"B")


def test_blocked_lead_cannot_approve(system,settings):
    _,_,analyze=system;row=analyze("assignment");service=ReviewService(system[0],settings)
    with pytest.raises(ReviewError,match="尚不能批准"):
        service.apply(row["id"],ReviewRequest(expected_revision=1,action="approve",reason="直接批准试试"),"A")
    assert system[0].get(row["id"])["revision"]==1


def test_external_correction_needs_independent_reviewer(system,settings):
    store,_,analyze=system;row=analyze();service=ReviewService(store,settings)
    out=service.apply(row["id"],ReviewRequest(expected_revision=1,action="approve",reason="客户在演示CRM中补充联系号码",fields={"phone":"13900000000"},source_reference="demo://crm/contact-1"),"A")
    assert out["result"]["review"]["stage"]=="secondary"
    assert out["result"]["delivery_status"]=="blocked"
    with pytest.raises(ReviewError,match="另一名"):
        service.apply(row["id"],ReviewRequest(expected_revision=2,action="second_review",reason="自己再确认一遍"),"A")
    final=service.apply(row["id"],ReviewRequest(expected_revision=2,action="second_review",reason="独立核验来源与联系信息一致"),"B")
    assert final["result"]["delivery_status"]=="ready"
    assert final["result"]["lead"]["phone"]=="13900000000"


def test_plain_approve_cannot_bypass_secondary(system,settings):
    store,_,analyze=system;row=analyze();service=ReviewService(store,settings)
    service.apply(row["id"],ReviewRequest(expected_revision=1,action="approve",reason="外部证据补充职位",fields={"contact_title":"采购经理"},source_reference="demo://proof"),"A")
    with pytest.raises(ReviewError,match="独立复核"):
        service.apply(row["id"],ReviewRequest(expected_revision=2,action="approve",reason="试图跳过独立复核"),"A")


def test_draft_after_approval_revokes_ready(system,settings):
    store,_,analyze=system;row=analyze();service=ReviewService(store,settings)
    service.apply(row["id"],ReviewRequest(expected_revision=1,action="approve",reason="核对原文无误"),"A")
    out=service.apply(row["id"],ReviewRequest(expected_revision=2,action="save",reason="清空原先客户公司口述名称",fields={"raw_company":None},evidence_turn_ids=["T02"]),"A")
    assert out["result"]["entity"]["selected"] is None
    assert out["result"]["delivery_status"]=="blocked"


def test_batch_version_conflict_rolls_back_everything(system,settings):
    store,_,analyze=system;a,b=analyze(),analyze();service=ReviewService(store,settings)
    body={"items":[{"id":a["id"],"revision":1},{"id":b["id"],"revision":1}],"action":"defer","reason":"等待统一补证","intent_level":None}
    preview=service.preview(body,"A")
    service.apply(b["id"],ReviewRequest(expected_revision=1,action="save",reason="另一人已领取修改"),"B")
    with pytest.raises(Conflict):service.commit(preview["id"],"A")
    assert store.get(a["id"])["revision"]==1
    assert store.get(b["id"])["revision"]==2


def test_batch_commit_is_single_use(system,settings):
    store,_,analyze=system;row=analyze();service=ReviewService(store,settings)
    p=service.preview({"items":[{"id":row["id"],"revision":1}],"action":"defer","reason":"等待客户确认"},"A")
    service.commit(p["id"],"A")
    with pytest.raises(Conflict):service.commit(p["id"],"A")


def test_batch_override_cannot_change_stop_contact(system,settings):
    store,_,analyze=system;row=analyze("dnc");service=ReviewService(store,settings)
    p=service.preview({"items":[{"id":row["id"],"revision":1}],"action":"set_intent","intent_level":"high","reason":"尝试批量提升"},"A")
    assert p["can_commit"] is False


def test_corrections_require_evidence(system,settings):
    store,_,analyze=system;row=analyze();service=ReviewService(store,settings)
    with pytest.raises(ReviewError,match="证据|来源"):
        service.apply(row["id"],ReviewRequest(expected_revision=1,action="save",reason="没有证据的修改",fields={"contact_name":"王五"}),"A")


def test_feedback_requires_completed_review_and_other_actor(system,settings):
    store,_,analyze=system;row=analyze();service=ReviewService(store,settings)
    service.apply(row["id"],ReviewRequest(expected_revision=1,action="approve",reason="CRM中补全联系人职位",fields={"contact_title":"采购负责人"},source_reference="demo://contact"),"A")
    fid=store.feedback()[0]["id"]
    with pytest.raises(ReviewError):service.publish_feedback(fid,"B")
    service.apply(row["id"],ReviewRequest(expected_revision=2,action="second_review",reason="独立核验该职位信息"),"B")
    with pytest.raises(ReviewError):service.publish_feedback(fid,"A")
    assert service.publish_feedback(fid,"B")["status"]=="published"
    assert service.revoke_feedback(fid,"B")["status"]=="revoked"
