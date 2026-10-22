from copy import deepcopy
import json
import pytest

from sdr.entities import resolve, source_valid, similarity
from sdr.extractors import rule_extract
from sdr.models import Extraction, Fact, Evidence, Signal, AnalyzeRequest, Lead
from sdr.preprocess import split_turns, redact_for_model, restore_placeholders
from sdr.scoring import assess
from sdr.validation import validate_extraction


def test_assignment_is_auditable_not_ready(system):
    _, _, analyze = system
    row = analyze("assignment")
    r, lead = row["result"], row["result"]["lead"]
    assert row["status"] == "completed"
    assert r["intent"]["scores"] == {"N":35,"T":20,"B":15,"E":5}
    assert lead["intent_level"] == "high" and lead["confidence_score"] == .5
    assert lead["contact_name"] == "张总"
    assert lead["contact_title"] is None and lead["company_name"] == "木里木外"
    assert lead["shejishi"] is None and r["extraction"]["fields"]["team_count"]["value"] == 15
    assert lead["call_time"] is None and lead["need_human_review"] is True
    assert r["delivery_status"] == "blocked" and "INTENT_BOUNDARY" not in r["review_reasons"]
    assert "参数化" not in lead["demand_summary"]
    assert len(Lead.model_validate(lead).model_dump()) == 15


@pytest.mark.parametrize("sample_id,level", [("verified","high"),("casual","low"),("dnc","low"),("service",None),("conditional","low"),("competitor","high"),("injection",None)])
def test_business_boundaries(system,sample_id,level):
    _,_,analyze=system
    r=analyze(sample_id)["result"]
    assert r["intent"]["level"] == level


def test_stop_contact_wins_even_if_model_omits_it():
    turns=split_turns("客户：不要再联系我，别打电话。")
    parsed=Extraction(call_type="sales",classification_evidence=[Evidence(turn_id="T01",quote=turns[0].text)])
    result,_=validate_extraction(parsed,turns)
    assert result["do_not_contact"] is True
    assert assess(result)["level"]=="low"


def test_stop_contact_never_suggests_nurture(system):
    _,_,analyze=system
    r=analyze("dnc")["result"]
    assert "停止" in r["lead"]["next_action"]
    assert r["delivery_status"]=="blocked"


def test_headcount_and_denied_person_are_separate(system):
    _,_,analyze=system
    f=analyze("headcount")["result"]["extraction"]["fields"]
    assert f["shejishi"]["value"]==8 and f["team_count"]["value"]==15
    assert f["contact_name"]["value"] is None and f["contact_title"]["value"]=="助理"


def test_sales_claims_and_fake_quotes_are_rejected():
    turns=split_turns("销售：我是总经理，手机号13812345678，我们有20名设计师。\n客户：我先看看。")
    ex=Extraction()
    ex.fields.contact_title=Fact(value="总经理",evidence=[Evidence(turn_id="T01",quote=turns[0].text)])
    ex.fields.shejishi=Fact(value=20,evidence=[Evidence(turn_id="T02",quote="我们有20名设计师")])
    result,issues=validate_extraction(ex,turns)
    assert result["fields"]["contact_title"]["value"] is None
    assert result["fields"]["shejishi"]["value"] is None
    assert {i["code"] for i in issues}>={"SPEAKER_MISMATCH","QUOTE_MISMATCH"}


def test_title_and_designer_count_require_correct_scope():
    turns=split_turns("客户：大家叫我张总，设计部大概15个人。")
    ex=Extraction()
    ev=[Evidence(turn_id="T01",quote=turns[0].text)]
    ex.fields.contact_title=Fact(value="总经理",evidence=ev)
    ex.fields.shejishi=Fact(value=15,evidence=ev)
    result,_=validate_extraction(ex,turns)
    assert result["fields"]["contact_title"]["value"] is None
    assert result["fields"]["shejishi"]["value"] is None


def test_unknown_not_normalized_into_high():
    ex=Extraction(call_type="sales").model_dump()
    ex["signals"]["B"]["kind"]="budget_approved"
    r=assess(ex)
    assert r["level"] == "low" and r["total"]==20 and r["unknown_dimensions"]==["N","T","E"] and r["coverage"]==.2


def test_model_cannot_turn_undecided_budget_and_timing_into_zero():
    turns=split_turns("客户：预算还没批，如果免费才考虑试用。\n客户：这个还说不准，也没讨论具体怎么联系。")
    ex=Extraction(call_type="sales",classification_evidence=[Evidence(turn_id="T01",quote=turns[0].text)])
    ex.signals.B=Signal(kind="budget_none",evidence=[Evidence(turn_id="T01",quote=turns[0].text)])
    ex.signals.T=Signal(kind="time_none",evidence=[Evidence(turn_id="T02",quote=turns[1].text)])
    ex.signals.E=Signal(kind="action_polite",evidence=[Evidence(turn_id="T02",quote=turns[1].text)])
    output,issues=validate_extraction(ex,turns)
    assert all(output["signals"][d]["kind"]=="unknown" for d in ("T","B","E"))
    assert len([i for i in issues if i['code']=='SIGNAL_UNSUPPORTED'])==3
    assert assess(output)['level'] is None


def test_numeric_count_requires_matching_number():
    turns=split_turns("客户：设计师有8人。")
    ex=Extraction();ex.fields.shejishi=Fact(value=15,evidence=[Evidence(turn_id="T01",quote=turns[0].text)])
    result,_=validate_extraction(ex,turns)
    assert result['fields']['shejishi']['value'] is None


def test_possible_levels_respects_high_gate():
    ex=Extraction(call_type="sales").model_dump()
    for d,kind in {"N":"need_general","T":"time_soon","B":"budget_approved"}.items():ex["signals"][d]["kind"]=kind
    r=assess(ex)
    assert r["total"]==60 and r["possible_levels"]==["medium"] and r["level"]=="medium"


def test_brand_is_not_a_company_and_phonetics_recall(system):
    _,_,analyze=system
    r=analyze("ambiguous")["result"]
    assert r["entity"]["status"]=="captured"
    assert r["entity"]["candidates"]==[]
    assert r["lead"]["company_name"] == r["entity"]["mention"]
    assert r["entity"]["selected"]["verified_registry"] is False
    assert similarity("七木定制","栖木定制")>.9


def test_synthetic_entities_cannot_verify_real_input(settings,samples):
    turns=split_turns(samples["verified"]["transcript"])
    ex,_=rule_extract(turns);valid,_=validate_extraction(ex,turns)
    r=resolve(valid["fields"],turns,settings,"real")
    assert r["status"]=="unresolved" and r["selected"] is None


def test_expired_source_never_verifies():
    entity={"id":"REAL-1","verified_registry":True,"source_url":"https://example.org/company",
            "data_mode":"real","observed_at":"2020-01-01","valid_until":"2020-02-01"}
    assert source_valid(entity,"real") is False


def test_phone_redaction_round_trips_without_exposing_to_model():
    turns=split_turns("客户：手机号13812345678，你加我。")
    safe,mapping=redact_for_model(turns)
    assert "13812345678" not in json.dumps(safe)
    restored=restore_placeholders({"quote":safe[0]["text"],"phone":"[PHONE_1]"},mapping)
    assert restored["quote"]==turns[0].text and restored["phone"]=="13812345678"


def test_repetitions_not_deleted():
    turns=split_turns("客户：我们是某品牌。\n客户：不对，我是该品牌代理。")
    assert len(turns)==2


def test_call_time_requires_trusted_timezone():
    with pytest.raises(ValueError):
        AnalyzeRequest(transcript="客户：你好",idempotency_key="valid-key",call_time="2026-10-20T12:00:00")
def test_designer_count_does_not_infer_department_total():
    from sdr.models import Extraction, Fact, Evidence, Turn
    from sdr.validation import validate_extraction
    t=Turn(id='T01',speaker='customer',text='设计师有12人。')
    ex=Extraction()
    ex.fields.team_count=Fact(value=12,evidence=[Evidence(turn_id='T01',quote=t.text)])
    validated,issues=validate_extraction(ex,[t])
    assert validated['fields']['team_count']['value'] is None
    assert any(i['field']=='team_count' for i in issues)


def test_legal_suffix_does_not_dominate_entity_similarity():
    from sdr.entities import similarity
    assert similarity('青禾空间设计（杭州）有限公司','栖木家居设计（杭州）有限公司') < .58
