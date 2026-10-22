import io
import wave
import pytest
import httpx
from sdr.audio import AudioError, inspect_audio, transcribe
from sdr.models import AnalyzeRequest
from conftest import TEST_PASSWORD
from test_accounts import customer, transfer


def wav(seconds=.2):
    f=io.BytesIO()
    with wave.open(f,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0\0'*int(16000*seconds))
    return f.getvalue()


def upload(client,cid,key='audio-upload-key',data=None,name='电话.wav'):
    return client.post('/api/transcriptions',params={'customer_id':cid,'filename':name,'idempotency_key':key,'data_mode':'synthetic'},
                       content=data if data is not None else wav(),headers={'Content-Type':'application/octet-stream'})


def test_recording_confirmation_analysis_and_transfer_keep_history(team,monkeypatch,samples):
    app,admin,alice,bob=team;app.state.settings.asr_key='test-asr-key'
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *a:None)
    original='您好，请问需要什么？我想试用。'
    monkeypatch.setattr('sdr.audio.transcribe',lambda *a:(original,{'model':'test-fixture','tokens':0}))
    c=customer(alice)
    response=upload(alice,c['id']);assert response.status_code==202,response.text
    tid=response.json()['id']
    assert upload(alice,c['id']).json()['id']==tid
    assert upload(alice,c['id'],data=wav(.4)).status_code==409
    assert bob.get('/api/transcriptions/'+tid).status_code==404
    assert bob.get('/api/transcriptions/'+tid+'/audio').status_code==404
    assert bob.get('/api/transcriptions').json()==[]
    assert alice.get('/api/transcriptions/'+tid+'/audio').content==wav()
    app.state.audio.run(tid)
    draft=alice.get('/api/transcriptions/'+tid).json()
    assert draft['status']=='transcribed' and draft['raw_text']==original
    unconfirmed={'transcript':samples['verified']['transcript'],'mode':'rules','data_mode':'synthetic','transcription_id':tid,'idempotency_key':'from-audio-001'}
    assert alice.post('/api/analyses',json=unconfirmed).status_code==409
    for text in ('这是没有角色的原始文本','销售：只是销售说话','客户：有效\n说话人B：未标注销售客户'):
        assert alice.post(f'/api/transcriptions/{tid}/confirm',json={'expected_revision':draft['revision'],'transcript':text}).status_code==422
    confirmed_text='销售：您好，请问需要什么？\n客户：我想试用。'
    response=alice.post(f'/api/transcriptions/{tid}/confirm',json={'expected_revision':draft['revision'],'transcript':confirmed_text})
    assert response.status_code==200,response.text
    confirmed=response.json()
    assert confirmed['raw_text']==original and confirmed['confirmed_text']==confirmed_text
    assert alice.post(f'/api/transcriptions/{tid}/confirm',json={'expected_revision':draft['revision'],'transcript':confirmed_text}).status_code==409
    assert alice.post('/api/analyses',json={**unconfirmed,'transcript':confirmed_text,'data_mode':'real'}).status_code==409
    req=AnalyzeRequest(**{**unconfirmed,'transcript':confirmed_text})
    row,_=app.state.store.create(req.model_dump(),'audio-analysis','alice',authorize=True)
    row=app.state.engine.run(row['id'])
    assert row['customer_id']==c['id'] and row['transcription_id']==tid
    assert row['request']['transcript']==confirmed_text
    assert row['request']['audio_source']['revision']==confirmed['revision']
    assert row['request']['audio_source']['sha256']==confirmed['sha256']
    edited=alice.post(f'/api/transcriptions/{tid}/confirm',json={'expected_revision':confirmed['revision'],
                        'transcript':confirmed_text.replace('我想试用。','我想试用一下。')})
    assert edited.status_code==200
    assert app.state.store.get(row['id'])['request']['transcript']==confirmed_text
    assert alice.delete('/api/transcriptions/'+tid).status_code==409
    assert transfer(admin,c,'bob').status_code==200
    assert alice.get('/api/transcriptions/'+tid+'/audio').status_code==404
    assert alice.post(f'/api/transcriptions/{tid}/confirm',json={'expected_revision':edited.json()['revision'],'transcript':confirmed_text}).status_code==404
    assert bob.get('/api/transcriptions/'+tid+'/audio').status_code==200
    assert bob.get('/api/analyses/'+row['id']).status_code==200


def test_upload_requires_valid_audio_scope_key_size_and_duration(team,monkeypatch):
    app,admin,alice,bob=team;c=customer(alice)
    assert upload(alice,c['id']).status_code==422
    app.state.settings.asr_key='test-asr-key'
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *a:None)
    assert upload(bob,c['id']).status_code==404
    assert upload(alice,c['id'],data=b'<html>not audio</html>').status_code==422
    assert upload(alice,c['id'],name='fake.mp3').status_code==422
    assert upload(alice,c['id'],name='file.exe').status_code==422
    app.state.settings.audio_max_seconds=.1
    assert upload(alice,c['id']).status_code==422
    app.state.settings.audio_max_seconds=300
    app.state.settings.audio_max_bytes=100
    assert upload(alice,c['id']).status_code==413
    assert list(app.state.audio.directory.iterdir())==[]


def test_failure_retry_restart_and_delete_are_explicit(team,monkeypatch):
    app,admin,alice,bob=team;c=customer(alice);app.state.settings.asr_key='test-asr-key'
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *a:None)
    def failing(*args):raise AudioError('转写超时，原录音已保留')
    monkeypatch.setattr('sdr.audio.transcribe',failing)
    tid=upload(alice,c['id']).json()['id'];app.state.audio.run(tid)
    assert alice.get('/api/transcriptions/'+tid).json()['status']=='failed'
    assert app.state.audio.path(tid).exists()
    assert alice.post('/api/transcriptions/'+tid+'/retry').status_code==200
    assert alice.post('/api/transcriptions/'+tid+'/retry').status_code==409
    app.state.audio.recover()
    assert alice.get('/api/transcriptions/'+tid).json()['status']=='interrupted'
    assert alice.delete('/api/transcriptions/'+tid).status_code==200
    assert not app.state.audio.path(tid).exists()


def test_asr_adapter_sends_audio_contract_and_handles_timeout(settings,monkeypatch):
    settings.asr_key='secret-asr-test'
    seen=[]
    def handler(request):
        import json,base64
        body=json.loads(request.content);seen.append(body)
        data=body['messages'][0]['content'][0]['input_audio']['data']
        assert base64.b64decode(data.split(',')[1])==wav()
        assert request.headers['Authorization']=='Bearer secret-asr-test'
        return httpx.Response(200,json={'choices':[{'message':{'content':'客户希望了解产品。'}}],'usage':{'total_tokens':12}})
    real_client=httpx.Client
    monkeypatch.setattr('sdr.audio.httpx.Client',lambda **kw:real_client(transport=httpx.MockTransport(handler),**kw))
    text,provider=transcribe(wav(),'audio/wav',settings)
    assert text=='客户希望了解产品。' and provider['tokens']==12
    assert seen[0]['model']=='qwen3-asr-flash' and seen[0]['asr_options']['enable_itn'] is True
    def timeout(request):raise httpx.ReadTimeout('sensitive upstream details')
    monkeypatch.setattr('sdr.audio.httpx.Client',lambda **kw:real_client(transport=httpx.MockTransport(timeout),**kw))
    with pytest.raises(AudioError,match='超时') as exc:transcribe(wav(),'audio/wav',settings)
    assert 'sensitive' not in str(exc.value)


@pytest.mark.parametrize('data,name,reason', [
    (b'', '电话.wav', '文件为空'),
    (wav(0), '电话.wav', '有效音频时长为0秒'),
    (wav(), '电话.mp3', '内容识别为WAV'),
    (b'PK\x03\x04archive', '电话.wav', 'ZIP压缩包'),
    (b'<!doctype html><html>download</html>', '电话.mp3', '网页而非录音'),
    (b'RF64\xff\xff\xff\xffWAVE', '电话.wav', 'RF64/RIFX'),
    (b'RIFF\x10\x00\x00\x00WAVEfmt ', '电话.wav', '无法解析此WAV'),
])
def test_upload_diagnostics_distinguish_file_problems(settings,data,name,reason):
    with pytest.raises(AudioError,match=reason):
        inspect_audio(data,name,settings)


def test_invalid_upload_returns_actionable_error_before_cloud_call(team,monkeypatch):
    app,admin,alice,bob=team;c=customer(alice);app.state.settings.asr_key='test-asr-key'
    def unexpected(*args):
        pytest.fail('invalid upload must not start a cloud transcription')
    monkeypatch.setattr(app.state.audio.pool,'submit',unexpected)
    response=upload(alice,c['id'],data=wav(0))
    assert response.status_code==422
    assert '有效音频时长为0秒' in response.json()['detail']
    assert alice.get('/api/transcriptions').json()==[]
