import io
import subprocess
import wave

import pytest
from imageio_ffmpeg import get_ffmpeg_exe

from sdr.audio import inspect_audio, AudioError
from sdr.audio_processing import normalized_audio, segment_ranges, RATE
from sdr.models import AnalyzeRequest
from test_accounts import customer
from test_audio import upload


def recording(seconds, rate=RATE, value=1000):
    f=io.BytesIO()
    with wave.open(f,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate)
        w.writeframes(value.to_bytes(2,'little',signed=True)*int(seconds*rate))
    return f.getvalue()


def setup(team,monkeypatch,segment_seconds=3):
    app,admin,alice,bob=team
    app.state.settings.asr_key='fixture-key'
    app.state.settings.asr_segment_seconds=segment_seconds
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *args:None)
    return app,alice,bob,customer(alice)


def test_over_five_minutes_and_ten_mb_upload_and_ordered_merge(team,monkeypatch):
    app,alice,bob,c=setup(team,monkeypatch,60)
    seen=[]
    def asr(data,mime,settings):
        with wave.open(io.BytesIO(data)) as w:
            assert w.getframerate()==RATE and w.getnchannels()==1 and w.getsampwidth()==2
            assert 0<w.getnframes()/RATE<=60
            seen.append(w.getnframes())
        assert mime=='audio/wav' and len(data)<10_000_000
        return f'第{len(seen)}段文字',{'model':'fixture','tokens':10}
    monkeypatch.setattr('sdr.audio.transcribe',asr)
    data=recording(361)
    assert len(data)>10*1024*1024
    response=upload(alice,c['id'],data=data);assert response.status_code==202,response.text
    tid=response.json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='transcribed' and row['duration']==361
    assert sum(seen)==361*RATE and len(seen)==7
    assert row['raw_text']=='\n'.join(f'第{i}段文字' for i in range(1,8))
    assert row['provider']['completed_segments']==row['provider']['segment_count']==7
    assert row['provider']['tokens']==70 and len(row['segments'])==7
    assert row['segments'][0]['start']==0 and row['segments'][-1]['end']==361
    assert all(a['end']==b['start'] for a,b in zip(row['segments'],row['segments'][1:]))
    assert bob.get('/api/transcriptions/'+tid).status_code==404
    assert alice.get('/api/transcriptions/'+tid+'/audio').content==data
    assert not list(app.state.audio.directory.glob('.processing-*'))
    assert alice.delete('/api/transcriptions/'+tid).status_code==200
    with app.state.store.connection() as db:
        assert db.execute('SELECT count(*) FROM audio_segments').fetchone()[0]==0


def test_failed_segment_retry_reuses_success_and_identifies_only_complete_text(team,monkeypatch):
    app,alice,bob,c=setup(team,monkeypatch)
    app.state.settings.api_key='fixture-llm'
    calls=[];identified=[]
    def asr(*args):
        calls.append(True)
        if len(calls)==2:raise AudioError('暂时超时')
        return f'文字{len(calls)}',{'model':'fixture','tokens':10}
    monkeypatch.setattr('sdr.audio.transcribe',asr)
    monkeypatch.setattr('sdr.audio.identify_customer',lambda text,*args:identified.append(text) or {'confidence':'unknown','reason':'fixture'})
    response=alice.post('/api/transcriptions',params={'customer_mode':'ai','filename':'电话.wav','idempotency_key':'long-audio-ai','data_mode':'synthetic'},content=recording(7))
    assert response.status_code==202
    tid=response.json()['id'];app.state.audio.run(tid)
    failed=alice.get('/api/transcriptions/'+tid).json()
    assert failed['status']=='failed' and '第2/3段' in failed['error']
    assert failed['raw_text']=='' and identified==[]
    assert failed['provider']['completed_segments']==1
    assert [s['status'] for s in failed['segments']]==['completed','pending','pending']
    assert alice.post(f'/api/transcriptions/{tid}/retry').status_code==200
    app.state.audio.recover()
    assert alice.get('/api/transcriptions/'+tid).json()['status']=='interrupted'
    assert alice.post(f'/api/transcriptions/{tid}/retry').status_code==200
    app.state.audio.run(tid)
    done=alice.get('/api/transcriptions/'+tid).json()
    assert done['status']=='transcribed' and done['identity_status']=='ready'
    assert len(calls)==4  # first segment was not sent again
    assert done['raw_text']=='文字1\n文字3\n文字4'
    assert identified==[done['raw_text']]


def test_silence_aware_splits_are_contiguous_and_lossless():
    content=bytearray(recording(122))
    content[44+58*RATE*2:44+59*RATE*2]=b'\0'*(RATE*2)
    with wave.open(io.BytesIO(content)) as w:
        ranges=segment_ranges(w,60)
        assert 58*RATE<ranges[0][1]<60*RATE
        assembled=[]
        for start,end in ranges:
            w.setpos(start);assembled.append(w.readframes(end-start))
        assert b''.join(assembled)==bytes(content[44:])


def test_merge_keeps_repeated_speech_and_long_text_can_be_confirmed(team,monkeypatch):
    app,alice,bob,c=setup(team,monkeypatch)
    text='客户：'+'这里是重复的真实发言。'*1400
    monkeypatch.setattr('sdr.audio.transcribe',lambda *args:(text,{'model':'fixture','tokens':1}))
    tid=upload(alice,c['id'],data=recording(5)).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['raw_text']==text+'\n'+text and len(row['raw_text'])>24000
    response=alice.post(f'/api/transcriptions/{tid}/confirm',json={'expected_revision':row['revision'],'transcript':row['raw_text']})
    assert response.status_code==200,response.text
    assert response.json()['confirmed_text']==row['raw_text']
    request=AnalyzeRequest(transcript=row['raw_text'],transcription_id=tid,customer_id=c['id'],data_mode='synthetic',idempotency_key='long-text-snapshot')
    saved,_=app.state.store.create(request.model_dump(),'long-text-fingerprint','alice',authorize=True)
    assert saved['request']['transcript']==row['raw_text']


@pytest.mark.parametrize('seconds,allowed',[(3600,True),(3601,False)])
def test_upload_hour_limit(settings,seconds,allowed):
    # Low sample rate fixture keeps the metadata boundary test small.
    data=recording(seconds,rate=10)
    if allowed:
        assert inspect_audio(data,'call.wav',settings)[1]==3600
    else:
        with pytest.raises(AudioError,match='60分钟'):inspect_audio(data,'call.wav',settings)


def test_decoded_length_is_checked_before_cloud_use(settings,tmp_path):
    source=tmp_path/'long.wav';source.write_bytes(recording(6));settings.audio_max_seconds=5
    with pytest.raises(AudioError,match='实际解码时长'):
        with normalized_audio(source,tmp_path,settings):pytest.fail('must not yield truncated audio')
    assert not list(tmp_path.glob('.processing-*'))


@pytest.mark.parametrize('suffix',['m4a','mp3'])
def test_compressed_audio_decodes_to_bounded_pcm(settings,tmp_path,suffix):
    source=tmp_path/'input.wav';source.write_bytes(recording(2,rate=48000))
    encoded=tmp_path/('input.'+suffix)
    subprocess.run([get_ffmpeg_exe(),'-nostdin','-v','error','-i',str(source),str(encoded)],check=True,capture_output=True,timeout=20)
    with normalized_audio(encoded,tmp_path,settings) as path:
        with wave.open(str(path)) as w:
            assert w.getframerate()==RATE and w.getnchannels()==1 and w.getsampwidth()==2
            assert 1.9<=w.getnframes()/RATE<=2.1
    assert not path.exists()


def test_large_short_recording_is_reencoded_under_provider_size_limit(team,monkeypatch):
    app,alice,bob,c=setup(team,monkeypatch,180)
    calls=[]
    monkeypatch.setattr('sdr.audio.transcribe',lambda data,mime,*args:calls.append((len(data),mime)) or ('短录音文字',{'tokens':1}))
    data=recording(20,rate=192000)
    assert len(data)>7_000_000
    tid=upload(alice,c['id'],data=data).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='transcribed' and row['provider']['segment_count']==1
    assert calls[0][0]<1_000_000 and calls[0][1]=='audio/wav'


def test_digital_silence_does_not_generate_fiction_or_cloud_calls(team,monkeypatch):
    app,alice,bob,c=setup(team,monkeypatch)
    monkeypatch.setattr('sdr.audio.transcribe',lambda *args:pytest.fail('digital silence should not call ASR'))
    tid=upload(alice,c['id'],data=recording(5,value=0)).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='failed' and '未识别出有效文字' in row['error']
    assert row['raw_text']=='' and all(s['text']=='' for s in row['segments'])


def test_retry_refuses_incompatible_plan_without_discarding_cached_results(team,monkeypatch):
    app,alice,bob,c=setup(team,monkeypatch);calls=[]
    def asr(*args):
        calls.append(True)
        if len(calls)==2:raise AudioError('fixture failure')
        return '已保存文字',{'tokens':1}
    monkeypatch.setattr('sdr.audio.transcribe',asr)
    tid=upload(alice,c['id'],data=recording(7)).json()['id'];app.state.audio.run(tid)
    app.state.settings.asr_model='another-model'
    alice.post(f'/api/transcriptions/{tid}/retry');app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='failed' and '处理配置已变化' in row['error']
    assert len(calls)==2 and row['segments'][0]['text']=='已保存文字'


def test_decoder_timeout_and_failure_clean_temporary_files(settings,tmp_path,monkeypatch):
    source=tmp_path/'input.wav';source.write_bytes(recording(1))
    def timeout(*args,**kwargs):raise subprocess.TimeoutExpired('sensitive command',1)
    monkeypatch.setattr('sdr.audio_processing.subprocess.run',timeout)
    with pytest.raises(AudioError,match='解码超时'):
        with normalized_audio(source,tmp_path,settings):pass
    assert not list(tmp_path.glob('.processing-*'))


def test_transcript_overflow_keeps_completed_segments_without_partial_success(team,monkeypatch):
    app,alice,bob,c=setup(team,monkeypatch)
    monkeypatch.setattr('sdr.audio.transcribe',lambda *args:('字'*23000,{'tokens':1}))
    tid=upload(alice,c['id'],data=recording(16)).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='failed' and '100,000字符' in row['error'] and row['raw_text']==''
    assert len(row['segments'])==6 and all(s['status']=='completed' for s in row['segments'])
