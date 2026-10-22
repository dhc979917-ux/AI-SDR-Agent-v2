"""Explicit cloud smoke check using only built-in synthetic identity examples."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdr.audio_identity import identify_customer
from sdr.config import Settings, ROOT


def main():
    parser=argparse.ArgumentParser(description='向已配置的大模型发送内置合成转写，验证客户身份识别')
    parser.add_argument('--synthetic',action='store_true',required=True)
    parser.parse_args()
    cases=[
        ('明确客户自述','您好，我是酷家乐的小李，方便介绍一下您吗？你好，我叫陈晓，我们是晨光设计工作室，主要做全屋定制。好的，收到。','晨光设计工作室'),
        ('仅销售身份','您好，我是酷家乐的小李。您好，我想了解一下软件。您方便介绍公司名称吗？暂时不方便说。',None),
        ('否认与第三方','您好，我是酷家乐的小李，请问您是晨光设计的陈晓吗？不是，你打错了，我也不认识陈晓。抱歉打扰了。',None),
    ]
    rows=[]
    for name,text,expected in cases:
        result=identify_customer(text,Settings())
        actual=(result.get('company') or {}).get('value')
        ok=actual==expected and (expected is not None or result.get('contact') is None)
        rows.append({'case':name,'transcript':text,'expected_company':expected,'passed':ok,'result':result})
        print(name+': '+('PASS' if ok else 'FAIL'))
    report={'data_mode':'synthetic','scope':'3 synthetic text cases; no real-customer audio or accuracy claim',
            'passed':sum(row['passed'] for row in rows),'total':len(rows),'cases':rows}
    (ROOT/'eval/artifacts/audio-identity-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if report['passed']!=len(rows):raise SystemExit(1)


if __name__=='__main__':main()
