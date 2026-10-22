"""Lossless turn segmentation and conservative business-role suggestions."""
import hashlib
import json
import re
import time
from typing import Literal

import httpx
from pydantic import Field

from .config import ROOT
from .models import Model,MAX_TRANSCRIPT_CHARS
from .model_transport import json_chat_body,provider_error

CHUNK_CHARS=3500
LABELS={'sales':'销售','customer':'客户','unknown':'待确认','other':'其他'}


class RoleError(Exception):
    pass


class RoleTurn(Model):
    role: Literal['sales','customer','unknown']
    text: str = Field(min_length=1,max_length=MAX_TRANSCRIPT_CHARS)
    certainty: Literal['clear','uncertain']
    note: str = Field(default='',max_length=200)


class RoleResponse(Model):
    turns: list[RoleTurn] = Field(min_length=1,max_length=1000)


class UnitResponse(Model):
    roles: dict[str,Literal['sales','customer','unknown']]
    notes: dict[str,str] = Field(default_factory=dict)
    splits: dict[str,list[RoleTurn]] = Field(default_factory=dict)


def automatic_other(text, source_start=0):
    value=re.sub(r'[\W_]+', '', text, flags=re.UNICODE)
    return bool(re.fullmatch(r'(?:[嗯啊哦噢呃唔喂哎诶欸唉]|对{2,})+', value)) or (source_start < 40 and value in ('家乐来电','酷家乐来电'))


def compact(text):
    return re.sub(r'\s+','',text)


def chunks(text,limit=CHUNK_CHARS):
    start=0
    while start<len(text):
        end=min(start+limit,len(text))
        if end<len(text):
            boundaries=[m.end() for m in re.finditer(r'[。！？!?；;\n]',text[start:end])]
            if boundaries and boundaries[-1]>=limit//2:end=start+boundaries[-1]
        yield start,end
        start=end


def speaker_spans(text,hints):
    """Ignore unusable hints rather than claiming acoustic evidence we lack."""
    if not hints:return []
    source=compact(text);cursor=0;spans=[]
    for hint in hints:
        part=compact(hint.get('text',''))
        if not part:continue
        if source[cursor:cursor+len(part)]!=part:return []
        spans.append({**hint,'begin':cursor,'finish':cursor+len(part)})
        cursor+=len(part)
    return spans if cursor==len(source) else []


def validate_turns(source,content,offset=0,hints=None):
    response=RoleResponse.model_validate_json(content)
    canonical=compact(source)
    if ''.join(compact(t.text) for t in response.turns)!=canonical:
        raise ValueError('原文覆盖不一致：不允许删句、重复或改写')
    # Reconstruct from original characters; even whitespace changes are not saved.
    positions=[i for i,c in enumerate(source) if not c.isspace()]
    cursor=0;raw_cursor=0;result=[]
    for item in response.turns:
        length=len(compact(item.text))
        if not length:raise ValueError('发言不能为空白')
        end=positions[cursor+length-1]+1
        if cursor+length==len(canonical):end=len(source)
        row=item.model_dump();row['text']=source[raw_cursor:end].strip()
        row.update(source_start=offset+raw_cursor,source_end=offset+end)
        overlap=[h for h in hints or [] if h['begin']<cursor+length and h['finish']>cursor]
        speakers={h['speaker'] for h in overlap}
        if len(speakers)>1:raise ValueError('一轮发言跨越不同声纹，必须分开')
        if overlap:
            row.update(speaker=overlap[0]['speaker'],start=overlap[0]['start'],end=overlap[-1]['end'])
        if row['certainty']=='uncertain' or row['role']=='unknown':
            row.update(role='unknown',certainty='uncertain',note=row['note'] or '角色依据不足，请回放核对')
        if automatic_other(row['text'], row['source_start']):
            row.update(role='other',certainty='clear',note='纯语气词或开场系统播报，自动归类')
        result.append(row);cursor+=length;raw_cursor=end
    return result


def source_units(source,hints=None):
    """Number original clauses, splitting at every supplied acoustic boundary."""
    cuts={0,len(source)}
    cuts.update(m.end() for m in re.finditer(r'[，,。！？!?；;：:\n]',source))
    positions=[i for i,c in enumerate(source) if not c.isspace()]
    for hint in hints or []:
        for point in (hint['begin'],hint['finish']):
            if 0<point<len(positions):cuts.add(positions[point-1]+1)
    units=[];start=0
    for end in sorted(cuts)[1:]:
        if not compact(source[start:end]):continue
        if units and not hints and not any(c.isalnum() for c in source[start:end]):
            units[-1]['text']+=source[start:end];start=end;continue
        units.append({'id':len(units)+1,'text':source[start:end]});start=end
    if units and start<len(source):units[-1]['text']+=source[start:]
    return units


def validate_unit_roles(source,content,units,offset=0,hints=None):
    response=UnitResponse.model_validate_json(content)
    expected={str(u['id']) for u in units}
    if set(response.roles)!=expected or not set(response.notes)<=expected or not set(response.splits)<=expected:
        raise ValueError('片段编号必须完整且不能虚构')
    turns=[]
    for unit in units:
        key=str(unit['id']);role=response.roles[key];parts=response.splits.get(key)
        if parts is not None:
            if len(parts)<2 or ''.join(compact(p.text) for p in parts)!=compact(unit['text']):
                raise ValueError('句内拆分不允许删改原文')
            turns.extend(p.model_dump() for p in parts)
        else:
            turns.append({'text':unit['text'],'role':role,'certainty':'uncertain' if role=='unknown' else 'clear',
                          'note':response.notes.get(key,'')})
    found=validate_turns(source,json.dumps({'turns':turns},ensure_ascii=False),offset,hints)
    merged=[]
    for turn in found:
        if (merged and merged[-1]['role']==turn['role']
            and (turn['role']!='unknown' or merged[-1]['note']==turn['note'])
            and merged[-1].get('speaker')==turn.get('speaker')):
            old=merged[-1];old['source_end']=turn['source_end']
            old['text']=source[old['source_start']-offset:old['source_end']-offset].strip()
            if 'end' in turn:old['end']=turn['end']
            if turn['note'] and turn['note']!=old['note']:old['note']=(old['note']+'；'+turn['note']).strip('；')[:200]
        else:merged.append(turn)
    return merged


def reconcile_units(first,review,units):
    """Disagreement is a review item, never silently accepted as customer fact."""
    a=UnitResponse.model_validate_json(first);b=UnitResponse.model_validate_json(review)
    def signature(value,key):
        if key in value.splits:return [(p.role,compact(p.text),p.certainty) for p in value.splits[key]]
        return value.roles[key]
    for unit in units:
        key=str(unit['id'])
        if signature(a,key)!=signature(b,key):
            b.roles[key]='unknown';b.splits.pop(key,None)
            b.notes[key]='两轮角色判断不一致，请结合前后句回放确认'
    return b.model_dump_json()


def classify_roles(text,settings,speaker_hints=None,on_progress=None):
    if not settings.api_key:raise RoleError('请先在「我的模型」配置文字分析模型，用于识别销售和客户角色')
    if not text.strip() or len(text)>MAX_TRANSCRIPT_CHARS:raise RoleError('没有可整理的有效转写文字')
    prompt=(ROOT/'prompts/audio_roles_v3.md').read_text()
    spans=speaker_spans(text,speaker_hints)
    parts=list(chunks(text));turns=[];tokens=0;started=time.monotonic()
    try:
        with httpx.Client(timeout=max(settings.timeout,90),trust_env=False,follow_redirects=False) as client:
            for index,(start,end) in enumerate(parts):
                target=text[start:end];base=len(compact(text[:start]));size=len(compact(target))
                local_hints=[{**h,'begin':max(0,h['begin']-base),'finish':min(size,h['finish']-base)}
                             for h in spans if h['begin']<base+size and h['finish']>base]
                context='\n'.join(f"{LABELS[t['role']]}：{t['text']}" for t in turns[-8:])[-1200:]
                units=source_units(target,local_hints)
                payload={'target_units':units,'previous_context':context,'next_context':text[end:end+400],
                         'speaker_hints':local_hints}
                messages=[{'role':'system','content':prompt},
                          {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
                outputs=[]
                for stage in range(2):
                    for attempt in range(2):
                        response=client.post(settings.base_url.rstrip('/')+'/chat/completions',
                            headers={'Authorization':'Bearer '+settings.api_key},
                            json=json_chat_body(settings.model,messages,8500))
                        if response.status_code!=200:raise RoleError(provider_error(response,'角色识别模型'))
                        try:
                            value=response.json()
                            tokens+=int((value.get('usage') or {}).get('total_tokens') or 0)
                            content=value['choices'][0]['message']['content']
                            validate_unit_roles(target,content,units,start,local_hints)
                            outputs.append(content)
                            break
                        except (ValueError,KeyError,IndexError,TypeError):
                            if attempt:raise RoleError(f'第{index+1}/{len(parts)}段角色结果未通过原文完整性检查，原文已保留，请重试') from None
                            messages.append({'role':'user','content':'上次结果未通过原文覆盖或格式校验。请返回roles对象，键为target_units中每一个id的字符串，从1到最后一个编号全部出现一次，不得遗漏或添加编号。splits内逐字保留原文。只返回规定JSON。'})
                    if stage==0:
                        messages=[*messages[:2],{'role':'assistant','content':content},
                            {'role':'user','content':REVIEW_INSTRUCTION}]
                found=validate_unit_roles(target,reconcile_units(*outputs,units),units,start,local_hints)
                turns.extend(found)
                if on_progress:on_progress(index+1,len(parts))
    except httpx.TimeoutException:
        raise RoleError('自动分角色超时，原始转写已保留，可重试') from None
    except httpx.HTTPError:
        raise RoleError('无法连接角色识别模型，原始转写已保留，可重试') from None
    if ''.join(compact(t['text']) for t in turns)!=compact(text):
        raise RoleError('角色整理未完整覆盖原文，已阻止使用，请重试')
    mapping={}
    for turn in turns:
        if turn.get('speaker') and turn['role'] in ('sales','customer'):mapping.setdefault(turn['speaker'],set()).add(turn['role'])
    for i,turn in enumerate(turns,1):
        turn['id']=f'R{i:03}'
        if turn['role']!='other' and len(mapping.get(turn.get('speaker'),set()))>1:
            turn.update(role='unknown',certainty='uncertain',note='同一声纹的角色判断前后冲突，请回放核对')
    draft='\n'.join(LABELS[t['role']]+'：'+re.sub(r'\s+', ' ',t['text']).strip() for t in turns)
    if len(draft)>MAX_TRANSCRIPT_CHARS:raise RoleError('添加角色后的文字过长，请拆分录音后处理')
    return {'turns':turns,'transcript':draft,'uncertain_count':sum(t['role']=='unknown' for t in turns),
            'method':'speaker_assisted' if spans else 'text_context','model':settings.model,
            'source_sha256':hashlib.sha256(text.encode()).hexdigest(),'prompt_version':'audio-roles-v3-reviewed',
            'prompt_hash':hashlib.sha256((prompt+REVIEW_INSTRUCTION).encode()).hexdigest()[:12],'tokens':tokens,
            'completed_chunks':len(parts),'total_chunks':len(parts),'elapsed_ms':round((time.monotonic()-started)*1000)}


REVIEW_INSTRUCTION='''请复核刚才的建议，不要直接照抄。再次逐句核对完整问答：
1. 主动问对方业务板块、过去用过哪些软件、希望多少人使用的是销售；“咱们这边”不是客户身份依据。一个完整问句不能因为逗号而随意切换角色。
2. 询问产品价格、账号数量限制、功能细节的是客户。销售长段说明中夹入的客户问话及答案应分开。
3. 同一人尚未说完的连贯句子不要随意拆给对方；反之，不能把明确的客户短回答合入销售介绍。
4. 孤立的嗯、好、对、问候、笑声，只有上下文而无音色依据时，不确定就标 unknown；只有标点的片段不能算独立的有效发言，应按相邻原句归属或 unknown。
5. 不修改任何原文，对不清楚的ASR片段保留 unknown 和 notes。若有声学说话人线索，按该边界复核。
返回完整的 roles/notes/splits JSON，包含每个编号。'''
