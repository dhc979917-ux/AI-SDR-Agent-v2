"""Shared JSON-chat request options and a minimal real connectivity probe."""
import json
import httpx


class ModelTransportError(Exception):
    pass


def provider_error(response,label):
    """Translate known codes without exposing provider messages or request data."""
    try:
        error=response.json().get('error') or {}
        code=str(error.get('code') or '') if isinstance(error,dict) else ''
    except (ValueError,AttributeError,TypeError):
        code=''
    if code.lower() in ('arrearage','insufficient_quota','balance_not_enough') or response.status_code==402:
        return f'{label}账号余额不足、欠费或额度不可用，请检查厂商账务状态，或在「我的模型」更换可用API Key'
    if response.status_code==401:
        return f'{label}密钥验证失败（HTTP 401），请检查API Key及对应厂商地区'
    if response.status_code==403:
        return f'{label}访问被拒绝（HTTP 403），请检查账号状态及模型权限'
    if response.status_code==429:
        return f'{label}触发调用频率或额度限制（HTTP 429），请稍后重试或检查厂商额度'
    return f'{label}返回HTTP {response.status_code}，请检查模型、密钥、额度及厂商地区'


def json_chat_body(model,messages,max_tokens):
    body={'model':model,'messages':messages,'temperature':0,'max_tokens':max_tokens,
          'response_format':{'type':'json_object'}}
    if 'qwen' in model.lower():body['enable_thinking']=False
    if model.startswith(('deepseek-v4','deepseek-flash')):body['thinking']={'type':'disabled'}
    return body


def check_text_connection(settings):
    body=json_chat_body(settings.model,[{'role':'user','content':'这是连接测试。请只返回这个JSON对象：{"status":"ok"}'}],64)
    try:
        with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:
            response=client.post(settings.base_url.rstrip('/')+'/chat/completions',
                                 headers={'Authorization':'Bearer '+settings.api_key},json=body)
        if response.status_code!=200:
            raise ModelTransportError(provider_error(response,'文字模型'))
        result=response.json()
        if json.loads(result['choices'][0]['message']['content'])!={'status':'ok'}:
            raise ValueError()
        return {'tokens':int((result.get('usage') or {}).get('total_tokens') or 0)}
    except httpx.TimeoutException:
        raise ModelTransportError('文字模型连接测试超时，请稍后重试') from None
    except httpx.HTTPError:
        raise ModelTransportError('无法连接文字模型，请检查网络和厂商地区') from None
    except (KeyError,ValueError,TypeError,IndexError):
        raise ModelTransportError('模型已响应，但未通过JSON输出检查，请选择支持JSON的文字模型') from None
