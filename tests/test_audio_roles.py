import io
import json
import hashlib

import httpx
import pytest

from sdr.audio import AudioError,transcribe
from sdr.audio_roles import classify_roles,validate_turns,source_units,validate_unit_roles,reconcile_units,RoleError,compact
from sdr.models import AudioIdentityRequest
from test_accounts import customer,transfer
from test_audio import upload,wav
from test_model_settings import save,profile


def role(text,speaker='sales',certainty='clear'):
    return {'text':text,'role':speaker,'certainty':certainty,'note':''}


def response(turns):return json.dumps({'turns':turns},ensure_ascii=False)


def references(request,turns):
    units=json.loads(json.loads(request.content)['messages'][1]['content'])['target_units']
    result={};cursor=0
    for turn in turns:
        first=cursor+1;joined=''
        while cursor<len(units) and len(compact(joined))<len(compact(turn['text'])):
            joined+=units[cursor]['text'];cursor+=1
        assert compact(joined)==compact(turn['text'])
        result.update({str(i):turn['role'] for i in range(first,cursor+1)})
    return json.dumps({'roles':result},ensure_ascii=False)


def install_transport(monkeypatch,handler):
    real=httpx.Client
    monkeypatch.setattr('sdr.audio_roles.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(handler),**kw))


def test_roles_keep_exact_words_numbers_and_consecutive_speaker():
    text='您好，我是酷价乐的小王。 我再介绍一下。\n嗯，预算3.5万。'
    turns=validate_turns(text,response([role('您好，我是酷价乐的小王。'),role('我再介绍一下。'),role('嗯，预算3.5万。','customer')]))
    assert [t['role'] for t in turns]==['sales','sales','customer']
    assert ''.join(compact(t['text']) for t in turns)==compact(text)
    assert '酷价乐' in turns[0]['text'] and turns[2]['text']=='嗯，预算3.5万。'
    for t in turns:assert text[t['source_start']:t['source_end']].strip()==t['text']


@pytest.mark.parametrize('parts',[
    ['您好。'],['您好。','预算3万。'],['预算2万。','您好。'],['您好。','您好。','预算2万。'],
])
def test_missing_rewritten_reordered_or_duplicated_words_rejected(parts):
    with pytest.raises(ValueError):validate_turns('您好。预算2万。',response([role(p) for p in parts]))


def test_uncertain_roles_are_never_presented_as_certain_customer_facts():
    turns=validate_turns('嗯。',response([role('嗯。','customer','uncertain')]))
    assert turns[0]['role']=='other' and turns[0]['note']


def test_chunks_keep_context_and_do_not_duplicate_it(settings,monkeypatch):
    settings.api_key='fixed-key';seen=[];progress=[]
    monkeypatch.setattr('sdr.audio_roles.CHUNK_CHARS',10)
    # Explicitly control boundaries to exercise the context contract.
    monkeypatch.setattr('sdr.audio_roles.chunks',lambda t:[(0,6),(6,len(t))])
    def handler(request):
        body=json.loads(request.content);item=json.loads(body['messages'][1]['content']);seen.append(item)
        target=''.join(u['text'] for u in item['target_units'])
        return httpx.Response(200,json={'choices':[{'message':{'content':references(request,[role(target,'sales' if target.startswith('我是') else 'customer')])}}],'usage':{'total_tokens':10}})
    install_transport(monkeypatch,handler)
    text='我是销售啊。我们要试用。'
    result=classify_roles(text,settings,on_progress=lambda *p:progress.append(p))
    assert result['transcript']=='销售：我是销售啊。\n客户：我们要试用。'
    assert seen[0]['previous_context']=='' and '销售：我是销售啊。' in seen[2]['previous_context']
    assert result['source_sha256']==hashlib.sha256(text.encode()).hexdigest()
    assert progress==[(1,2),(2,2)] and result['tokens']==40


def test_invalid_model_output_gets_one_repair_then_safe_failure(settings,monkeypatch):
    settings.api_key='test-key';calls=[]
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':response([role('虚构的电话123')])}}]})
    install_transport(monkeypatch,handler)
    with pytest.raises(RoleError,match='完整性'):classify_roles('原话。',settings)
    assert len(calls)==2 and len(calls[1]['messages'])==3


def test_valid_repair_and_safe_provider_failure(settings,monkeypatch):
    settings.api_key='test-key';calls=[]
    def handler(request):
        calls.append(1)
        content=references(request,[role('原话。')]) if len(calls)>1 else response([role('改写。')])
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}]})
    install_transport(monkeypatch,handler)
    assert classify_roles('原话。',settings)['transcript']=='销售：原话。'
    assert len(calls)==3
    # Replace only the transport factory, avoiding a nested monkeypatch factory.
    monkeypatch.undo()
    install_transport(monkeypatch,lambda request:httpx.Response(401,text='secret-key provider private details'))
    with pytest.raises(RoleError) as exc:classify_roles('原话。',settings)
    assert '401' in str(exc.value) and 'secret-key' not in str(exc.value)


def test_speaker_conflicts_mark_unknown_and_unmatched_hints_are_not_claimed(settings,monkeypatch):
    settings.api_key='test-key'
    hints=[{'speaker':'1:A','text':'您好。','start':0,'end':1},{'speaker':'1:A','text':'好的。','start':1,'end':2}]
    install_transport(monkeypatch,lambda request:httpx.Response(200,json={'choices':[{'message':{'content':references(request,[role('您好。'),role('好的。','customer')])}}]}))
    result=classify_roles('您好。好的。',settings,hints)
    assert result['method']=='speaker_assisted' and result['uncertain_count']==2
    assert all(t['role']=='unknown' for t in result['turns'])
    result=classify_roles('您好。好的。',settings,[{**hints[0],'text':'对不上原文'}])
    assert result['method']=='text_context' and all('speaker' not in t for t in result['turns'])


def test_one_turn_cannot_span_different_acoustic_speakers():
    hints=[{'speaker':'A','begin':0,'finish':3,'start':0,'end':1},
           {'speaker':'B','begin':3,'finish':6,'start':1,'end':2}]
    with pytest.raises(ValueError,match='不同声纹'):
        validate_turns('您好。好的。',response([role('您好。好的。')]),hints=hints)


def test_numbered_source_references_preserve_every_character_and_allow_inside_clause_roles():
    text='嗯，你好你好预算3.5万。\n我记下了。  '
    units=source_units(text)
    assert ''.join(u['text'] for u in units)==text
    content={'roles':{'1':'unknown','2':'unknown','3':'sales'},'splits':{
        '2':[role('你好','sales'),role('你好预算3.5万。','customer')]}}
    found=validate_unit_roles(text,json.dumps(content,ensure_ascii=False),units)
    assert ''.join(compact(t['text']) for t in found)==compact(text)
    assert found[2]['role']=='customer' and found[2]['text']=='你好预算3.5万。'


@pytest.mark.parametrize('labels',[
    {'2':'sales'},{'1':'sales'},{'1':'sales','2':'customer','3':'sales'},
    {'1':'sales','2':True},{'1':'sales','2':'other'},{'01':'sales','2':'sales'},
])
def test_reference_gaps_extra_ids_invalid_roles_and_noncanonical_ids_rejected(labels):
    text='您好。预算2万。';units=source_units(text)
    content={'roles':labels}
    with pytest.raises(ValueError):validate_unit_roles(text,json.dumps(content),units)


def test_model_review_disagreement_requires_human_confirmation(settings,monkeypatch):
    settings.api_key='test-key';calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        labels={'1':'sales','2':'customer' if len(calls)==1 else 'sales'}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'roles':labels})}}]})
    install_transport(monkeypatch,handler)
    result=classify_roles('你好。您之前用过其他软件吗？',settings)
    assert len(calls)==2 and calls[1]['messages'][-2]['role']=='assistant'
    assert result['turns'][1]['role']=='unknown' and '不一致' in result['turns'][1]['note']
    assert result['uncertain_count']==1


def test_review_changed_inside_clause_boundaries_never_silently_replaces_roles():
    units=source_units('你好吗好的。')
    a=json.dumps({'roles':{'1':'unknown'},'splits':{'1':[role('你好吗'),role('好的。','customer')]}})
    b=json.dumps({'roles':{'1':'sales'}})
    found=validate_unit_roles(units[0]['text'],reconcile_units(a,b,units),units)
    assert found[0]['role']=='unknown' and '不一致' in found[0]['note']


def test_standalone_punctuation_stays_with_original_clause():
    assert [u['text'] for u in source_units('您好。，，嗯。')]==['您好。，，','嗯。']


def test_acoustic_boundary_inside_clause_is_retained_by_source_units():
    hints=[{'speaker':'A','begin':0,'finish':2,'start':0,'end':1},
           {'speaker':'B','begin':2,'finish':5,'start':1,'end':2}]
    units=source_units('你好你好。',hints)
    assert [u['text'] for u in units]==['你好','你好。']


def test_openai_diarization_request_and_segments_contract(settings,monkeypatch):
    settings.asr_model='gpt-4o-transcribe-diarize';settings.asr_protocol='openai_transcription';settings.asr_key='key'
    def handler(request):
        assert b'diarized_json' in request.content and b'chunking_strategy' in request.content and b'auto' in request.content
        assert b'prompt' not in request.content and b'response_format' in request.content
        return httpx.Response(200,json={'text':'您好。好的。','segments':[
            {'speaker':'A','text':'您好。','start':0,'end':1},
            {'speaker':'B','text':'好的。','start':1,'end':2}]})
    install_transport(monkeypatch,handler)
    text,info=transcribe(wav(),'audio/wav',settings)
    assert text=='您好。\n好的。' and info['speaker_roles']=='acoustic_segments'
    assert [s['speaker'] for s in info['speaker_segments']]==['A','B']


@pytest.mark.parametrize('segment',[
    {'speaker':'A','text':'您好。','start':-1,'end':1},
    {'speaker':'A','text':'遗漏其他内容','start':0,'end':1},
])
def test_invalid_diarization_response_rejected(settings,monkeypatch,segment):
    settings.asr_model='gpt-4o-transcribe-diarize';settings.asr_protocol='openai_transcription'
    install_transport(monkeypatch,lambda request:httpx.Response(200,json={'text':'您好。','segments':[segment]}))
    with pytest.raises(AudioError,match='响应格式'):transcribe(wav(),'audio/wav',settings)


def transcribed(team,monkeypatch,ai=False):
    app,admin,alice,bob=team;c=customer(alice)
    save(alice,profile(key='alice-text'),profile('audio',key='alice-audio'))
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *a:None)
    monkeypatch.setattr('sdr.audio.transcribe',lambda *a:('您好。我想试用。',{'model':'fixture'}))
    if ai:
        r=alice.post('/api/transcriptions',params={'customer_mode':'ai','filename':'call.wav','idempotency_key':'roles-ai-test','data_mode':'synthetic'},content=wav())
    else:r=upload(alice,c['id'])
    assert r.status_code==202,r.text
    return app,admin,alice,bob,c,r.json()['id']


def test_new_audio_auto_generates_draft_without_confirming_and_failure_keeps_raw(team,monkeypatch):
    app,admin,alice,bob,c,tid=transcribed(team,monkeypatch,ai=True);seen=[]
    def roles(text,s,*args):
        seen.append(s.api_key)
        raise RoleError('角色服务暂时超时')
    monkeypatch.setattr('sdr.audio.classify_roles',roles)
    monkeypatch.setattr('sdr.audio.identify_customer',lambda *a:{'confidence':'unknown','reason':'合成样例'})
    app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='transcribed' and row['raw_text']=='您好。我想试用。'
    assert row['role_status']=='failed' and row['confirmed_text'] is None
    assert row['identity_status']=='ready' and seen==['alice-text']


def test_roles_access_revision_transfer_and_confirmed_draft_preservation(team,monkeypatch):
    app,admin,alice,bob,c,tid=transcribed(team,monkeypatch)
    app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json();draft='销售：您好。\n客户：我想试用。'
    confirmed=alice.post('/api/transcriptions/'+tid+'/confirm',json={'expected_revision':row['revision'],'transcript':draft})
    assert confirmed.status_code==200
    revision=confirmed.json()['revision']
    assert alice.post('/api/transcriptions/'+tid+'/roles',headers={'x-csrf-token':'wrong'},json={'expected_revision':revision}).status_code==403
    assert alice.post('/api/transcriptions/'+tid+'/roles',json={'expected_revision':revision-1}).status_code==409
    save(bob,profile(key='bob-text'));assert transfer(admin,c,'bob').status_code==200
    assert alice.post('/api/transcriptions/'+tid+'/roles',json={'expected_revision':revision}).status_code==404
    assert bob.post('/api/transcriptions/'+tid+'/roles',json={'expected_revision':revision}).status_code==200
    assert bob.post('/api/transcriptions/'+tid+'/roles',json={'expected_revision':revision}).status_code==409
    assert bob.delete('/api/transcriptions/'+tid).status_code==409
    seen=[]
    def roles(text,s,*args):
        seen.append(s.api_key)
        return {'transcript':'待确认：您好。\n客户：我想试用。','turns':[],'uncertain_count':1,'model':s.model}
    monkeypatch.setattr('sdr.audio.classify_roles',roles);app.state.audio.run_roles(tid)
    row=bob.get('/api/transcriptions/'+tid).json()
    assert row['role_status']=='ready' and row['confirmed_text']==draft and row['status']=='confirmed'
    assert seen==['bob-text'] and row['role_result']['configured_by']=='bob'
    r=bob.post('/api/transcriptions/'+tid+'/confirm',json={'expected_revision':row['revision'],'transcript':row['role_result']['transcript']})
    assert r.status_code==422 and '待确认' in r.json()['detail']


def test_restart_invalidates_inflight_role_completion(team,monkeypatch):
    app,admin,alice,bob,c,tid=transcribed(team,monkeypatch);app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    app.state.audio.roles(tid,AudioIdentityRequest(expected_revision=row['revision']),'alice')
    def roles(*args):
        app.state.audio.recover()
        return {'transcript':'销售：旧任务不能发布','turns':[]}
    monkeypatch.setattr('sdr.audio.classify_roles',roles);app.state.audio.run_roles(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['role_status']=='interrupted' and '旧任务不能发布' not in json.dumps(row['role_result'],ensure_ascii=False)


def test_missing_text_key_skips_roles_and_diarize_can_be_selected(team,monkeypatch):
    app,admin,alice,bob=team;c=customer(alice)
    assert save(alice,audio=profile('audio',provider='openai',model='gpt-4o-transcribe-diarize')).status_code==200
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *a:None)
    monkeypatch.setattr('sdr.audio.transcribe',lambda *a:('原始转写',{'model':'fixture'}))
    # Avoid digital silence, which is intentionally not sent to a provider.
    from test_long_audio import recording
    tid=upload(alice,c['id'],data=recording(1)).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='transcribed' and row['role_status']=='skipped'
    assert alice.post('/api/transcriptions/'+tid+'/roles',json={'expected_revision':row['revision']}).status_code==422
