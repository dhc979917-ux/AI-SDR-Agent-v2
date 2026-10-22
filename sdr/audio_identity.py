"""Evidence-backed identity hints; CRM matching stays local and permission scoped."""
import hashlib
import json
import re
import unicodedata

import httpx
from pydantic import Field
from rapidfuzz.fuzz import ratio
from typing import Literal

from .config import ROOT
from .models import Model
from .workspace import active_user
from .model_transport import json_chat_body,provider_error


class IdentityError(Exception):
    pass


class IdentityFact(Model):
    value: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=1200)


class IdentityResult(Model):
    company: IdentityFact | None = None
    contact: IdentityFact | None = None
    phone: IdentityFact | None = None
    customer_type: IdentityFact | None = None
    confidence: Literal['clear', 'uncertain', 'unknown']
    reason: str = Field(min_length=1, max_length=500)


def identify_customer(text, settings):
    if not settings.api_key:
        raise IdentityError('未配置客户识别模型，可直接选择已有客户或人工建档')
    prompt = (ROOT / 'prompts/audio_identity_v1.md').read_text()
    body = json_chat_body(settings.model,[
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': json.dumps({'transcript': text,
                    'schema': IdentityResult.model_json_schema()}, ensure_ascii=False)}],2200)
    try:
        with httpx.Client(timeout=settings.timeout, trust_env=False) as client:
            response = client.post(settings.base_url.rstrip('/')+'/chat/completions', json=body,
                                   headers={'Authorization': 'Bearer '+settings.api_key})
        if response.status_code != 200:
            raise IdentityError(provider_error(response,'客户识别模型'))
        payload = response.json()
        result = IdentityResult.model_validate_json(payload['choices'][0]['message']['content'])
        for name in ('company', 'contact', 'phone', 'customer_type'):
            fact = getattr(result, name)
            if fact and (not fact.value.strip() or fact.value not in fact.quote or fact.quote not in text):
                raise IdentityError('客户身份结果缺少可核对的原文依据，请重试或手动选择客户')
        return {**result.model_dump(), 'model': settings.model, 'prompt_version': 'audio-identity-v1',
                'prompt_hash': hashlib.sha256(prompt.encode()).hexdigest()[:12],
                'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'tokens': int((payload.get('usage') or {}).get('total_tokens') or 0)}
    except httpx.TimeoutException:
        raise IdentityError('客户身份识别超时，转写已保留，可重试或手动选择客户') from None
    except httpx.HTTPError:
        raise IdentityError('无法连接客户识别服务，转写已保留，可手动选择客户') from None
    except (ValueError, KeyError, TypeError, IndexError):
        raise IdentityError('客户识别结果格式异常，请重试或手动选择客户') from None


def normalized(value):
    return re.sub(r'[\s·,，。()（）\-]', '', unicodedata.normalize('NFKC', value)).casefold()


def phone_key(value):
    value = re.sub(r'[\s()（）\-]', '', value)
    if value.startswith('+86'):
        value = value[3:]
    return value if re.fullmatch(r'\d{7,15}', value) else ''


def match_customers(db, actor, result):
    """Recompute on every read: transferred candidates must disappear immediately."""
    user = active_user(db, actor)
    if result.get('confidence') != 'clear':
        return {'candidates': [], 'candidate_count': 0}
    values = {key: (result.get(key) or {}).get('value', '') for key in ('company', 'contact', 'phone')}
    where, args = ('', ()) if user['role'] == 'admin' else ('WHERE owner_id=?', (actor,))
    candidates = []
    for row in db.execute('SELECT * FROM customers '+where, args):
        reasons, rank = [], 0
        company, name = normalized(values['company']), normalized(row['name'])
        if phone_key(values['phone']) and phone_key(values['phone']) == phone_key(row['phone']):
            reasons.append('电话一致'); rank += 100
        if company and company == name:
            reasons.append('客户名称一致'); rank += 60
        elif min(len(company), len(name)) >= 4 and ratio(company, name) >= 88:
            reasons.append('客户名称相近，需核对'); rank += 15
        if values['contact'] and normalized(values['contact']) == normalized(row['contact']):
            reasons.append('联系人同名，需核对'); rank += 20
        if reasons:
            candidates.append({'id': row['id'], 'name': row['name'], 'contact': row['contact'],
                               'phone': row['phone'], 'revision': row['revision'],
                               'reasons': reasons, '_rank': rank})
    candidates.sort(key=lambda c: (-c['_rank'], c['name'], c['id']))
    return {'candidates': [{k: v for k, v in c.items() if k != '_rank'} for c in candidates[:10]],
            'candidate_count': len(candidates)}
