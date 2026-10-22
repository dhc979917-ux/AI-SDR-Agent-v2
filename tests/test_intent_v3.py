from copy import deepcopy
import json
import pytest
from sdr.scoring import assess, final_level
from sdr.models import Extraction, ReviewRequest
from sdr.review import revise, ReviewError
from sdr.engine import recalculate
from sdr.entities import resolve
from sdr.queue import review_display

@pytest.mark.parametrize('values,level', [((0,0,0,0),None),((0,0,0,5),None),((0,0,0,6),'low'),((15,10,0,0),'low'),((15,10,0,5),'medium'),((35,20,0,5),'high'),((25,0,15,20),'high'),((15,25,20,0),'medium'),((35,10,20,0),'medium')])
def test_new_thresholds(values,level):
    assert final_level(values)==level

def test_unknown_zero_keeps_semantics():
    result=assess(Extraction(call_type='sales').model_dump())
    assert result['total']==0 and result['scores']==dict.fromkeys('NTBE',0)
    assert result['unknown_dimensions']==list('NTBE')
    assert result['requires_manual_intent'] and result['status']=='abnormal'

@pytest.mark.parametrize('score', [0, 5])
def test_zero_requires_individual_review(system,settings,score):
    _,engine,analyze=system
    row=analyze('verified')
    row['result']['extraction']['signals']=Extraction().model_dump()['signals']
    if score == 5:
        row['result']['extraction']['signals']['E']['kind']='action_contact'
    recalculate(row['result'],row['request'],settings)
    assert 'INTENT_ABNORMAL' in row['result']['blockers']
    assert review_display(row)['state']=='exception'
    with pytest.raises(ReviewError):
        revise(row,ReviewRequest(expected_revision=row['revision'],action='save',reason='已核对客户原文',intent_level='medium'), 'A',settings)
    tid=next(t['id'] for t in row['result']['turns'] if t['speaker']=='customer')
    result,_=revise(row,ReviewRequest(expected_revision=row['revision'],action='save',reason='已核听客户录音并确认',intent_level='medium',recording_reviewed=True,evidence_turn_ids=[tid]),'A',settings)
    assert result['intent']['total']==score and result['intent']['level']=='medium'
    assert result['manual_intent_review']['actor']=='A'
    assert 'INTENT_ABNORMAL' not in result['blockers']
    assert result['review']['stage']=='secondary'

def test_multiple_candidates_always_require_selection(system,settings,tmp_path):
    _,_,analyze=system
    row=analyze('verified');r=row['result']
    from sdr.models import Turn
    selected=resolve(r['extraction']['fields'],[Turn.model_validate(t) for t in r['turns']],settings,'demo')['selected']
    assert selected is not None
    from sdr.entities import Entity
    first=Entity.model_validate({k:v for k,v in selected.items() if k in Entity.model_fields}).model_dump()
    second=deepcopy(first);second['id']='ANOTHER';second['verified_registry']=False
    catalog=tmp_path/'catalog.json';catalog.write_text(json.dumps([first,second]))
    settings.entity_catalog=catalog
    from sdr.models import Turn
    result=resolve(r['extraction']['fields'],[Turn.model_validate(t) for t in r['turns']],settings,'demo')
    assert len(result['candidates'])==2
    assert result['status']=='ambiguous' and result['selected'] is None
