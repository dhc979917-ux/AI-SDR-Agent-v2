import io
import sys
import types
import wave
from pathlib import Path

import pytest

from sdr.audio import AudioError, transcribe
from sdr.audio_processing import segment_ranges, vad_speech_timestamps, RATE
from test_accounts import customer
from test_audio import upload, wav
from test_long_audio import recording


@pytest.fixture(autouse=True)
def fresh_funasr_cache(monkeypatch):
    monkeypatch.setattr('sdr.audio._FUNASR_MODELS', {})


def fake_funasr(monkeypatch, results):
    module = types.ModuleType('funasr')
    created = []
    class FakeAutoModel:
        def __init__(self, **kwargs):
            created.append(kwargs)
        def generate(self, input, **kwargs):
            return results(input, kwargs)
    module.AutoModel = FakeAutoModel
    post = types.ModuleType('funasr.utils.postprocess_utils')
    post.rich_transcription_postprocess = lambda text: text.replace('<|zh|>', '').replace('<|NEUTRAL|>', '')
    utils = types.ModuleType('funasr.utils')
    utils.postprocess_utils = post
    module.utils = utils
    monkeypatch.setitem(sys.modules, 'funasr', module)
    monkeypatch.setitem(sys.modules, 'funasr.utils', utils)
    monkeypatch.setitem(sys.modules, 'funasr.utils.postprocess_utils', post)
    return created


def local_settings(settings, tmp_path, model='paraformer-zh'):
    settings.asr_protocol = 'funasr_local'
    settings.asr_model = model
    settings.asr_key = ''
    settings.asr_base_url = 'local'
    settings.asr_hotwords_path = tmp_path / 'hot.txt'
    return settings


def test_speech_cut_uses_widest_silence_gap_and_stays_lossless():
    content = bytearray(recording(122))
    with wave.open(io.BytesIO(bytes(content))) as w:
        ranges = segment_ranges(w, 60, speech=[(0, 58), (58.5, 122)])
        assert len(ranges) == 3 and ranges[0][1] == int(58.25*RATE)
        assembled = []
        for start, end in ranges:
            w.setpos(start); assembled.append(w.readframes(end-start))
        assert b''.join(assembled) == bytes(content[44:])


def test_speech_cut_requires_minimum_gap():
    with wave.open(io.BytesIO(recording(122))) as w:
        assert segment_ranges(w, 60, speech=[(0, 60), (60.05, 122)])[0][1] == 60*RATE
        assert segment_ranges(w, 60, speech=[(0, 58), (58.3, 122)])[0][1] == int(58.15*RATE)


def test_speech_cut_trailing_silence_stays_inside_search_zone():
    with wave.open(io.BytesIO(recording(61))) as w:
        ranges = segment_ranges(w, 60, speech=[(0, 50)])
    assert len(ranges) == 2 and ranges[0][1] == 56*RATE and ranges[1] == (56*RATE, 61*RATE)


def test_vad_unavailable_keeps_energy_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, 'silero_vad', None)
    assert vad_speech_timestamps(Path('missing.wav')) is None


def test_vad_plan_is_recorded_and_cut_at_gap(team, monkeypatch):
    app, admin, alice, bob = team
    app.state.settings.asr_key = 'fixture-key'
    app.state.settings.asr_segment_seconds = 3
    monkeypatch.setattr(app.state.audio.pool, 'submit', lambda *a: None)
    monkeypatch.setattr('sdr.audio.vad_speech_timestamps', lambda path: [(0, 5.4), (5.6, 8)])
    monkeypatch.setattr('sdr.audio.transcribe', lambda *a: ('文字', {'model': 'fixture'}))
    tid = upload(alice, customer(alice)['id'], data=recording(8)).json()['id']
    app.state.audio.run(tid)
    row = alice.get('/api/transcriptions/' + tid).json()
    assert row['status'] == 'transcribed' and row['provider']['vad'] == 'silero'
    spans = [(s['start'], s['end']) for s in row['segments']]
    assert spans == [(0, 3), (3, 5.5), (5.5, 8)]


def test_funasr_paraformer_hotwords_and_speaker_hints(settings, monkeypatch, tmp_path):
    local_settings(settings, tmp_path)
    settings.asr_hotwords_path.write_text('# 注释\n木里木外\n青禾 40\n', encoding='utf-8')
    seen = {}
    def results(path, kwargs):
        seen.update(kwargs)
        return [{'text': '我们做木里木外', 'sentence_info': [
            {'start': 0, 'end': 1200, 'sentence': '我们做', 'spk': 0},
            {'start': 1200, 'end': 2400, 'sentence': '木里木外', 'spk': 1}]}]
    created = fake_funasr(monkeypatch, results)
    text, info = transcribe(wav(3), 'audio/wav', settings)
    assert text == '我们做木里木外'
    assert created[0]['spk_model'] == 'cam++' and created[0]['vad_model'] == 'fsmn-vad' and created[0]['punc_model'] == 'ct-punc'
    assert seen['hotword'] == '木里木外 20 青禾 40'
    assert info['protocol'] == 'funasr_local' and info['local'] is True and info['speaker_roles'] == 'acoustic_segments'
    assert info['speaker_segments'] == [
        {'speaker': '0', 'text': '我们做', 'start': 0.0, 'end': 1.2},
        {'speaker': '1', 'text': '木里木外', 'start': 1.2, 'end': 2.4}]


def test_funasr_sensevoice_strips_tags_and_skips_hotword(settings, monkeypatch, tmp_path):
    local_settings(settings, tmp_path, model='sensevoice-small')
    settings.asr_hotwords_path.write_text('木里木外\n', encoding='utf-8')
    def results(path, kwargs):
        assert 'hotword' not in kwargs
        return [{'text': '<|zh|><|NEUTRAL|>您好', 'sentence_info': [
            {'start': 0, 'end': 900, 'sentence': '<|zh|><|NEUTRAL|>您好', 'spk': 2}]}]
    created = fake_funasr(monkeypatch, results)
    text, info = transcribe(wav(1), 'audio/wav', settings)
    assert text == '您好' and 'punc_model' not in created[0]
    assert info['speaker_segments'] == [{'speaker': '2', 'text': '您好', 'start': 0.0, 'end': 0.9}]


def test_funasr_invalid_hints_and_overlaps_are_dropped(settings, monkeypatch, tmp_path):
    local_settings(settings, tmp_path)
    def results(path, kwargs):
        return [{'text': '有效文字', 'sentence_info': [
            {'start': 100, 'end': 50, 'sentence': '时间倒置', 'spk': 0},
            {'start': 0, 'end': 900, 'sentence': '   ', 'spk': 1},
            {'start': 1500, 'end': 2000, 'sentence': '超出时长', 'spk': 1},
            {'start': 0, 'end': 500, 'sentence': '缺少spk'},
            {'start': 0, 'end': 1500, 'sentence': '裁剪到时长', 'spk': 2}]}]
    fake_funasr(monkeypatch, results)
    text, info = transcribe(wav(1), 'audio/wav', settings)
    assert text == '有效文字'
    assert info['speaker_segments'] == [{'speaker': '2', 'text': '裁剪到时长', 'start': 0.0, 'end': 1.0}]
    assert info['speaker_roles'] == 'acoustic_segments'


def test_funasr_missing_dependency_is_actionable(settings, monkeypatch, tmp_path):
    local_settings(settings, tmp_path)
    monkeypatch.setitem(sys.modules, 'funasr', None)
    with pytest.raises(AudioError, match='pip install'):
        transcribe(wav(), 'audio/wav', settings)


def test_funasr_invalid_or_empty_output_fails_closed(settings, monkeypatch, tmp_path):
    from sdr import audio
    local_settings(settings, tmp_path)
    fake_funasr(monkeypatch, lambda p, k: [{'text': ''}])
    with pytest.raises(AudioError, match='未返回有效文字'):
        transcribe(wav(), 'audio/wav', settings)
    audio._FUNASR_MODELS.clear()
    fake_funasr(monkeypatch, lambda p, k: ['not-a-dict'])
    with pytest.raises(AudioError, match='本地离线转写失败'):
        transcribe(wav(), 'audio/wav', settings)


def test_funasr_model_loaded_once_and_reused(settings, monkeypatch, tmp_path):
    local_settings(settings, tmp_path)
    fake_funasr(monkeypatch, lambda p, k: [{'text': '文字'}])
    for _ in range(2):
        text, info = transcribe(wav(), 'audio/wav', settings)
        assert text == '文字' and info['speaker_roles'] == 'text_context_required'
    import sdr.audio as audio
    assert len(audio._FUNASR_MODELS) == 1 and 'speaker_segments' not in info


def funasr_profile(model='paraformer-zh', api_key=None):
    return {'use_default': False, 'provider': 'funasr_local', 'model': model, 'api_key': api_key}


def test_local_funasr_provider_needs_no_key(team):
    app, admin, alice, bob = team
    body = {'expected_revision': 0, 'text': {'use_default': True}, 'audio': funasr_profile()}
    result = alice.put('/api/model-settings', json=body)
    assert result.status_code == 200, result.text
    public = result.json()['profiles']['audio']
    assert public['provider'] == 'funasr_local' and public['configured'] and public['protocol'] == 'funasr_local'
    resolved = app.state.model_settings.resolve('alice')
    assert resolved.asr_protocol == 'funasr_local' and resolved.asr_key == '' and resolved.asr_model == 'paraformer-zh'
    typed = alice.put('/api/model-settings', json={'expected_revision': 1, 'text': {'use_default': True},
                                                   'audio': funasr_profile(api_key='typed-secret')})
    assert typed.status_code == 422 and '无需填写' in typed.text
    invalid = alice.put('/api/model-settings', json={'expected_revision': 1, 'text': {'use_default': True},
                                                     'audio': funasr_profile(model='qwen3-asr-flash')})
    assert invalid.status_code == 422 and 'FunASR模型' in invalid.text
    assert app.state.model_settings.resolve('alice').asr_model == 'paraformer-zh'


def test_local_funasr_uploads_and_retries_without_api_key(team, monkeypatch):
    app, admin, alice, bob = team
    app.state.settings.asr_key = ''
    app.state.settings.asr_segment_seconds = 3
    body = {'expected_revision': 0, 'text': {'use_default': True}, 'audio': funasr_profile()}
    assert alice.put('/api/model-settings', json=body).status_code == 200
    monkeypatch.setattr(app.state.audio.pool, 'submit', lambda *a: None)
    seen = []
    def asr(data, mime, s):
        seen.append(s.asr_protocol)
        if len(seen) == 1:
            raise AudioError('本地中断')
        return '本地文字', {'model': s.asr_model, 'protocol': 'funasr_local'}
    monkeypatch.setattr('sdr.audio.transcribe', asr)
    response = upload(alice, customer(alice)['id'], key='funasr-local', data=recording(2))
    assert response.status_code == 202, response.text
    tid = response.json()['id']
    app.state.audio.run(tid)
    failed = alice.get('/api/transcriptions/' + tid).json()
    assert failed['status'] == 'failed' and '本地中断' in failed['error']
    assert alice.post(f'/api/transcriptions/{tid}/retry').status_code == 200
    app.state.audio.run(tid)
    row = alice.get('/api/transcriptions/' + tid).json()
    assert row['status'] == 'transcribed' and row['raw_text'] == '本地文字' and row['provider']['segmented'] is True
    assert seen == ['funasr_local', 'funasr_local']
