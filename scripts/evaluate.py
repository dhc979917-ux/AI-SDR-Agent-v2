"""Local regression evaluation; optional live LLM mode uses only synthetic cases."""
from pathlib import Path
from collections import Counter
import argparse
import hashlib
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdr.config import ROOT, Settings
from sdr.engine import Engine
from sdr.models import AnalyzeRequest
from sdr.store import Store, now


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['rules','llm'],default='rules')
    parser.add_argument('--dataset',default='eval/cases.json')
    parser.add_argument('--output',default=None)
    args=parser.parse_args()
    path=ROOT/args.dataset
    cases=json.loads(path.read_text())
    settings=Settings(db_path=Path(tempfile.mkdtemp(prefix='sdr-eval-'))/'eval.sqlite', entity_provider='off')
    if args.mode=='llm' and not settings.api_key:parser.error('LLM key is required.')
    store=Store(settings.db_path);engine=Engine(store,settings)
    rows=[]
    for c in cases:
        req=AnalyzeRequest(transcript=c['text'],title=c['id'],data_mode='synthetic',mode=args.mode,idempotency_key='eval-'+c['id']).model_dump()
        record,_=store.create(req,c['id'],'evaluation');record=engine.run(record['id']);r=record['result']
        actual=r['intent']['level']
        checks={'level':actual==c['expected'],'processing':record['status']=='completed'}
        if 'do_not_contact' in c:checks['contact_policy']=r['extraction']['do_not_contact']==c['do_not_contact']
        if 'designers' in c:checks['designer_scope']=r['extraction']['fields']['shejishi']['value']==c['designers']
        rows.append({'id':c['id'],'slice':c['slice'],'expected':c['expected'],'actual':actual,'checks':checks,
                     'passed':all(checks.values()),'scores':r['intent']['scores'],'elapsed_ms':r['elapsed_ms'],'issues':r['validation_issues']})
    engine.pool.shutdown(wait=True)
    labels=['high','medium','low'];scores={};f1=[]
    known=[r for r in rows if r['expected'] is not None]
    for label in labels:
        tp=sum(r['expected']==label and r['actual']==label for r in known)
        fp=sum(r['expected']!=label and r['actual']==label for r in known)
        fn=sum(r['expected']==label and r['actual']!=label for r in known)
        precision=tp/(tp+fp) if tp+fp else None;recall=tp/(tp+fn) if tp+fn else None
        value=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None
        if value is not None:f1.append(value)
        scores[label]={'tp':tp,'fp':fp,'fn':fn,'precision':precision,'recall':recall,'f1':value}
    report={'kind':'synthetic_regression_not_business_accuracy','mode':args.mode,'created_at':now(),
            'dataset_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'count':len(rows),'passed':sum(r['passed'] for r in rows),
            'graded_count':len(known),'class_metrics':scores,'macro_f1':sum(f1)/len(f1) if f1 else None,
            'abstention_count':sum(r['actual'] is None for r in rows),'results':rows,
            'limitations':'合成边界回归集，已用于工程调试，不是冻结盲测集或真实业务准确率。实际录音尚未取得。'}
    output=ROOT/(args.output or f'eval/artifacts/{args.mode}-regression.json');output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:report[k] for k in ('mode','count','passed','macro_f1','abstention_count')},ensure_ascii=False))
    for r in rows:
        if not r['passed']:print('FAILED',r['id'],'expected',r['expected'],'actual',r['actual'])
    return 0 if report['passed']==report['count'] else 1


if __name__=='__main__':raise SystemExit(main())
