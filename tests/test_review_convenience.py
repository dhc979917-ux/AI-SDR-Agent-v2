import json
import pytest
from sdr.audio_roles import validate_turns, automatic_other
from sdr.models import BatchPreviewRequest, ReviewRequest
from sdr.review import ReviewService
from test_accounts import customer, analyzed

@pytest.mark.parametrize('text',['嗯','嗯嗯。','啊！','哦，哦。','酷家乐来电。','家乐来电','喂？','哎，哎。','诶','欸','唉','对对','对，对，对。','嗯，对对。'])
def test_auto_other(text):
    result=validate_turns(text,json.dumps({'turns':[{'role':'unknown','certainty':'uncertain','text':text}]}))
    assert result[0]['role']=='other' and result[0]['text']==text

@pytest.mark.parametrize('text',['嗯，我们下周试用。','好的','对','不是酷家乐来电','哦预算五万','喂，我们想试用','对对，预算五万','哎，下周联系','不对','对接生产'])
def test_business_words_kept(text):
    assert not automatic_other(text)

def test_announcement_only_at_start():
    assert not automatic_other('酷家乐来电',100)

def test_optional_reason_and_large_batch(system,settings):
    store,_,analyze=system
    rows=[analyze() for _ in range(51)]
    payload=BatchPreviewRequest(items=[{'id':r['id'],'revision':r['revision']} for r in rows],action='defer').model_dump()
    assert ReviewRequest(expected_revision=1,action='save').reason==''
    svc=ReviewService(store,settings)
    preview=svc.preview(payload,'A')
    assert preview['can_commit'] and len(preview['impact'])==51
    assert len(svc.commit(preview['id'],'A'))==51

def test_delete_permissions_revision_and_cleanup(team,samples):
    app,admin,alice,bob=team
    c=customer(alice)
    r=analyzed(app,'alice',c['id'],samples)
    path=f"/api/analyses/{r['id']}?expected_revision={r['revision']}"
    assert bob.delete(path).status_code==404
    assert alice.delete(path.replace('revision=1','revision=0')).status_code==409
    assert alice.delete(path).status_code==200
    assert alice.get('/api/analyses/'+r['id']).status_code==404
    assert any(x['id']==c['id'] for x in alice.get('/api/customers').json())
    with app.state.store.connection() as db:
        assert db.execute('SELECT count(*) FROM events WHERE analysis_id=?',(r['id'],)).fetchone()[0]==0
