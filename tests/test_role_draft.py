from sdr.role_draft import auto_label_draft, draft_has_customer, draft_pending_count, has_labels, label_unlabeled


def test_has_labels_detects_sales_and_customer():
    assert has_labels("销售：您好\n客户：想了解")
    assert not has_labels("这是一段没有角色标记的转写")


def test_auto_draft_promotes_customer_when_none():
    draft = "销售：您好\n待确认：我们想试用\n待确认：嗯"
    result = auto_label_draft(draft)
    assert "待确认" not in result
    assert "客户：我们想试用" in result
    assert "其他：嗯" in result


def test_auto_draft_preserves_existing_customer():
    draft = "销售：您好\n客户：我们想试用\n待确认：好的"
    result = auto_label_draft(draft)
    assert "客户：我们想试用" in result
    assert "待确认：好的" in result
    assert draft_pending_count(result) == 1


def test_auto_draft_converts_filler_pending_to_other():
    result = auto_label_draft("客户：我们想试用\n待确认：嗯")
    assert "其他：嗯" in result
    assert draft_pending_count(result) == 0


def test_auto_draft_keeps_pending_when_customer_exists():
    draft = "销售：您好\n客户：我们想试用\n待确认：这句听不清"
    result = auto_label_draft(draft)
    assert draft_has_customer(result)
    assert "待确认：这句听不清" in result
    assert draft_pending_count(result) == 1


def test_unlabeled_text_becomes_single_customer_turn():
    result = auto_label_draft("出图慢，想下个月试用")
    assert result == "客户：出图慢，想下个月试用"
    assert label_unlabeled("出图慢") == "客户：出图慢"
