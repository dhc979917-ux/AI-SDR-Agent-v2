"""Bounded recording uploads, asynchronous ASR, and explicit transcript confirmation."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import base64
import hashlib
import io
import json
import math
import re
import shutil
import tempfile
import threading
import time
import uuid
import wave

import httpx
import mutagen
from mutagen.mp3 import MP3

from .preprocess import split_turns, LABEL
from .store import Conflict, NotFound, dumps, now
from .workspace import active_user, event, require_customer, require_audio, new_customer, Forbidden
from .audio_identity import identify_customer, match_customers, IdentityError
from .audio_roles import classify_roles, RoleError
from .audio_processing import AudioError, normalized_audio, segment_ranges, vad_speech_timestamps, wav_bytes, RATE, PIPELINE_VERSION
from .models import MAX_TRANSCRIPT_CHARS
from .model_transport import provider_error
from .role_draft import auto_label_draft, draft_has_customer, draft_pending_count, label_unlabeled


MIMES = {'.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4',
         '.flac': 'audio/flac', '.ogg': 'audio/ogg'}


def inspect_audio(data, filename, settings):
    stream = io.BytesIO(data) if isinstance(data, (bytes,bytearray)) else data
    stream.seek(0,io.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    header = stream.read(64)
    stream.seek(0)
    suffix = Path(filename).suffix.lower()
    if suffix not in MIMES:
        raise AudioError('支持 WAV、MP3、M4A、FLAC、OGG 音频文件')
    if not size:
        raise AudioError('文件为空（0字节），请重新导出或下载完整录音')
    if size > settings.audio_max_bytes:
        raise AudioError(f'音频不能超过{settings.audio_max_bytes/1024/1024:g} MB')
    if header.startswith(b'PK\x03\x04'):
        raise AudioError('文件内容是ZIP压缩包，请先解压，再上传里面的录音文件')
    if header.lstrip().lower().startswith((b'<!doctype html', b'<html')):
        raise AudioError('文件内容是网页而非录音，可能保存了下载页面；请重新下载录音原文件')
    if header.startswith((b'RF64', b'RIFX')) and header[8:12] == b'WAVE':
        raise AudioError('检测到RF64/RIFX格式的WAV，本版解析器暂不支持；请导出为标准PCM WAV或MP3后上传')
    try:
        audio = mutagen.File(stream)
        # The auto-detector misses untagged MPEG-2.5 frames (common in 8 kHz
        # telephone recordings). The MP3 parser still validates actual frames;
        # the filename alone never makes an invalid upload acceptable.
        if audio is None and suffix == '.mp3':
            stream.seek(0)
            audio = MP3(stream)
    except Exception:
        raise AudioError(f'无法解析此{suffix[1:].upper()}文件的音频信息，可能是文件不完整或编码暂不兼容；请确认本地能播放，并重新导出为PCM WAV或MP3') from None
    if audio is None:
        raise AudioError('未识别出受支持的音频内容，请上传录音原文件；修改后缀不会转换音频编码')
    detected = type(audio).__name__
    expected = {'.wav': {'WAVE'}, '.mp3': {'MP3'}, '.m4a': {'MP4'}, '.flac': {'FLAC'},
                '.ogg': {'OggVorbis', 'OggOpus', 'OggFLAC', 'OggSpeex'}}
    if detected not in expected[suffix]:
        actual = {'WAVE':'WAV','MP4':'M4A/MP4','OggVorbis':'OGG','OggOpus':'OGG/Opus',
                  'OggFLAC':'OGG/FLAC','OggSpeex':'OGG/Speex'}.get(detected, detected)
        raise AudioError(f'文件后缀为{suffix}，但内容识别为{actual}；请上传保留原格式的录音，或通过音频软件导出为WAV/MP3')
    try:
        duration = float(audio.info.length)
    except (AttributeError, ValueError, TypeError, OverflowError):
        raise AudioError('无法读取录音时长，请重新导出完整录音后上传') from None
    if not math.isfinite(duration) or duration < 0:
        raise AudioError('录音时长信息无效，请重新导出完整录音后上传')
    if duration == 0:
        raise AudioError('文件可识别，但有效音频时长为0秒；可能只有文件头或未完成录制，请重新导出完整录音')
    if duration > settings.audio_max_seconds:
        raise AudioError(f'本版支持{settings.audio_max_seconds/60:g}分钟以内的通话录音，请拆分后上传')
    return MIMES[suffix], duration


def transcribe(data, mime, settings):
    if len(data) > 10 * 1000 * 1000:
        raise AudioError('单个语音请求超过10 MB，请通过录音上传入口自动分段处理')
    if settings.asr_protocol=='funasr_local':
        return transcribe_funasr(data,settings)
    if settings.asr_protocol=='openai_transcription':
        return transcribe_openai(data,mime,settings)
    if settings.asr_protocol!='qwen_asr':
        raise AudioError('未知的录音接口类型，请在「我的模型」重新选择厂商')
    encoded = base64.b64encode(data).decode('ascii')
    body = {'model': settings.asr_model, 'messages': [{'role': 'user', 'content': [
        {'type': 'input_audio', 'input_audio': {'data': f'data:{mime};base64,{encoded}'}}]}],
        'stream': False, 'asr_options': {'enable_itn': True}}
    try:
        with httpx.Client(timeout=settings.asr_timeout, trust_env=False) as client:
            response = client.post(settings.asr_base_url.rstrip('/')+'/chat/completions',
                headers={'Authorization': 'Bearer '+settings.asr_key}, json=body)
        if response.status_code != 200:
            raise AudioError(provider_error(response,'语音服务'))
        result = response.json()
        text = result['choices'][0]['message']['content']
        if not isinstance(text, str) or not text.strip():
            raise AudioError('语音服务未返回有效文字，请检查录音是否有清晰人声')
        if len(text) > 24000:
            raise AudioError('转写超过24,000字符，请将录音分段后重试')
        usage = result.get('usage') or {}
        return text.strip(), {'model': settings.asr_model, 'tokens': int(usage.get('total_tokens') or 0),
                              'speaker_roles': 'manual_confirmation_required'}
    except httpx.TimeoutException:
        raise AudioError('语音识别超时，原录音已保留，可以重试') from None
    except httpx.HTTPError:
        raise AudioError('暂时无法连接语音服务，原录音已保留，可以重试') from None
    except (KeyError, ValueError, TypeError, IndexError):
        raise AudioError('语音服务响应格式异常，请稍后重试') from None


def transcribe_openai(data,mime,settings):
    suffix=next((s for s,m in MIMES.items() if m==mime),'.wav')
    diarize=settings.asr_model.startswith('gpt-4o-transcribe-diarize')
    form={'model':settings.asr_model,'response_format':'diarized_json' if diarize else 'json'}
    if diarize:form['chunking_strategy']='auto'
    try:
        with httpx.Client(timeout=settings.asr_timeout,trust_env=False,follow_redirects=False) as client:
            response=client.post(settings.asr_base_url.rstrip('/')+'/audio/transcriptions',
                headers={'Authorization':'Bearer '+settings.asr_key},
                data=form,
                files={'file':('recording'+suffix,data,mime)})
        if response.status_code!=200:
            raise AudioError(provider_error(response,'语音服务'))
        result=response.json();text=result['text']
        hints=[]
        if diarize:
            segments=result['segments']
            if not isinstance(segments,list) or not segments:raise ValueError()
            last_start=0
            for segment in segments:
                start,end=float(segment['start']),float(segment['end'])
                speaker,part=segment['speaker'],segment['text']
                if not all(math.isfinite(t) for t in (start,end)) or start<last_start or end<start:
                    raise ValueError()
                if not isinstance(speaker,str) or not speaker or len(speaker)>100 or not isinstance(part,str) or not part.strip():raise ValueError()
                hints.append({'speaker':speaker,'text':part.strip(),'start':start,'end':end})
                last_start=start
            from .audio_roles import compact
            if compact(''.join(h['text'] for h in hints))!=compact(text):raise ValueError()
            text='\n'.join(h['text'] for h in hints)
        if not isinstance(text,str) or not text.strip() or len(text)>24000:
            raise AudioError('语音服务未返回有效转写文字，请检查录音和所选模型')
        return text.strip(),{'model':settings.asr_model,'tokens':int((result.get('usage') or {}).get('total_tokens') or 0),
                            'speaker_roles':'acoustic_segments' if diarize else 'text_context_required',
                            'protocol':'openai_transcription',**({'speaker_segments':hints} if diarize else {})}
    except httpx.TimeoutException:
        raise AudioError('语音识别超时，原录音已保留，可以重试') from None
    except httpx.HTTPError:
        raise AudioError('暂时无法连接语音服务，原录音已保留，可以重试') from None
    except (KeyError,ValueError,TypeError,IndexError):
        raise AudioError('语音服务响应格式异常，请检查所选模型') from None


_FUNASR_MODELS={}
_FUNASR_LOCK=threading.Lock()


def funasr_hotwords(settings):
    """One hotword per line, optional trailing weight 1-100 (default 20). Empty when unconfigured."""
    try:
        lines=settings.asr_hotwords_path.read_text(encoding='utf-8').splitlines()
    except (OSError,UnicodeError):
        return ''
    words=[]
    for line in lines:
        line=line.strip()
        if line and not line.startswith('#'):
            words.append(line if re.search(r'\s[0-9]{1,3}$',line) else f'{line} 20')
    return ' '.join(words)


def funasr_pipeline(model, settings):
    with _FUNASR_LOCK:
        entry=_FUNASR_MODELS.get((model,settings.funasr_device))
        if entry is None:
            try:
                from funasr import AutoModel
            except ImportError:
                raise AudioError('本地离线转写组件未安装，请先执行 pip install funasr 并安装 torch 后重启服务') from None
            kwargs={'model':model,'vad_model':'fsmn-vad','spk_model':'cam++','device':settings.funasr_device}
            if 'sensevoice' not in model.lower():
                kwargs['punc_model']='ct-punc'
            try:
                entry={'model':AutoModel(**kwargs),'lock':threading.Lock()}
            except Exception:
                raise AudioError('本地转写模型加载失败，请检查网络、磁盘空间或模型名称后重试') from None
            _FUNASR_MODELS[(model,settings.funasr_device)]=entry
        return entry


def transcribe_funasr(data, settings):
    """Local offline ASR: brand hotwords, millisecond timestamps and anonymous speaker labels."""
    model=settings.asr_model
    entry=funasr_pipeline(model,settings)
    with io.BytesIO(data) as stream:
        with wave.open(stream,'rb') as audio:
            duration=audio.getnframes()/audio.getframerate()
    hotword=funasr_hotwords(settings)
    kwargs={}
    if hotword and 'paraformer' in model.lower():
        kwargs['hotword']=hotword
    post=None
    if 'sensevoice' in model.lower():
        from funasr.utils.postprocess_utils import rich_transcription_postprocess as post
    try:
        with tempfile.NamedTemporaryFile(suffix='.wav') as local:
            local.write(data);local.flush()
            with entry['lock']:
                result=entry['model'].generate(input=local.name,**kwargs)
        first=result[0] if isinstance(result,list) and result else {}
        if not isinstance(first,dict):
            raise ValueError('unexpected funasr output')
        text=post(first.get('text') or '') if post else (first.get('text') or '')
    except AudioError:
        raise
    except Exception:
        raise AudioError('本地离线转写失败，原录音已保留，可以重试') from None
    text=(text or '').strip()
    if not text:
        raise AudioError('本地转写未返回有效文字，请检查录音是否包含清晰人声')
    if len(text)>24000:
        raise AudioError('转写超过24,000字符，请将录音分段后重试')
    hints=[]
    for segment in first.get('sentence_info') or []:
        try:
            start,end=float(segment['start'])/1000,float(segment['end'])/1000
            speaker,sentence=str(segment['spk']),str(segment.get('sentence') or segment.get('text') or '')
        except (KeyError,TypeError,ValueError):
            continue
        if post:
            sentence=post(sentence)
        sentence=sentence.strip()
        if not sentence or not speaker or not math.isfinite(start) or not math.isfinite(end):
            continue
        end=min(end,duration)
        if end<=start or start>duration:
            continue
        hints.append({'speaker':speaker,'text':sentence,'start':round(start,3),'end':round(end,3)})
    info={'model':model,'tokens':0,'protocol':'funasr_local','local':True,
          'speaker_roles':'acoustic_segments' if hints else 'text_context_required'}
    if hints:
        info['speaker_segments']=hints
    return text,info


class AudioService:
    def __init__(self, store, settings, engine=None):
        self.store, self.settings, self.engine = store, settings, engine
        self.directory = store.path.parent / 'audio'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='asr')

    def path(self, tid):
        if not re.fullmatch(r'audio_[a-f0-9]{24}', tid):
            raise NotFound('录音不存在')
        return self.directory / (tid+'.bin')

    @staticmethod
    def decode(row):
        if not row:
            raise NotFound('录音不存在或不属于当前账号')
        result = dict(row)
        result.pop('idem', None)
        result['provider'] = json.loads(result['provider'])
        result['identity_result'] = json.loads(result['identity_result'])
        result['role_result'] = json.loads(result['role_result'])
        return result

    def get(self, tid, actor, db=None):
        if db is None:
            with self.store.connection() as conn:
                return self.get(tid, actor, conn)
        row = db.execute('SELECT * FROM transcriptions WHERE id=?', (tid,)).fetchone()
        if not row:
            raise NotFound('录音不存在或不属于当前账号')
        require_audio(db, row, actor)
        result = self.decode(row)
        result['segments'] = [{'index':s['segment_index'],'start':s['start_frame']/RATE,
                               'end':s['end_frame']/RATE,'status':s['status'],'text':s['text']}
                              for s in db.execute('SELECT * FROM audio_segments WHERE transcription_id=? ORDER BY segment_index', (tid,))]
        if not row['customer_id']:
            result.update(match_customers(db, actor, result['identity_result']))
        return result

    def listing(self, actor):
        with self.store.connection() as db:
            user = active_user(db, actor)
            where, args = ('', ()) if user['role'] == 'admin' else (
                'WHERE c.owner_id=? OR (t.customer_id IS NULL AND t.uploaded_by=?)', (actor,actor))
            return [self.decode(r) for r in db.execute(f'''SELECT t.*,c.name AS customer_name FROM transcriptions t
                LEFT JOIN customers c ON c.id=t.customer_id {where} ORDER BY t.created_at DESC LIMIT 500''', args)]

    def create(self, data, filename, cid, data_mode, key, actor, customer_mode='existing', auto_analyze=False):
        if customer_mode not in ('existing', 'ai', 'auto') or (customer_mode == 'existing') != bool(cid):
            raise AudioError('请选择已有客户、AI识别或自动建档，且不要同时传入客户')
        auto_analyze = bool(auto_analyze or customer_mode == 'auto')
        mime, duration = inspect_audio(data, filename, self.settings)
        stream = io.BytesIO(data) if isinstance(data,(bytes,bytearray)) else data
        stream.seek(0)
        digest, size = hashlib.sha256(), 0
        while block := stream.read(1024*1024):
            digest.update(block);size += len(block)
        sha, identity = digest.hexdigest(), actor+':'+key
        tid = 'audio_'+uuid.uuid4().hex[:24]
        path = self.path(tid)
        try:
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                active_user(db, actor)
                if cid:
                    require_customer(db, cid, actor)
                old = db.execute('SELECT * FROM transcriptions WHERE idem=?', (identity,)).fetchone()
                if old:
                    require_audio(db, old, actor)
                    if old['sha256'] != sha or old['customer_mode'] != customer_mode or (cid and old['customer_id'] != cid) or old['data_mode'] != data_mode:
                        raise Conflict('相同上传任务键对应不同文件或客户')
                    return self.decode(old), False
                if self.pending(db) >= 10:
                    raise Conflict('转写任务较多，请稍后再上传')
                used = db.execute('SELECT coalesce(sum(size),0) FROM transcriptions').fetchone()[0]
                if used+size > self.settings.audio_quota_bytes:
                    raise AudioError('录音空间已满，请删除不再使用且未关联分析的录音')
                with path.open('xb') as f:
                    path.chmod(0o600)
                    stream.seek(0)
                    shutil.copyfileobj(stream, f, 1024*1024)
                stamp = now()
                db.execute('''INSERT INTO transcriptions(id,customer_id,uploaded_by,idem,filename,mime,size,sha256,
                    duration,data_mode,status,created_at,updated_at,customer_mode,identity_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (tid,cid,actor,identity,Path(filename).name,mime,size,sha,duration,data_mode,'queued',stamp,stamp,
                     customer_mode,'waiting_transcript' if customer_mode == 'ai' else 'not_requested'))
                db.execute("UPDATE transcriptions SET model_actor=?,identity_model_actor=?,role_model_actor=?,role_status='waiting_transcript' WHERE id=?",(actor,actor,actor,tid))
                if auto_analyze:
                    db.execute("UPDATE transcriptions SET auto_analyze=1, auto_analysis_status='waiting_transcript' WHERE id=?", (tid,))
                event(db, actor, 'audio_uploaded', '上传并请求云端转写', after={'id': tid, 'sha256': sha}, customer_id=cid)
                return self.get(tid, actor, db), True
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def run(self, tid):
        started = time.monotonic()
        with self.store.connection() as db:
            changed = db.execute("UPDATE transcriptions SET status='running',updated_at=? WHERE id=? AND status='queued'", (now(),tid)).rowcount
            row = db.execute('SELECT * FROM transcriptions WHERE id=?', (tid,)).fetchone()
        if not changed:
            return
        try:
            actor=row['model_actor'] or row['uploaded_by']
            with self.store.connection() as db:require_audio(db,row,actor)
            settings=self.settings.resolved(actor)
            if not settings.asr_key and settings.asr_protocol!='funasr_local':
                raise AudioError('请先在「我的模型」配置录音转写模型，或选择FunASR本地离线转写')
            previous=json.loads(row['provider'])
            if previous.get('segmented') or row['duration'] > settings.asr_segment_seconds or row['size'] > 7*1000*1000 or settings.asr_protocol!='qwen_asr':
                text, provider = self.transcribe_segments(row,settings)
            else:
                text, provider = transcribe(self.path(tid).read_bytes(), row['mime'], settings)
            provider['configured_by']=actor
            provider['provider_id']=settings.audio_provider
            provider['elapsed_ms'] = round((time.monotonic()-started)*1000)
            with self.store.connection() as db:
                db.execute("""UPDATE transcriptions SET status='transcribed',raw_text=?,provider=?,error=NULL,
                    identity_status=?,role_status=?,role_model_actor=?,role_result='{}',role_error=?,
                    role_generation=role_generation+1,revision=revision+1,updated_at=? WHERE id=?""",
                    (text,dumps(provider),'queued' if row['customer_mode']=='ai' else 'not_requested',
                     'queued' if settings.api_key else 'skipped',actor,
                     None if settings.api_key else '尚未配置文字分析模型，可配置后点击「AI自动分角色」',now(),tid))
        except Exception as exc:
            from .model_settings import ModelConfigError
            message = str(exc) if isinstance(exc, (AudioError,ModelConfigError,Forbidden,NotFound)) else '转写发生内部错误，原录音已保留，请重试'
            with self.store.connection() as db:
                db.execute("UPDATE transcriptions SET status='failed',error=?,revision=revision+1,updated_at=? WHERE id=?", (message,now(),tid))
            return
        if settings.api_key:
            self.run_roles(tid,settings)
        if row['customer_mode'] == 'ai':
            self.run_identity(tid,settings)
        with self.store.connection() as db:
            current = db.execute('SELECT * FROM transcriptions WHERE id=?', (tid,)).fetchone()
        if current and current['customer_id'] and current['role_status'] == 'ready' and current['status'] in ('transcribed', 'confirmed'):
            self.auto_finish(tid, current['model_actor'] or current['uploaded_by'], settings)
        elif int(row['auto_analyze'] or 0) or row['customer_mode'] == 'auto':
            self.auto_finish(tid, row['model_actor'] or row['uploaded_by'], settings)

    def auto_finish(self, tid, actor, settings):
        try:
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM transcriptions WHERE id=?', (tid,)).fetchone()
                if not row:
                    raise NotFound('录音不存在')
                if row['status'] not in ('transcribed', 'confirmed'):
                    raise AudioError('转写尚未完成，不能自动分析')
                if row['auto_analysis_id'] and row['auto_analysis_status'] in ('submitted', 'queued', 'running'):
                    return
                role_result = json.loads(row['role_result'] or '{}')
                fallback = bool(row['auto_role_fallback'])
                pending_count = 0
                if row['status'] == 'confirmed' and row['confirmed_text']:
                    draft = row['confirmed_text']
                    pending_count = draft_pending_count(draft)
                elif row['role_status'] == 'ready' and role_result.get('transcript'):
                    original = role_result['transcript']
                    draft = auto_label_draft(original)
                    fallback = not draft_has_customer(original)
                    pending_count = draft_pending_count(draft)
                else:
                    draft = label_unlabeled(row['raw_text'])
                    fallback = True
                if not draft.strip():
                    raise AudioError('没有可分析的有效文字')
                cid = row['customer_id']
                if not cid:
                    name = Path(row['filename']).stem.strip()[:100] or ('批量录音 ' + row['created_at'][:10])
                    customer = new_customer(db, name, actor, actor)
                    cid = customer['id']
                provider = json.loads(row['provider'] or '{}')
                provider['auto_role_fallback'] = fallback
                provider['auto_pending_count'] = pending_count
                db.execute("""UPDATE transcriptions SET customer_id=?, status='confirmed', confirmed_text=?, auto_analyze=1,
                    confirmed_by='auto-pipeline', provider=?, auto_role_fallback=?, auto_analysis_status='queued',
                    auto_analysis_error=NULL, revision=revision+1, updated_at=? WHERE id=?""",
                    (cid, draft, dumps(provider), 1 if fallback else 0, now(), tid))
                event(db, actor, 'transcript_auto_confirmed', '批量自动化：自动确认角色并提交分析',
                      {'id': tid, 'revision': row['revision']},
                      {'text': draft, 'customer_id': cid, 'role_fallback': fallback}, cid)
            if sum(item['status'] in ('queued', 'running') for item in self.store.listing(500)) >= getattr(settings, 'max_pending', 20):
                raise AudioError('分析任务较多，请稍后重试自动分析')
            request = {
                'transcript': draft,
                'title': Path(row['filename']).stem.strip()[:100] or '批量录音',
                'mode': 'llm' if settings.api_key else 'rules',
                'data_mode': row['data_mode'],
                'call_time': None,
                'idempotency_key': f'auto-{tid}-r{row["revision"]}',
                'customer_id': cid,
                'transcription_id': tid,
            }
            fingerprint = hashlib.sha256(dumps({k: v for k, v in request.items() if k != 'idempotency_key'}).encode()).hexdigest()
            if not self.engine:
                raise AudioError('分析引擎未初始化，请手动进入分析')
            record, _ = self.store.create(request, fingerprint, actor)
            self.engine.submit(record['id'])
            with self.store.connection() as db:
                db.execute("UPDATE transcriptions SET auto_analysis_status='submitted', auto_analysis_id=?, updated_at=? WHERE id=?",
                           (record['id'], now(), tid))
        except Exception as exc:
            message = str(exc) if isinstance(exc, (AudioError, Conflict, NotFound, Forbidden)) else '自动分析失败，转写已保留，可手动进入分析'
            with self.store.connection() as db:
                db.execute("UPDATE transcriptions SET auto_analysis_status='failed', auto_analysis_error=?, updated_at=? WHERE id=?",
                           (message[:500], now(), tid))

    def transcribe_segments(self, row, settings=None):
        settings=settings or self.settings.resolved(row['model_actor'] or row['uploaded_by'])
        tid = row['id']
        previous = json.loads(row['provider'])
        progress = {**previous, 'model':settings.asr_model, 'segmented':True, 'stage':'preparing'}
        self.save_progress(tid, progress)
        with normalized_audio(self.path(tid), self.directory, settings) as path:
            speech = vad_speech_timestamps(path)
            with wave.open(str(path),'rb') as audio:
                ranges = segment_ranges(audio, settings.asr_segment_seconds, speech)
                plan_parts=[PIPELINE_VERSION,row['sha256'],settings.asr_model,settings.asr_base_url,ranges]
                if settings.asr_protocol!='qwen_asr':plan_parts.append(settings.asr_protocol)
                if speech:plan_parts.append('vad:silero-v1')
                plan = hashlib.sha256(dumps(plan_parts).encode()).hexdigest()
                with self.store.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    old = [dict(s) for s in db.execute('SELECT * FROM audio_segments WHERE transcription_id=? ORDER BY segment_index',(tid,))]
                    if old and (previous.get('plan_hash') != plan or [(s['start_frame'],s['end_frame']) for s in old] != ranges):
                        raise AudioError('音频处理配置已变化，已完成片段仍保留；请恢复原设置重试或重新上传录音')
                    if not old:
                        db.executemany('INSERT INTO audio_segments(transcription_id,segment_index,start_frame,end_frame) VALUES(?,?,?,?)',
                                       [(tid,i+1,start,end) for i,(start,end) in enumerate(ranges)])
                    db.execute('UPDATE transcriptions SET duration=? WHERE id=?',(audio.getnframes()/RATE,tid))
                progress = {'model':settings.asr_model,'segmented':True,'stage':'transcribing',
                            'plan_hash':plan,'pipeline_version':PIPELINE_VERSION,'segment_count':len(ranges),
                            'completed_segments':sum(s['status']=='completed' for s in old),
                            'speaker_roles':'manual_confirmation_required'}
                if speech:progress['vad']='silero'
                self.save_progress(tid,progress)
                texts, tokens = [], 0
                speaker_hints=[]
                for i,(start,end) in enumerate(ranges,1):
                    audio.setpos(start)
                    frames = audio.readframes(end-start)
                    if len(frames) != (end-start)*2:
                        raise AudioError('音频片段不完整，请检查原文件')
                    data = wav_bytes(frames)
                    sha = hashlib.sha256(data).hexdigest()
                    cached = old[i-1] if old else None
                    if cached and cached['status']=='completed':
                        if cached['sha256']!=sha:
                            raise AudioError('音频分段内容已变化，原结果保留，请重新上传')
                        text, info = cached['text'], json.loads(cached['provider'])
                    else:
                        progress['current_segment'] = i
                        self.save_progress(tid,progress)
                        if not any(frames):
                            text, info = '', {'model':'digital-silence','tokens':0,'silent':True}
                        else:
                            try:
                                text, info = transcribe(data,'audio/wav',settings)
                            except AudioError as exc:
                                raise AudioError(f'第{i}/{len(ranges)}段转写失败：{exc}。已完成片段已保存，点击重试可继续') from None
                        with self.store.connection() as db:
                            db.execute("UPDATE audio_segments SET status='completed',sha256=?,text=?,provider=? WHERE transcription_id=? AND segment_index=?",
                                       (sha,text,dumps(info),tid,i))
                        progress['completed_segments'] += 1
                        self.save_progress(tid,progress)
                    texts.append(text)
                    tokens += int(info.get('tokens') or 0)
                    for hint in info.get('speaker_segments',[]):
                        if hint['end']>(end-start)/RATE+1:
                            raise AudioError('语音服务返回的说话人时间超出片段范围，请重试')
                        speaker_hints.append({**hint,'speaker':f"{i}:{hint['speaker']}",
                                              'start':start/RATE+hint['start'],'end':min(end/RATE,start/RATE+hint['end'])})
                merged = '\n'.join(text for text in texts if text.strip())
                if not merged.strip():
                    raise AudioError('录音未识别出有效文字，请确认录音包含清晰人声')
                if len(merged) > MAX_TRANSCRIPT_CHARS:
                    raise AudioError(f'转写超过{MAX_TRANSCRIPT_CHARS:,}字符，片段结果已保留，请拆分录音后处理')
                progress.update(stage='complete',tokens=tokens)
                if speaker_hints:
                    progress.update(speaker_roles='acoustic_segments',speaker_segments=speaker_hints)
                return merged, progress

    def save_progress(self, tid, progress):
        with self.store.connection() as db:
            db.execute("UPDATE transcriptions SET provider=?,updated_at=? WHERE id=? AND status='running'",
                       (dumps(progress),now(),tid))

    @staticmethod
    def pending(db):
        return db.execute("SELECT count(*) FROM transcriptions WHERE status IN ('queued','running') OR identity_status IN ('queued','running') OR role_status IN ('queued','running')").fetchone()[0]

    def roles(self,tid,body,actor):
        settings=self.settings.resolved(actor)
        if not settings.api_key:
            raise AudioError('请先在「我的模型」配置文字分析模型，用于识别销售和客户角色')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self.get(tid,actor,db)
            if row['revision']!=body.expected_revision or row['status'] not in ('transcribed','confirmed') or row['role_status'] in ('queued','running') or row['identity_status'] in ('queued','running'):
                raise Conflict('录音正在处理或版本已变化，请刷新后重试')
            if self.pending(db)>=10:raise Conflict('正在处理的录音较多，请稍后重试')
            db.execute("""UPDATE transcriptions SET role_status='queued',role_result='{}',role_error=NULL,
                role_model_actor=?,role_generation=role_generation+1,revision=revision+1,updated_at=? WHERE id=?""",(actor,now(),tid))
            event(db,actor,'audio_roles_requested','请求AI划分销售与客户发言',after={'id':tid},customer_id=row['customer_id'])
        self.pool.submit(self.run_roles,tid)
        return self.get(tid,actor)

    def run_roles(self,tid,settings=None):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute("UPDATE transcriptions SET role_status='running',revision=revision+1,updated_at=? WHERE id=? AND role_status='queued'",(now(),tid)).rowcount
            row=db.execute('SELECT * FROM transcriptions WHERE id=?',(tid,)).fetchone()
        if not changed:return
        actor=row['role_model_actor'] or row['uploaded_by']
        generation=row['role_generation']
        def progress(done,total):
            with self.store.connection() as db:
                current=db.execute('SELECT * FROM transcriptions WHERE id=?',(tid,)).fetchone()
                require_audio(db,current,actor)
                if current['role_status']!='running' or current['role_generation']!=generation:
                    raise RoleError('角色任务已失效，请刷新后重试')
                db.execute("UPDATE transcriptions SET role_result=?,updated_at=? WHERE id=? AND role_status='running' AND role_generation=?",
                    (dumps({'completed_chunks':done,'total_chunks':total}),now(),tid,generation))
        try:
            with self.store.connection() as db:require_audio(db,row,actor)
            settings=settings or self.settings.resolved(actor)
            hints=json.loads(row['provider']).get('speaker_segments')
            result=classify_roles(row['raw_text'],settings,hints,progress)
            result.update(configured_by=actor,provider_id=settings.text_provider)
            status,error='ready',None
            with self.store.connection() as db:
                current=db.execute('SELECT * FROM transcriptions WHERE id=?',(tid,)).fetchone()
                require_audio(db,current,actor)
                if current['raw_text']!=row['raw_text']:raise RoleError('原始转写已变化，请重新分角色')
        except Exception as exc:
            from .model_settings import ModelConfigError
            result,status={},'failed'
            error=str(exc) if isinstance(exc,(RoleError,ModelConfigError,Forbidden,NotFound)) else '自动分角色失败，原始转写与已有校对稿已保留，可重试'
        with self.store.connection() as db:
            db.execute("""UPDATE transcriptions SET role_status=?,role_result=?,role_error=?,revision=revision+1,updated_at=?
                WHERE id=? AND role_status='running' AND role_generation=?""",(status,dumps(result),error,now(),tid,generation))

    def run_identity(self, tid, settings=None):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE transcriptions SET identity_status='running',revision=revision+1,updated_at=? WHERE id=? AND identity_status='queued' AND customer_id IS NULL", (now(),tid)).rowcount
            row = db.execute('SELECT * FROM transcriptions WHERE id=?', (tid,)).fetchone()
        if not changed:
            return
        try:
            actor=row['identity_model_actor'] or row['uploaded_by']
            with self.store.connection() as db:require_audio(db,row,actor)
            settings=settings or self.settings.resolved(actor)
            result = identify_customer(row['confirmed_text'] or row['raw_text'], settings)
            result['configured_by']=actor
            result.update(source_revision=row['revision'], source='confirmed' if row['confirmed_text'] else 'raw')
            status, error = 'ready', None
        except Exception as exc:
            from .model_settings import ModelConfigError
            result, status = {}, 'failed'
            error = str(exc) if isinstance(exc, (IdentityError,ModelConfigError,Forbidden,NotFound)) else '客户识别失败，转写已保留，可手动选择客户'
        with self.store.connection() as db:
            db.execute("UPDATE transcriptions SET identity_status=?,identity_result=?,identity_error=?,revision=revision+1,updated_at=? WHERE id=? AND identity_status='running' AND revision=?",
                       (status,dumps(result),error,now(),tid,row['revision']))

    def identify(self, tid, body, actor):
        if not self.settings.resolved(actor).api_key:
            raise AudioError('未配置客户识别模型，可直接选择已有客户或人工建档')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.get(tid, actor, db)
            if row['revision'] != body.expected_revision or row['customer_id'] or row['status'] not in ('transcribed','confirmed') or row['identity_status'] in ('queued','running') or row['role_status'] in ('queued','running'):
                raise Conflict('录音状态已变化或客户已确认，请刷新后重试')
            if self.pending(db) >= 10:
                raise Conflict('正在处理的录音较多，请稍后重试')
            db.execute("UPDATE transcriptions SET identity_status='queued',identity_model_actor=?,identity_error=NULL,identity_result='{}',revision=revision+1,updated_at=? WHERE id=?", (actor,now(),tid))
            event(db, actor, 'audio_identity_requested', '从已保存的转写识别客户身份', after={'id':tid})
        self.pool.submit(self.run_identity, tid)
        return self.get(tid, actor)

    def confirm_customer(self, tid, body, actor):
        if bool(body.customer_id) == (body.new_customer is not None):
            raise AudioError('请选择一个已有客户，或填写一份新客户档案')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.get(tid, actor, db)
            if row['revision'] != body.expected_revision or row['customer_id'] or row['status'] not in ('transcribed','confirmed') or row['identity_status'] in ('queued','running') or row['role_status'] in ('queued','running'):
                raise Conflict('录音状态或客户归属已变化，请刷新后确认')
            if body.customer_id:
                customer = require_customer(db, body.customer_id, actor)
            else:
                draft, user = body.new_customer, active_user(db, actor)
                if not draft.name.strip():
                    raise AudioError('请填写客户名称')
                owner = draft.owner_id or actor
                if user['role'] != 'admin' and owner != actor:
                    raise Forbidden('员工只能为自己创建客户')
                customer = new_customer(db, draft.name.strip(), owner, actor,
                                        draft.contact.strip(), draft.phone.strip(), draft.notes.strip())
            db.execute('UPDATE transcriptions SET customer_id=?,revision=revision+1,updated_at=? WHERE id=?',
                       (customer['id'],now(),tid))
            event(db, actor, 'audio_customer_confirmed', '人工核对录音并确认归属客户',
                  {'id':tid,'customer_id':None}, {'customer_id':customer['id'],
                   'identity_result':row['identity_result']}, customer['id'])
            return self.get(tid, actor, db)

    def confirm(self, tid, body, actor):
        if re.search(r'^\s*待确认\s*[:：]',body.transcript,re.M):
            raise AudioError('还有「待确认」发言，请在「逐条确认角色」选择销售、客户或其他；仍无法判断可选保留存疑，排除该句后继续分析')
        turns = split_turns(body.transcript)
        if not turns or not any(t.speaker == 'customer' and t.text.strip() for t in turns) or any(t.speaker == 'unknown' for t in turns) or any(not LABEL.match(line) or not LABEL.match(line)[2].strip() for line in body.transcript.splitlines() if line.strip()):
            raise AudioError('请分行标注「销售：」「客户：」「其他：」或「存疑：」，每段需有内容，至少包含一段已确认客户发言')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.get(tid, actor, db)
            if old['revision'] != body.expected_revision or old['status'] not in ('transcribed', 'confirmed') or old['identity_status'] in ('queued','running') or old['role_status'] in ('queued','running'):
                raise Conflict('转写状态或版本已变化，请刷新后核对')
            text = body.transcript.strip()
            if not old['customer_id'] and text != (old['confirmed_text'] or old['raw_text']):
                db.execute("UPDATE transcriptions SET identity_status='outdated',identity_result='{}',identity_error=NULL WHERE id=?", (tid,))
            db.execute("UPDATE transcriptions SET status='confirmed',confirmed_text=?,confirmed_by=?,revision=revision+1,updated_at=? WHERE id=?", (text,actor,now(),tid))
            if old['auto_analyze']:
                db.execute("UPDATE transcriptions SET auto_analysis_status='manual_ready',auto_analysis_id=NULL,auto_analysis_error=NULL WHERE id=?", (tid,))
            event(db, actor, 'transcript_confirmed', '听录音核对文本和说话人角色',
                  {'id':tid, 'text': old['confirmed_text'], 'revision': old['revision']}, {'text': text}, old['customer_id'])
            return self.get(tid, actor, db)

    def retry(self, tid, actor):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.get(tid, actor, db)
            if row['status'] not in ('failed', 'interrupted'):
                raise Conflict('只有失败或中断的转写可以重试')
            if self.pending(db) >= 10:
                raise Conflict('转写任务较多，请稍后重试')
            db.execute("UPDATE transcriptions SET status='queued',model_actor=?,identity_model_actor=?,role_status='waiting_transcript',role_result='{}',role_error=NULL,error=NULL,revision=revision+1,updated_at=? WHERE id=?", (actor,actor,now(),tid))
            db.execute("UPDATE transcriptions SET auto_analysis_status='waiting_transcript',auto_analysis_error=NULL,auto_analysis_id=NULL WHERE id=? AND auto_analyze=1",(tid,))
            event(db, actor, 'audio_retry', '重新请求云端转写', after={'id':tid}, customer_id=row['customer_id'])
        self.pool.submit(self.run, tid)
        return {'id':tid, 'status':'queued'}

    def delete(self, tid, actor):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.get(tid, actor, db)
            if row['status'] in ('queued','running') or row['identity_status'] in ('queued','running') or row['role_status'] in ('queued','running') or db.execute('SELECT 1 FROM analyses WHERE transcription_id=?', (tid,)).fetchone():
                raise Conflict('正在转写或已关联分析的录音不能删除')
            db.execute('DELETE FROM transcriptions WHERE id=?', (tid,))
            event(db, actor, 'audio_deleted', '删除未关联分析的录音', before={'id':tid,'sha256':row['sha256']}, customer_id=row['customer_id'])
        self.path(tid).unlink(missing_ok=True)
        return {'deleted':True}

    def recover(self):
        with self.store.connection() as db:
            db.execute("UPDATE transcriptions SET status='interrupted',error='服务重启导致转写中断，录音已保留，请重试',revision=revision+1 WHERE status IN ('queued','running')")
            db.execute("UPDATE transcriptions SET identity_status='interrupted',identity_error='客户识别被服务重启中断，可重新识别或手动选择客户',revision=revision+1 WHERE identity_status IN ('queued','running')")
            db.execute("UPDATE transcriptions SET role_status='interrupted',role_error='服务重启导致角色识别中断，原始转写与校对稿已保留，可重新分角色',role_generation=role_generation+1,revision=revision+1 WHERE role_status IN ('queued','running')")
            db.execute("UPDATE transcriptions SET auto_analysis_status='interrupted',auto_analysis_error='服务重启导致自动分析中断，转写已保留，可重新分析',revision=revision+1 WHERE auto_analysis_status IN ('queued','running')")
