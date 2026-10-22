"""Per-account model routing. Keys stay encrypted server-side; jobs take snapshots."""
from dataclasses import replace
from pathlib import Path
import json
import os
import re
import threading
import time

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, SecretStr, field_validator
from typing import Literal

from .config import ROOT
from .models import Model
from .store import Conflict, dumps, now
from .workspace import active_user, event


PROVIDERS = {
    'qwen_cn': {'name':'阿里云百炼 · 北京', 'base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1',
                'text':['qwen-plus','qwen-flash'], 'audio':['qwen3-asr-flash'], 'audio_protocol':'qwen_asr'},
    'qwen_intl': {'name':'阿里云百炼 · 新加坡', 'base_url':'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
                  'text':['qwen-plus','qwen-flash'], 'audio':['qwen3-asr-flash'], 'audio_protocol':'qwen_asr'},
    'openai': {'name':'OpenAI', 'base_url':'https://api.openai.com/v1',
               'text':['gpt-4.1-mini','gpt-4.1'], 'audio':['gpt-4o-mini-transcribe','gpt-4o-transcribe','gpt-4o-transcribe-diarize','whisper-1'],
               'audio_protocol':'openai_transcription'},
    'deepseek': {'name':'DeepSeek', 'base_url':'https://api.deepseek.com',
                 'text':['deepseek-flash','deepseek-v4-pro'], 'audio':[]},
    'funasr_local': {'name':'FunASR · 本地离线（无需密钥）', 'base_url':'local',
                     'text':[], 'audio':['paraformer-zh','sensevoice-small'], 'audio_protocol':'funasr_local'},
}


class ModelConfigError(Exception):
    pass


class ModelProfileInput(Model):
    use_default: bool = False
    provider: Literal['qwen_cn','qwen_intl','openai','deepseek','funasr_local'] = 'qwen_cn'
    model: str = Field(default='qwen-plus',min_length=1,max_length=120)
    api_key: SecretStr | None = None
    clear_key: bool = False

    @field_validator('model')
    @classmethod
    def valid_model(cls,value):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,119}',value):
            raise ValueError('模型ID只能包含字母、数字和常用分隔符')
        return value

    @field_validator('api_key')
    @classmethod
    def valid_key(cls,value):
        if value is not None:
            raw=value.get_secret_value()
            if len(raw)>4096 or any(ord(c)<33 or ord(c)>126 for c in raw):
                raise ValueError('API Key格式无效，请去掉空格或换行')
        return value


class ModelSettingsUpdate(Model):
    expected_revision: int = Field(ge=0)
    text: ModelProfileInput
    audio: ModelProfileInput


class ModelTestRequest(Model):
    expected_revision: int = Field(ge=0)
    kind: Literal['text','audio']
    profile: ModelProfileInput


class ModelSettingsService:
    def __init__(self,store,defaults):
        self.store,self.defaults=store,defaults
        self.key_path=store.path.parent/'model-settings.key'
        self.lock=threading.RLock()
        self.testing=set()
        self.last_test={}

    def cipher(self):
        with self.lock:
            if not self.key_path.exists():
                with self.store.connection() as db:
                    if db.execute('SELECT 1 FROM model_preferences LIMIT 1').fetchone():
                        raise ModelConfigError('模型密钥文件缺失，请恢复备份中的model-settings.key；未切换到其他密钥')
                try:
                    fd=os.open(self.key_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'wb') as f:
                        f.write(Fernet.generate_key());f.flush();os.fsync(f.fileno())
                except FileExistsError:
                    pass
            try:
                return Fernet(self.key_path.read_bytes())
            except (ValueError,OSError):
                raise ModelConfigError('模型密钥文件不可用，请恢复正确备份') from None

    def default_profile(self,kind):
        s=self.defaults
        url=s.base_url if kind=='text' else s.asr_base_url
        if kind=='audio' and s.asr_protocol=='funasr_local':
            provider='funasr_local'
        else:
            provider=next((p for p,v in PROVIDERS.items() if v['base_url'].rstrip('/')==url.rstrip('/')),'environment')
        return {'use_default':True,'provider':provider,'base_url':url,
                'model':s.model if kind=='text' else s.asr_model,
                'protocol':'chat' if kind=='text' else s.asr_protocol,
                'key':s.api_key if kind=='text' else s.asr_key}

    def read_profiles(self,actor,db):
        active_user(db,actor)
        row=db.execute('SELECT * FROM model_preferences WHERE user_id=?',(actor,)).fetchone()
        profiles={k:self.default_profile(k) for k in ('text','audio')}
        if row:
            for kind in profiles:
                stored=json.loads(row[kind+'_config'])
                if not stored['use_default']:
                    try:
                        key=self.cipher().decrypt(stored.pop('encrypted_key').encode()).decode() if stored.get('encrypted_key') else ''
                    except (InvalidToken,ValueError,UnicodeError):
                        raise ModelConfigError('个人模型密钥无法解密，请恢复正确的密钥文件') from None
                    profiles[kind]={**stored,'key':key}
        return profiles,row['revision'] if row else 0

    @staticmethod
    def public_profile(profile):
        return {k:profile[k] for k in ('use_default','provider','model','protocol')} | {
            'configured':bool(profile['key']) or profile.get('protocol')=='funasr_local',
            'provider_name':PROVIDERS.get(profile['provider'],{}).get('name','工作区环境配置')}

    def public(self,actor):
        with self.lock,self.store.connection() as db:
            profiles,revision=self.read_profiles(actor,db)
        return {'revision':revision,'profiles':{k:self.public_profile(v) for k,v in profiles.items()},
                'providers':PROVIDERS,'scope':'personal','default_available':{
                    k:bool(self.default_profile(k)['key']) for k in profiles}}

    def merge_profile(self,kind,body,old):
        if body.use_default:
            return self.default_profile(kind)
        provider=PROVIDERS[body.provider]
        if not provider[kind]:
            raise ModelConfigError('所选厂商没有接入录音转写接口，请选择阿里云或OpenAI')
        if kind=='audio':
            if body.provider=='funasr_local':
                if body.model not in PROVIDERS['funasr_local']['audio']:
                    raise ModelConfigError('本地离线转写请选择列表内的FunASR模型')
            else:
                valid=(body.model.startswith('qwen3-asr-flash') if body.provider.startswith('qwen_') else
                       body.model=='whisper-1' or body.model.startswith(('gpt-4o-transcribe','gpt-4o-mini-transcribe')))
                if not valid or ('diarize' in body.model and not body.model.startswith('gpt-4o-transcribe-diarize')):
                    raise ModelConfigError('此模型不适用于已接入的转写接口，请选择列表内的转写模型或其快照版本')
        elif any(token in body.model.lower() for token in ('asr','whisper','transcribe','omni','gpt-audio')):
            raise ModelConfigError('文字分析请选择支持文本JSON输出的聊天模型')
        entered=body.api_key.get_secret_value() if body.api_key else ''
        if entered and body.clear_key:
            raise ModelConfigError('填写新密钥和清除密钥不能同时选择')
        if body.provider=='funasr_local':
            if entered:
                raise ModelConfigError('FunASR本地离线转写在本机运行，无需填写API Key')
            return {'use_default':False,'provider':body.provider,'base_url':provider['base_url'],
                    'model':body.model,'protocol':'funasr_local','key':''}
        # A blank field can reuse only this account's key for the same destination.
        same=old['provider']==body.provider and old['base_url'].rstrip('/')==provider['base_url']
        key='' if body.clear_key else entered or (old['key'] if same and not old['use_default'] else '')
        if not key and not body.clear_key:
            raise ModelConfigError('请填写所选厂商的个人API Key；切换厂商或地区时不会复用旧密钥')
        return {'use_default':False,'provider':body.provider,'base_url':provider['base_url'],
                'model':body.model,'protocol':'chat' if kind=='text' else provider['audio_protocol'],'key':key}

    def update(self,actor,body):
        with self.lock:
            cipher=self.cipher()
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                old,revision=self.read_profiles(actor,db)
                if body.expected_revision!=revision:
                    raise Conflict('模型配置已被其他页面修改，请刷新后重新设置')
                profiles={k:self.merge_profile(k,getattr(body,k),old[k]) for k in old}
                stored={}
                for kind,profile in profiles.items():
                    stored[kind]={'use_default':True} if profile['use_default'] else {
                        **{k:v for k,v in profile.items() if k!='key'},
                        'encrypted_key':cipher.encrypt(profile['key'].encode()).decode() if profile['key'] else ''}
                db.execute('''INSERT INTO model_preferences(user_id,text_config,audio_config,revision,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET text_config=excluded.text_config,
                    audio_config=excluded.audio_config,revision=excluded.revision,updated_at=excluded.updated_at''',
                    (actor,dumps(stored['text']),dumps(stored['audio']),revision+1,now()))
                event(db,actor,'personal_models_updated','更新本账号模型配置',
                      after={k:self.public_profile(v) for k,v in profiles.items()})
        return self.public(actor)

    def apply(self,profiles):
        text,audio=profiles['text'],profiles['audio']
        return replace(self.defaults,api_key=text['key'],base_url=text['base_url'],model=text['model'],
                       asr_key=audio['key'],asr_base_url=audio['base_url'],asr_model=audio['model'],
                       asr_protocol=audio['protocol'],text_provider=text['provider'],audio_provider=audio['provider'])

    def resolve(self,actor):
        with self.lock,self.store.connection() as db:
            profiles,_=self.read_profiles(actor,db)
            return self.apply(profiles)

    def test(self,actor,body):
        with self.lock,self.store.connection() as db:
            profiles,revision=self.read_profiles(actor,db)
            if revision!=body.expected_revision:
                raise Conflict('模型配置已变化，请刷新后测试')
            profiles[body.kind]=self.merge_profile(body.kind,body.profile,profiles[body.kind])
            if not profiles[body.kind]['key'] and profiles[body.kind]['provider']!='funasr_local':
                raise ModelConfigError('请先填写API Key')
            stamp=time.monotonic()
            if actor in self.testing or stamp-self.last_test.get(actor,-100)<10:
                raise Conflict('连接测试较频繁，请等待10秒后重试')
            self.testing.add(actor);self.last_test[actor]=stamp
            settings=self.apply(profiles)
        started=time.monotonic()
        try:
            if body.kind=='audio':
                from .audio import transcribe,AudioError
                _,info=transcribe((ROOT/'data/model-probe.wav').read_bytes(),'audio/wav',settings)
            else:
                from .model_transport import check_text_connection
                info=check_text_connection(settings)
            return {'ok':True,'kind':body.kind,'model':profiles[body.kind]['model'],
                    'provider':profiles[body.kind]['provider'],'elapsed_ms':round((time.monotonic()-started)*1000),
                    'tokens':info.get('tokens',0),'saved':False}
        except Exception as exc:
            from .audio import AudioError
            from .model_transport import ModelTransportError
            if isinstance(exc,(AudioError,ModelTransportError)):
                raise ModelConfigError(str(exc)) from None
            raise ModelConfigError('模型连接测试失败，请检查所选模型和密钥') from None
        finally:
            with self.lock:self.testing.discard(actor)
