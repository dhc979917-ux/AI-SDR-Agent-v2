import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'evaluate_labeled.py'


def load_script():
    spec = importlib.util.spec_from_file_location('evaluate_labeled', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_dataset_passes_and_reports_metrics(tmp_path):
    module = load_script()
    output = tmp_path / 'report.json'
    assert module.main(['--dataset', 'eval/labeled_cases.example.json', '--output', str(output)]) == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['kind'] == 'labeled_set_evaluation' and report['mode'] == 'rules'
    assert report['count'] == 4 and report['passed'] == 4
    assert report['intent_macro_f1'] == 1.0 and report['dnc_recall'] == 1.0
    assert report['entity_graded_count'] == 0 and report['field_coverage'] == 1.0
    assert all(row['passed'] for row in report['rows'])
    assert report['rows'][0]['fields']['shejishi']['expected'] is None
    assert report['rows'][0]['fields']['shejishi']['match'] is True
    assert 'dataset_sha256' in report and report['limitations']


def test_missing_dataset_is_actionable(capsys):
    module = load_script()
    with pytest.raises(SystemExit) as exc:
        module.main(['--dataset', 'eval/labeled-missing.json'])
    assert exc.value.code == 2
    assert 'labeled_cases.example.json' in capsys.readouterr().err
