from pathlib import Path
import json
import stat

import httpx
import pytest

from sdr.audio import transcribe,AudioError
from sdr.model_settings import ModelSettingsService,ModelConfigError
from sdr.models import AnalyzeRequest,Extraction
from test_accounts import customer,transfer
from test_audio import upload,wav
from test_long_audio import recording


DEFAULT={'use_default':True}


def profile(kind='text',key='personal-test-secret',provider='qwen_cn',model=None):
    return {'use_default':False,'provider':provider,'model':model or ('qwen-plus' if kind=='text' else 'qwen3-asr-flash'),'api_key':key}


def save(client,text=None,audio=None,revision=None):
    if revision is None:revision=client.get('/api/model-settings').json()['revision']
    return client.put('/api/model-settings',json={'expected_revision':revision,'text':text or DEFAULT,'audio':audio or DEFAULT})


def test_personal_settings_encrypted_isolated_and_restored_after_restart(team):
    app,admin,alice,bob=team
    result=save(alice,profile(key='alice-text-secret'),profile('audio',key='alice-audio-secret'))
    assert result.status_code==200,result.text
    assert result.json()['profiles']['text']['configured']
    assert 'alice-text-secret' not in result.text and 'encrypted_key' not in result.text
    assert alice.get('/api/config').json()['llm_available']
    for client in (admin,bob):
        assert client.get('/api/model-settings').json()['profiles']['text']['use_default']
        assert not client.get('/api/config').json()['llm_available']
    assert alice.get('/api/model-settings?user_id=bob').json()['revision']==1
    with app.state.store.connection() as db:
        raw=db.execute('SELECT text_config,audio_config FROM model_preferences').fetchone()
        assert 'alice-text-secret' not in str(tuple(raw)) and 'alice-audio-secret' not in str(tuple(raw))
        audit=str([tuple(r) for r in db.execute("SELECT * FROM workspace_events WHERE action='personal_models_updated'")])
        assert 'alice-text-secret' not in audit and 'encrypted_key' not in audit
    service=ModelSettingsService(app.state.store,app.state.settings)
    assert service.resolve('alice').api_key=='alice-text-secret'
    assert service.resolve('alice').asr_key=='alice-audio-secret'
    assert stat.S_IMODE(service.key_path.stat().st_mode)==0o600


def test_blank_key_retained_only_for_same_account_and_destination(team):
    app,admin,alice,bob=team
    save(alice,profile())
    assert save(alice,profile(key=None,model='qwen-flash')).status_code==200
    assert app.state.model_settings.resolve('alice').api_key=='personal-test-secret'
    assert save(bob,profile(key=None)).status_code==422
    for provider,model in [('openai','gpt-4.1-mini'),('qwen_intl','qwen-plus')]:
        assert save(alice,profile(key=None,provider=provider,model=model)).status_code==422
    assert app.state.model_settings.resolve('alice').model=='qwen-flash'


def test_explicit_defaults_and_key_clear_do_not_silently_fallback(team):
    app,admin,alice,bob=team
    app.state.settings.api_key='workspace-key'
    assert save(alice,profile()).status_code==200
    assert save(alice,{**profile(key=None),'clear_key':True}).status_code==200
    assert not alice.get('/api/config').json()['llm_available']
    assert app.state.model_settings.resolve('alice').api_key==''
    assert save(alice).status_code==200
    assert app.state.model_settings.resolve('alice').api_key=='workspace-key'
    with app.state.store.connection() as db:
        assert json.loads(db.execute('SELECT text_config FROM model_preferences WHERE user_id=?',('alice',)).fetchone()[0])==DEFAULT


def test_revision_conflict_and_invalid_audio_do_not_partially_save(team):
    app,admin,alice,bob=team
    assert save(alice,profile(),revision=0).status_code==200
    assert save(alice,profile(key='replacement'),revision=0).status_code==409
    result=save(alice,profile(key='replacement'),profile('audio',provider='deepseek',model='deepseek-flash'))
    assert result.status_code==422
    assert app.state.model_settings.resolve('alice').api_key=='personal-test-secret'


@pytest.mark.parametrize('kind,provider,model',[
    ('audio','qwen_cn','qwen-plus'),('audio','openai','gpt-4.1-mini'),
    ('audio','openai','gpt-4o-mini-transcribe-diarize'),('text','qwen_cn','qwen3-asr-flash'),
])
def test_incompatible_models_rejected_locally(team,kind,provider,model):
    app,admin,alice,bob=team
    kwargs={kind:profile(kind,provider=provider,model=model)}
    assert save(alice,**kwargs).status_code==422
    assert alice.get('/api/model-settings').json()['revision']==0


def test_model_api_requires_auth_csrf_and_never_echoes_bad_key(team):
    app,admin,alice,bob=team
    response=alice.put('/api/model-settings',json={'expected_revision':0,'text':profile(key='secret\ninvalid'),'audio':DEFAULT})
    assert response.status_code==422 and 'secret' not in response.text
    assert alice.put('/api/model-settings',headers={'x-csrf-token':'wrong'},json={'expected_revision':0,'text':DEFAULT,'audio':DEFAULT}).status_code==403
    alice.post('/api/auth/logout')
    assert alice.get('/api/model-settings').status_code==401


def test_missing_encryption_key_fails_closed(team):
    app,admin,alice,bob=team
    save(alice,profile())
    service=app.state.model_settings
    service.key_path.rename(service.key_path.with_suffix('.saved'))
    with pytest.raises(ModelConfigError,match='密钥文件缺失'):service.resolve('alice')
    assert not service.key_path.exists()


def test_audio_and_identity_keep_one_snapshot_when_preferences_change(team,monkeypatch):
    app,admin,alice,bob=team;c=customer(alice)
    app.state.settings.asr_segment_seconds=3
    assert save(alice,profile(key='old-text'),profile('audio',key='old-audio')).status_code==200
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *a:None)
    seen=[];identity=[]
    def asr(data,mime,settings):
        seen.append((settings.asr_key,settings.asr_model))
        if len(seen)==1:assert save(alice,profile(key='new-text'),profile('audio',key='new-audio')).status_code==200
        return '客户文字',{'model':settings.asr_model}
    monkeypatch.setattr('sdr.audio.transcribe',asr)
    monkeypatch.setattr('sdr.audio.identify_customer',lambda text,s:identity.append(s.api_key) or {'confidence':'unknown','reason':'合成测试'})
    r=alice.post('/api/transcriptions',params={'customer_mode':'ai','filename':'long.wav','idempotency_key':'personal-snapshot','data_mode':'synthetic'},content=recording(7))
    assert r.status_code==202,r.text
    app.state.audio.run(r.json()['id'])
    assert seen==[('old-audio','qwen3-asr-flash')]*3 and identity==['old-text']
    assert app.state.model_settings.resolve('alice').api_key=='new-text'


def test_transferred_audio_retry_uses_new_owners_key_and_keeps_completed_segments(team,monkeypatch):
    app,admin,alice,bob=team;c=customer(alice)
    app.state.settings.asr_segment_seconds=3
    save(alice,audio=profile('audio',key='alice-key'));save(bob,audio=profile('audio',key='bob-key'))
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *a:None)
    calls=[]
    def asr(data,mime,s):
        calls.append(s.asr_key)
        if len(calls)==2:raise AudioError('合成中断')
        return '片段文字',{'model':s.asr_model}
    monkeypatch.setattr('sdr.audio.transcribe',asr)
    tid=upload(alice,c['id'],data=recording(7)).json()['id'];app.state.audio.run(tid)
    assert transfer(admin,c,'bob').status_code==200
    assert alice.post('/api/transcriptions/'+tid+'/retry').status_code==404
    assert bob.post('/api/transcriptions/'+tid+'/retry').status_code==200
    app.state.audio.run(tid)
    assert calls==['alice-key','alice-key','bob-key','bob-key']
    row=bob.get('/api/transcriptions/'+tid).json()
    assert row['status']=='transcribed' and row['provider']['configured_by']=='bob'


def test_short_recording_retry_rejects_a_different_provider_plan(team,monkeypatch):
    app,admin,alice,bob=team;c=customer(alice)
    save(alice,audio=profile('audio',provider='openai',model='gpt-4o-mini-transcribe'))
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *a:None)
    calls=[]
    def asr(data,mime,s):
        calls.append(s.asr_protocol)
        raise AudioError('合成中断')
    monkeypatch.setattr('sdr.audio.transcribe',asr)
    tid=upload(alice,c['id'],data=recording(1)).json()['id']
    app.state.audio.run(tid)
    assert calls==['openai_transcription']
    assert save(alice,audio=profile('audio')).status_code==200
    assert alice.post('/api/transcriptions/'+tid+'/retry').status_code==200
    app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='failed' and '音频处理配置已变化' in row['error']
    assert calls==['openai_transcription']


def test_analysis_uses_requesting_employee_configuration(team,monkeypatch,samples):
    app,admin,alice,bob=team
    save(alice,profile(key='alice-analysis'));save(bob,profile(key='bob-analysis'))
    monkeypatch.setattr(app.state.engine.pool,'submit',lambda *a:None)
    seen=[]
    monkeypatch.setattr('sdr.engine.llm_extract',lambda turns,s:seen.append(s.api_key) or (Extraction(),{'model':s.model,'tokens':0}))
    for client in (alice,bob):
        r=client.post('/api/analyses',json={'transcript':samples['verified']['transcript'],'mode':'llm','data_mode':'synthetic','idempotency_key':'personal-analysis'})
        assert r.status_code==202,r.text
        app.state.engine.run(r.json()['id'])
    assert seen==['alice-analysis','bob-analysis']


def test_openai_transcription_multipart_contract_and_sanitized_error(settings,monkeypatch):
    settings.asr_protocol='openai_transcription';settings.asr_model='gpt-4o-mini-transcribe'
    settings.asr_base_url='https://api.openai.com/v1';settings.asr_key='own-key'
    real=httpx.Client
    def handler(request):
        assert request.url.path=='/v1/audio/transcriptions'
        assert request.headers['Authorization']=='Bearer own-key'
        assert request.headers['Content-Type'].startswith('multipart/form-data;')
        assert b'gpt-4o-mini-transcribe' in request.content and wav() in request.content
        assert b'asr_options' not in request.content
        return httpx.Response(200,json={'text':'合成连接测试','usage':{'total_tokens':5}})
    monkeypatch.setattr('sdr.audio.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(handler),**kw))
    assert transcribe(wav(),'audio/wav',settings)[0]=='合成连接测试'
    monkeypatch.setattr('sdr.audio.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(lambda r:httpx.Response(401,text='private provider data own-key')),**kw))
    with pytest.raises(AudioError) as exc:transcribe(wav(),'audio/wav',settings)
    assert '401' in str(exc.value) and 'private' not in str(exc.value) and 'own-key' not in str(exc.value)


def test_draft_connection_test_does_not_save_and_sends_only_fixed_fixture(team,monkeypatch):
    app,admin,alice,bob=team;seen=[]
    def check(s):seen.append((s.model,s.api_key));return {'tokens':1}
    monkeypatch.setattr('sdr.model_transport.check_text_connection',check)
    body={'expected_revision':0,'kind':'text','profile':profile(key='draft-key')}
    r=alice.post('/api/model-settings/test',json=body)
    assert r.status_code==200 and r.json()['saved'] is False
    assert seen==[('qwen-plus','draft-key')]
    assert alice.get('/api/model-settings').json()['revision']==0
    assert alice.post('/api/model-settings/test',json=body).status_code==409
    app.state.model_settings.last_test.clear()
    from sdr.config import ROOT
    def audio(data,mime,s):
        assert data==(ROOT/'data/model-probe.wav').read_bytes() and len(data)>1000
        assert s.asr_key=='audio-draft-key'
        return '这是语音连接测试',{'tokens':1}
    monkeypatch.setattr('sdr.audio.transcribe',audio)
    r=alice.post('/api/model-settings/test',json={'expected_revision':0,'kind':'audio','profile':profile('audio',key='audio-draft-key')})
    assert r.status_code==200 and alice.get('/api/model-settings').json()['revision']==0


def test_text_connection_contract_and_invalid_json(settings,monkeypatch):
    from sdr.model_transport import check_text_connection,ModelTransportError
    settings.api_key='text-key';settings.model='deepseek-flash'
    real=httpx.Client
    def handler(request):
        body=json.loads(request.content)
        assert body['thinking']=={'type':'disabled'}
        assert body['response_format']=={'type':'json_object'}
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"status":"ok"}'}}]})
    monkeypatch.setattr('sdr.model_transport.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(handler),**kw))
    assert check_text_connection(settings)=={'tokens':0}
    monkeypatch.setattr('sdr.model_transport.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[{'message':{'content':'key secret response'}}]})),**kw))
    with pytest.raises(ModelTransportError,match='JSON'):check_text_connection(settings)


def test_provider_billing_error_is_actionable_without_exposing_upstream_content():
    from sdr.model_transport import provider_error
    r=httpx.Response(400,json={'error':{'code':'Arrearage','message':'sensitive billing account information'}})
    message=provider_error(r,'语音服务')
    assert '欠费' in message and 'API Key' in message and 'sensitive' not in message
    r=httpx.Response(429,json={'error':{'code':'insufficient_quota','message':'secret'}})
    assert '额度不可用' in provider_error(r,'文字模型')
