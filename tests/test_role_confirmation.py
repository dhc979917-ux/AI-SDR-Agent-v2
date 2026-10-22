import pytest
from sdr.models import AnalyzeRequest, Extraction
from sdr.preprocess import split_turns
from sdr.validation import validate_extraction
from test_audio_roles import transcribed


@pytest.mark.parametrize('label,speaker',[('其他','other'),('存疑','excluded')])
def test_confirm_other_preserves_original_and_allows_analysis(team, monkeypatch,label,speaker):
    app, admin, alice, bob, customer, tid = transcribed(team, monkeypatch)
    app.state.audio.run(tid)
    row = alice.get('/api/transcriptions/' + tid).json()
    endpoint = f'/api/transcriptions/{tid}/confirm'
    pending = '待确认：系统来电播报。\n销售：您好。\n客户：我想试用。'
    assert alice.post(endpoint, json={'expected_revision': row['revision'], 'transcript': pending}).status_code == 422
    confirmed = pending.replace('待确认：', label+'：')
    response = alice.post(endpoint, json={'expected_revision': row['revision'], 'transcript': confirmed})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['status'] == 'confirmed' and saved['confirmed_text'] == confirmed
    assert saved['raw_text'] == row['raw_text'] and saved['role_result'] == row['role_result']
    req = AnalyzeRequest(transcript=confirmed, mode='rules', data_mode='synthetic',
                         transcription_id=tid, customer_id=customer['id'], idempotency_key='confirmed-other-test')
    analysis, _ = app.state.store.create(req.model_dump(), 'confirmed-other-test', 'alice', authorize=True)
    result = app.state.engine.run(analysis['id'])
    assert result['status'] == 'completed'
    assert result['result']['turns'][0]['speaker'] == speaker
    assert result['result']['turns'][0]['text'] == '系统来电播报。'
    # Confirmation still requires actual customer speech and nonempty, valid lines.
    for text in ('其他：播报。\n销售：您好。', '其他：\n客户：想试用。', '其他：播报。\n说话人B：想试用。'):
        assert alice.post(endpoint, json={'expected_revision': saved['revision'], 'transcript': text}).status_code == 422


def test_other_is_not_sent_to_extractor_and_does_not_trigger_customer_refusal(system, monkeypatch):
    store, engine, _ = system
    text = '其他：不要再联系，预算十万已经批准。\n销售：您好。\n客户：我只是了解一下。\n存疑：不要再打电话。'
    seen = []
    from sdr.extractors import rule_extract
    def extract(turns):
        seen.extend(turns)
        return rule_extract(turns)
    monkeypatch.setattr('sdr.engine.rule_extract', extract)
    request = AnalyzeRequest(transcript=text, mode='rules', data_mode='synthetic', idempotency_key='exclude-other-test')
    row, _ = store.create(request.model_dump(), 'exclude-other-test', 'A')
    row = engine.run(row['id'])
    assert [t.id for t in seen] == ['T02', 'T03']
    assert all(t.speaker not in ('other','excluded') for t in seen)
    assert row['result']['turns'][0]['text'] == '不要再联系，预算十万已经批准。'
    assert not row['result']['extraction']['do_not_contact']
    assert row['result']['extraction']['signals']['B']['kind'] == 'unknown'


@pytest.mark.parametrize('label',['其他','存疑'])
def test_other_quotes_cannot_be_used_even_with_customer_support(label):
    turns = split_turns(label+'：虚构公司。\n客户：想了解一下。')
    extraction = Extraction.model_validate({
        'call_type':'sales','classification_evidence':[{'turn_id':'T01','quote':'虚构公司。'}],
        'fields':{'raw_company':{'value':'虚构公司','evidence':[
            {'turn_id':'T01','quote':'虚构公司。'}, {'turn_id':'T02','quote':'想了解一下。'}]}}})
    output, issues = validate_extraction(extraction, turns)
    assert output['fields']['raw_company']['value'] is None
    assert output['call_type'] == 'unknown'
    assert sum(i['code'] == 'EXCLUDED_SPEAKER' for i in issues) == 2
