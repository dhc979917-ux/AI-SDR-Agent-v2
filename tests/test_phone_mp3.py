"""Untagged MPEG-2.5 telephone recordings must pass real frame parsing."""
import io
import math
import subprocess
import wave

import mutagen
import pytest
from imageio_ffmpeg import get_ffmpeg_exe

from sdr.audio import inspect_audio
from sdr.audio_processing import RATE
from test_accounts import customer
from test_audio import upload


def phone_mp3(rate=8000,seconds=2):
    result=subprocess.run([
        get_ffmpeg_exe(),'-nostdin','-v','error','-f','lavfi',
        '-i',f'sine=frequency=440:sample_rate={rate}:duration={seconds}',
        '-ac','1','-c:a','libmp3lame','-b:a','16k','-write_xing','0',
        '-id3v2_version','0','-map_metadata','-1','-f','mp3','pipe:1',
    ],capture_output=True,check=True,timeout=20)
    return result.stdout


@pytest.mark.parametrize('rate',[8000,11025,12000])
def test_untagged_mpeg25_is_recognized_from_stream(settings,rate):
    data=phone_mp3(rate)
    assert not data.startswith(b'ID3')
    assert mutagen.File(io.BytesIO(data)) is None  # Reproduce the old detector's miss.
    mime,duration=inspect_audio(io.BytesIO(data),'电话.mp3',settings)
    assert mime=='audio/mpeg' and 2<=duration<=2.2


def test_phone_mp3_upload_normalizes_segments_and_preserves_original(team,monkeypatch):
    app,admin,alice,bob=team;c=customer(alice)
    app.state.settings.asr_key='synthetic-test-key'
    app.state.settings.asr_segment_seconds=3
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *args:None)
    frames=[]
    def transcribe(data,mime,settings):
        assert mime=='audio/wav'
        with wave.open(io.BytesIO(data)) as audio:
            assert (audio.getframerate(),audio.getnchannels(),audio.getsampwidth())==(RATE,1,2)
            assert 0<audio.getnframes()<=3*RATE
            frames.append(audio.getnframes())
        return f'合成片段{len(frames)}',{'model':'mock-asr','tokens':1}
    monkeypatch.setattr('sdr.audio.transcribe',transcribe)
    data=phone_mp3(seconds=7)
    response=upload(alice,c['id'],data=data,name='电话.mp3')
    assert response.status_code==202,response.text
    tid=response.json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='transcribed'
    assert len(frames)==math.ceil(row['duration']/3)
    assert sum(frames)/RATE==row['duration']
    assert row['raw_text']=='\n'.join(f'合成片段{i+1}' for i in range(len(frames)))
    assert alice.get('/api/transcriptions/'+tid+'/audio').content==data
    assert bob.get('/api/transcriptions/'+tid).status_code==404
    assert not list(app.state.audio.directory.glob('.processing-*'))


@pytest.mark.parametrize('data',[b'not an audio recording',b'\xff\xe3\x28\xc4'+b'\0'*500])
def test_mp3_extension_and_fake_header_do_not_bypass_validation(team,monkeypatch,data):
    app,admin,alice,bob=team;c=customer(alice)
    app.state.settings.asr_key='synthetic-test-key'
    def unexpected(*args):
        pytest.fail('Invalid MP3 must not queue a cloud request')
    monkeypatch.setattr(app.state.audio.pool,'submit',unexpected)
    response=upload(alice,c['id'],data=data,name='电话.mp3')
    assert response.status_code==422
    assert '无法解析此MP3' in response.json()['detail']
    assert alice.get('/api/transcriptions').json()==[]
