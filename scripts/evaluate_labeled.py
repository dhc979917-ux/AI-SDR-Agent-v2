"""Labeled-set evaluation: intent confusion matrix, entity resolution, DNC recall and field coverage.

Methodology for authorized real-call evaluation (docs/真实数据评测.md). The bundled
example dataset is synthetic and only demonstrates the schema and metrics; replace it
with authorized labeled calls before drawing conclusions. Reports measure pipeline
behavior on the labeled set, not business-accuracy guarantees.
"""
from pathlib import Path
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


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['rules', 'llm'], default='rules')
    parser.add_argument('--dataset', default='eval/labeled_cases.json')
    parser.add_argument('--output', default=None)
    args = parser.parse_args(argv)
    path = ROOT / args.dataset
    if not path.exists():
        parser.error(f'标注集 {args.dataset} 不存在；从 eval/labeled_cases.example.json 复制开始')
    cases = json.loads(path.read_text(encoding='utf-8'))
    settings = Settings(db_path=Path(tempfile.mkdtemp(prefix='sdr-labeled-'))/'eval.sqlite', entity_provider='off')
    if args.mode == 'llm' and not settings.api_key:
        parser.error('LLM key is required.')
    store = Store(settings.db_path)
    engine = Engine(store, settings)
    rows = []
    for case in cases:
        expected = case.get('expected') or {}
        req = AnalyzeRequest(transcript=case['transcript'], title=case.get('title', case['id']),
                             data_mode=case.get('data_mode', 'synthetic'), mode=args.mode,
                             idempotency_key='labeled-'+case['id']).model_dump()
        record, _ = store.create(req, case['id'], 'evaluation')
        record = engine.run(record['id'])
        result = record['result']
        actual_level = result['intent']['level']
        actual_dnc = bool(result['extraction']['do_not_contact'])
        checks = {'processing': record['status'] == 'completed'}
        if 'intent_level' in expected:
            checks['intent_level'] = actual_level == expected['intent_level']
        if 'do_not_contact' in expected:
            checks['do_not_contact'] = actual_dnc == bool(expected['do_not_contact'])
        entity = result.get('entity') or {}
        entity_row = {'status': entity.get('status'),
                      'selected': (entity.get('selected') or {}).get('name'),
                      'candidate_count': len(entity.get('candidates') or [])}
        if expected.get('entity_name'):
            names = [item.get('name') for item in entity.get('candidates') or []]
            top1 = entity_row['selected'] or (names[0] if names else None)
            entity_row.update(top1=top1, hit1=top1 == expected['entity_name'],
                              hit3=expected['entity_name'] in names[:3],
                              recall=expected['entity_name'] in names)
            checks['entity_recall'] = entity_row['recall']
        field_rows = {}
        for name, want in (expected.get('fields') or {}).items():
            got = (result['extraction']['fields'].get(name) or {}).get('value')
            field_rows[name] = {'expected': want, 'actual': got, 'match': got == want}
            checks['field:' + name] = got == want
        rows.append({'id': case['id'], 'title': case.get('title', case['id']),
                     'data_mode': case.get('data_mode', 'synthetic'),
                     'expected': expected,
                     'actual': {'intent_level': actual_level, 'do_not_contact': actual_dnc,
                                'call_type': result['extraction']['call_type']},
                     'entity': entity_row, 'fields': field_rows,
                     'scores': result['intent']['scores'], 'checks': checks,
                     'passed': all(checks.values()), 'elapsed_ms': result['elapsed_ms']})
    engine.pool.shutdown(wait=True)
    graded = [r for r in rows if 'intent_level' in r['expected']]
    class_metrics = {}
    f1_values = []
    for label in ('high', 'medium', 'low'):
        tp = sum(r['expected']['intent_level'] == label and r['actual']['intent_level'] == label for r in graded)
        fp = sum(r['expected']['intent_level'] != label and r['actual']['intent_level'] == label for r in graded)
        fn = sum(r['expected']['intent_level'] == label and r['actual']['intent_level'] != label for r in graded)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
        if f1 is not None:
            f1_values.append(f1)
        class_metrics[label] = {'tp': tp, 'fp': fp, 'fn': fn, 'precision': precision, 'recall': recall, 'f1': f1}
    dnc_graded = [r for r in rows if 'do_not_contact' in r['expected']]
    dnc_missed = [r for r in dnc_graded if r['expected']['do_not_contact'] and not r['actual']['do_not_contact']]
    entity_graded = [r for r in rows if r['expected'].get('entity_name')]
    field_checks = sum(len(r['fields']) for r in rows)
    field_matched = sum(1 for r in rows for f in r['fields'].values() if f['match'])
    report = {'kind': 'labeled_set_evaluation', 'mode': args.mode, 'created_at': now(),
              'dataset_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'count': len(rows), 'passed': sum(r['passed'] for r in rows),
              'intent_graded_count': len(graded), 'intent_class_metrics': class_metrics,
              'intent_macro_f1': sum(f1_values) / len(f1_values) if f1_values else None,
              'intent_abstained_count': sum(r['actual']['intent_level'] is None for r in graded),
              'dnc_graded_count': len(dnc_graded),
              'dnc_recall': round(1 - len(dnc_missed) / len(dnc_graded), 4) if dnc_graded else None,
              'dnc_missed_ids': [r['id'] for r in dnc_missed],
              'entity_graded_count': len(entity_graded),
              'entity_top1_accuracy': round(sum(r['entity']['hit1'] for r in entity_graded) / len(entity_graded), 4) if entity_graded else None,
              'entity_top3_recall': round(sum(r['entity']['hit3'] for r in entity_graded) / len(entity_graded), 4) if entity_graded else None,
              'entity_candidate_recall': round(sum(r['entity']['recall'] for r in entity_graded) / len(entity_graded), 4) if entity_graded else None,
              'entity_abstained_count': sum(r['entity']['status'] in ('ambiguous', 'unresolved', 'lookup_failed') for r in entity_graded),
              'field_check_count': field_checks,
              'field_coverage': round(field_matched / field_checks, 4) if field_checks else None,
              'rows': rows,
              'limitations': '标注集度量结果依赖标注口径与样本代表性；合成示例行仅演示指标，不代表真实业务准确率。'
                             '企业抬头指标需要配置企查查（SDR_ENTITY_PROVIDER=auto）后评测；entity_name 为空的用例不参与企业指标。'}
    output = ROOT / (args.output or f'eval/artifacts/labeled-{args.mode}.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {k: report[k] for k in ('mode', 'count', 'passed', 'intent_macro_f1', 'dnc_recall',
                                       'entity_top1_accuracy', 'entity_top3_recall', 'field_coverage')}
    print(json.dumps(summary, ensure_ascii=False))
    for row in rows:
        if not row['passed']:
            failed = [k for k, v in row['checks'].items() if not v]
            print('MISMATCH', row['id'], failed, 'expected', row['expected'], 'actual', row['actual'])
    return 0 if report['passed'] == report['count'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
