import json

from sdr.preprocess import split_turns, redact_for_model, restore_placeholders

VALID_ID = '330106198606200012'      # checksum 2
INVALID_ID = '330106198606200013'    # wrong checksum
VALID_CARD = '4111111111111111'      # Luhn valid
INVALID_CARD = '4111111111111112'    # Luhn invalid


def test_resident_id_with_valid_checksum_is_redacted_and_restored():
    turns = split_turns(f'客户：我的身份证号是{VALID_ID}，你记一下。')
    safe, mapping = redact_for_model(turns)
    assert safe[0]['text'] == f'我的身份证号是[IDCARD_1]，你记一下。'
    assert VALID_ID not in json.dumps(safe)
    assert mapping == {'[IDCARD_1]': VALID_ID}
    assert restore_placeholders(safe[0]['text'], mapping) == turns[0].text


def test_resident_id_with_bad_checksum_is_left_untouched():
    turns = split_turns(f'客户：编号{INVALID_ID}可能记错了。')
    safe, mapping = redact_for_model(turns)
    assert INVALID_ID in safe[0]['text'] and not mapping


def test_bank_card_with_luhn_pass_is_redacted_including_spaced_form():
    spaced = '4111 1111 1111 1111'
    turns = split_turns(f'客户：卡号{VALID_CARD}，另一张{spaced}。')
    safe, mapping = redact_for_model(turns)
    assert VALID_CARD not in safe[0]['text'] and spaced not in safe[0]['text']
    assert mapping == {'[BANKCARD_1]': VALID_CARD, '[BANKCARD_2]': spaced}
    restored = restore_placeholders(safe[0]['text'], mapping)
    assert VALID_CARD in restored and spaced in restored


def test_bank_card_with_luhn_failure_and_business_figures_are_untouched():
    turns = split_turns(f'客户：设计部大概15个人，门店有23家，流水号{INVALID_CARD}。')
    safe, mapping = redact_for_model(turns)
    assert INVALID_CARD in safe[0]['text']
    assert '15个人' in safe[0]['text'] and '23家' in safe[0]['text'] and not mapping


def test_email_is_redacted():
    turns = split_turns('客户：发到 zhang.san+work@example.com 吧。')
    safe, mapping = redact_for_model(turns)
    assert 'example.com' not in safe[0]['text'] and mapping == {'[EMAIL_1]': 'zhang.san+work@example.com'}


def test_mixed_pii_gets_unique_keys_and_round_trips():
    text = f'客户：手机13812345678，证件{VALID_ID}，邮箱a@b.co，卡{VALID_CARD}。'
    turns = split_turns(text)
    safe, mapping = redact_for_model(turns)
    payload = json.dumps(safe, ensure_ascii=False)
    for secret in ('13812345678', VALID_ID, 'a@b.co', VALID_CARD):
        assert secret not in payload
    assert sorted(mapping) == ['[BANKCARD_1]', '[EMAIL_1]', '[IDCARD_1]', '[PHONE_1]']
    assert restore_placeholders(safe[0]['text'], mapping) == turns[0].text


def test_already_masked_phone_and_short_numbers_stay_verbatim():
    turns = split_turns('客户：你加这个手机号就行，138xxxx1234，门店就2家。')
    safe, mapping = redact_for_model(turns)
    assert safe[0]['text'] == turns[0].text
    assert '138xxxx1234' in safe[0]['text'] and not mapping
