from sdr.models import AnalyzeRequest


def _run(store, engine, text, key):
    engine.settings.api_key = key
    request = AnalyzeRequest(transcript=text, title="auto roles", mode="rules",
                             data_mode="synthetic", idempotency_key="auto-roles-" + key).model_dump()
    record, _ = store.create(request, "auto-roles-" + key, "A")
    return engine.run(record["id"])["result"]


def test_unlabeled_text_falls_back_to_single_customer_turn(system):
    store, engine, _ = system
    result = _run(store, engine, "出图慢，想下个月试用，预算一年几万。", "")
    assert result["auto_roles"]["method"] == "single_customer_fallback"
    assert result["turns"][0]["speaker"] == "customer"
    assert result["extraction"]["call_type"] == "sales"


def test_unlabeled_text_uses_role_model_and_converts_pending(system, monkeypatch):
    store, engine, _ = system
    monkeypatch.setattr("sdr.engine.classify_roles", lambda text, settings: {
        "transcript": "销售：您好\n待确认：我们想下个月试用企业版软件\n待确认：嗯",
        "model": "test-role-model", "uncertain_count": 2,
    })
    result = _run(store, engine, "销售和客户的一段无标记转写", "test-key")
    assert result["auto_roles"]["method"] == "text_role_model"
    assert result["auto_roles"]["model"] == "test-role-model"
    assert all(turn["speaker"] != "unknown" for turn in result["turns"])
    assert any(turn["speaker"] == "customer" for turn in result["turns"])
    assert any(turn["speaker"] == "other" for turn in result["turns"])
    assert result["extraction"]["call_type"] == "sales"


def test_pending_role_is_excluded_and_flagged(system):
    store, engine, _ = system
    result = _run(store, engine, "销售：您好\n客户：我们做全屋定制，出图慢。\n待确认：这句听不清", "")
    assert result["role_pending_count"] == 1
    assert "ROLE_PENDING" in result["review_reasons"]
    assert result["lead"]["need_human_review"] is True
    assert any(turn["speaker"] == "pending" for turn in result["turns"])
    assert not any("这句听不清" in evidence["quote"]
                   for field in result["extraction"]["fields"].values()
                   for evidence in field["evidence"])
