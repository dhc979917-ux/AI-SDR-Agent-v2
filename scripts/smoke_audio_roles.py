"""Explicit paid smoke test: only the fixed synthetic conversation below is sent."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sdr.config import ROOT,Settings
from sdr.audio_roles import classify_roles,compact
from sdr.model_settings import ModelSettingsService
from sdr.store import Store

GOLD=[
    ('sales','您好，请问是陈经理吗？我是酷家乐的小李。'),
    ('customer','是的，你讲。'),
    ('sales','您之前留了试用信息，我想了解一下团队需求。'),
    ('customer','我们是杭州星河家居工作室，做全屋定制。'),
    ('customer','设计部有8个人，出图有点慢。'),
    ('sales','明白，您大概什么时候想开始试用？'),
    ('customer','最好下个月，预算一年3万元左右。'),
    ('sales','我们可以安排产品演示。'),
    ('sales','我加您微信，把试用资料发给您。'),
    ('customer','好的，您发过来，我看一下。'),
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--actor',help='Use this local account’s saved text configuration; otherwise use environment defaults.')
    args=parser.parse_args();settings=Settings()
    if args.actor:settings=ModelSettingsService(Store(settings.db_path),settings).resolve(args.actor)
    source=''.join(text for _,text in GOLD)
    result=classify_roles(source,settings)
    expected=[role for role,text in GOLD for _ in compact(text)]
    actual=[turn['role'] for turn in result['turns'] for _ in compact(turn['text'])]
    assert len(expected)==len(actual)
    correct=sum(a==b for a,b in zip(expected,actual))
    wrong=sum(a!=b and a!='unknown' for a,b in zip(actual,expected))
    report={'data_mode':'synthetic','model':result['model'],'source':source,'result':result,
            'matched_role_characters':correct,'total_characters':len(expected),
            'wrong_role_characters':wrong,'scope':'固定合成对话，不代表真实电话声纹分离准确率。'}
    target=ROOT/'eval/artifacts/audio-roles-live.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:report[k] for k in ('data_mode','model','matched_role_characters','total_characters','wrong_role_characters')},ensure_ascii=False))
    print(json.dumps({'turns':len(result['turns']),'uncertain':result['uncertain_count'],'elapsed_ms':result['elapsed_ms']},ensure_ascii=False))


if __name__=='__main__':main()
