import json
import sqlite3

import httpx
import pytest

from sdr.audio_identity import identify_customer, IdentityError, match_customers
from sdr.store import Store
from test_audio import wav, upload
from test_accounts import customer, transfer, analyzed


TEXT = '销售：您好，我是酷家乐的小李。\n客户：我叫陈晓，我们是晨光设计工作室，做全屋定制。'


def facts(confidence='clear'):
    return {'company': {'value':'晨光设计工作室','quote':'我们是晨光设计工作室，做全屋定制。'},
            'contact': {'value':'陈晓','quote':'我叫陈晓'}, 'phone':None,
            'customer_type':{'value':'全屋定制','quote':'做全屋定制。'},
            'confidence':confidence,'reason':'客户自报姓名和公司。'}


def prepare(team, monkeypatch, result=None):
    app,admin,alice,bob=team
    app.state.settings.asr_key='test-asr';app.state.settings.api_key='test-llm'
    monkeypatch.setattr(app.state.audio.pool,'submit',lambda *a:None)
    monkeypatch.setattr('sdr.audio.transcribe',lambda *a:(TEXT,{'model':'test-asr'}))
    monkeypatch.setattr('sdr.audio.identify_customer',lambda *a:result or facts())
    return app,admin,alice,bob


def ai_upload(client, key='ai-audio-task-key'):
    return client.post('/api/transcriptions',params={'customer_mode':'ai','filename':'identity.wav',
        'idempotency_key':key,'data_mode':'synthetic'},content=wav())


def link(client, row, **kwargs):
    return client.post(f'/api/transcriptions/{row["id"]}/customer',json={'expected_revision':row['revision'],**kwargs})


def test_ai_path_is_private_until_confirmed_and_follows_customer_transfer(team, monkeypatch):
    app,admin,alice,bob=prepare(team,monkeypatch)
    c=customer(alice,'晨光设计工作室');hidden=customer(bob,'晨光设计工作室')
    response=ai_upload(alice);assert response.status_code==202,response.text
    tid=response.json()['id'];assert response.json()['customer_id'] is None
    assert len(alice.get('/api/customers').json())==1  # no placeholder customer
    assert ai_upload(alice).json()['id']==tid
    assert bob.get('/api/transcriptions').json()==[]
    for suffix in ('','/audio'):
        assert bob.get('/api/transcriptions/'+tid+suffix).status_code==404
        assert admin.get('/api/transcriptions/'+tid+suffix).status_code==200
    app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='transcribed' and row['identity_status']=='ready'
    assert row['customer_id'] is None and row['raw_text']==TEXT
    assert [c['id'] for c in row['candidates']]==[c['id']]
    assert hidden['id'] not in json.dumps(row)
    assert len(admin.get('/api/transcriptions/'+tid).json()['candidates'])==2
    assert link(bob,row,customer_id=c['id']).status_code==404
    assert link(alice,row,customer_id=hidden['id']).status_code==404
    confirmation=alice.post(f'/api/transcriptions/{tid}/confirm',json={'expected_revision':row['revision'],'transcript':TEXT})
    assert confirmation.status_code==200,confirmation.text
    confirmed=confirmation.json()
    assert alice.post('/api/analyses',json={'transcription_id':tid,'transcript':TEXT,'mode':'rules',
        'data_mode':'synthetic','idempotency_key':'unlinked-audio-analysis'}).status_code==409
    assert link(alice,row,customer_id=c['id']).status_code==409  # stale revision
    bound=link(alice,confirmed,customer_id=c['id']);assert bound.status_code==200,bound.text
    assert bound.json()['customer_id']==c['id']
    assert link(alice,bound.json(),customer_id=hidden['id']).status_code==409  # immutable association
    assert any(e['action']=='audio_customer_confirmed' for e in alice.get('/api/customers/'+c['id']+'/events').json())
    assert transfer(admin,c,'bob').status_code==200
    assert alice.get('/api/transcriptions/'+tid+'/audio').status_code==404
    assert ai_upload(alice).status_code==404  # idempotency cannot reopen transferred audio
    assert bob.get('/api/transcriptions/'+tid).status_code==200


def test_candidates_rechecked_and_duplicate_names_never_auto_bind(team,monkeypatch):
    app,admin,alice,bob=prepare(team,monkeypatch)
    a=customer(alice,'晨光设计工作室');b=customer(alice,'晨光设计工作室')
    tid=ai_upload(alice).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['candidate_count']==2 and row['customer_id'] is None
    assert transfer(admin,a,'bob').status_code==200
    fresh=alice.get('/api/transcriptions/'+tid).json()
    assert [c['id'] for c in fresh['candidates']]==[b['id']]
    assert link(alice,row,customer_id=a['id']).status_code==404


def test_create_and_associate_is_atomic_and_cannot_assign_other_employee(team,monkeypatch):
    app,admin,alice,bob=prepare(team,monkeypatch)
    tid=ai_upload(alice).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    for body in ({},{'customer_id':'x','new_customer':{'name':'客户'}},{'new_customer':{'name':'  '}}):
        assert link(alice,row,**body).status_code==422
    assert link(alice,row,new_customer={'name':'新客户','owner_id':'bob'}).status_code==403
    assert alice.get('/api/customers').json()==[]
    bound=link(alice,row,new_customer={'name':'晨光设计工作室','contact':'陈晓'})
    assert bound.status_code==200,bound.text
    created=alice.get('/api/customers').json()
    assert len(created)==1 and created[0]['owner_id']=='alice'
    assert bound.json()['customer_id']==created[0]['id']
    assert link(alice,row,new_customer={'name':'重复客户'}).status_code==409
    assert len(alice.get('/api/customers').json())==1


def test_identity_failure_preserves_transcript_and_supports_retry_or_manual_choice(team,monkeypatch):
    app,admin,alice,bob=prepare(team,monkeypatch)
    def fail(*args):raise IdentityError('识别超时')
    monkeypatch.setattr('sdr.audio.identify_customer',fail)
    tid=ai_upload(alice).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['status']=='transcribed' and row['raw_text']==TEXT and row['identity_status']=='failed'
    path=f'/api/transcriptions/{tid}/identify'
    assert bob.post(path,json={'expected_revision':row['revision']}).status_code==404
    retry=alice.post(path,json={'expected_revision':row['revision']})
    assert retry.status_code==200 and retry.json()['identity_status']=='queued'
    assert alice.delete('/api/transcriptions/'+tid).status_code==409
    assert link(alice,retry.json(),new_customer={'name':'客户'}).status_code==409
    assert alice.post(path,json={'expected_revision':retry.json()['revision']}).status_code==409
    app.state.audio.recover()
    interrupted=alice.get('/api/transcriptions/'+tid).json()
    assert interrupted['identity_status']=='interrupted' and interrupted['status']=='transcribed'
    assert link(alice,interrupted,new_customer={'name':'人工核对客户'}).status_code==200


def test_saved_edits_invalidate_suggestions_and_retry_uses_corrected_text(team,monkeypatch):
    app,admin,alice,bob=prepare(team,monkeypatch)
    tid=ai_upload(alice).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    text=TEXT.replace('晨光','晨广')
    edited=alice.post(f'/api/transcriptions/{tid}/confirm',json={'expected_revision':row['revision'],'transcript':text})
    assert edited.status_code==200
    assert edited.json()['identity_status']=='outdated' and edited.json()['identity_result']=={}
    seen=[]
    monkeypatch.setattr('sdr.audio.identify_customer',lambda text,*args:seen.append(text) or facts())
    assert alice.post(f'/api/transcriptions/{tid}/identify',json={'expected_revision':edited.json()['revision']}).status_code==200
    app.state.audio.run_identity(tid)
    assert seen==[text]
    assert alice.get('/api/transcriptions/'+tid).json()['raw_text']==TEXT


def test_upload_modes_and_configuration_are_validated_before_save(team,monkeypatch):
    app,admin,alice,bob=team
    app.state.settings.asr_key='test-asr'
    assert ai_upload(alice).status_code==422
    app.state.settings.api_key='test-llm'
    assert upload(alice,'').status_code==422
    response=alice.post('/api/transcriptions',params={'customer_mode':'ai','customer_id':'anything',
        'filename':'test.wav','idempotency_key':'conflicting-modes'},content=wav())
    assert response.status_code==422
    assert alice.get('/api/transcriptions').json()==[]


@pytest.mark.parametrize('confidence', ['uncertain','unknown'])
def test_uncertain_identity_does_not_recommend_or_auto_associate(team,monkeypatch,confidence):
    app,admin,alice,bob=prepare(team,monkeypatch,facts(confidence))
    customer(alice,'晨光设计工作室')
    tid=ai_upload(alice).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    assert row['customer_id'] is None and row['candidates']==[]


def test_matching_uses_exact_phone_or_name_hints_and_no_mask_completion(team):
    app,admin,alice,bob=team
    a=alice.post('/api/customers',json={'name':'其他公司','phone':'13800000001','contact':'陈晓'}).json()
    result=facts();result['company']=None;result['contact']=None
    result['phone']={'value':'+86 138-0000-0001','quote':'我的电话是+86 138-0000-0001'}
    with app.state.store.connection() as db:
        assert match_customers(db,'alice',result)['candidates'][0]['id']==a['id']
        result['phone']['value']='138****0001'
        assert match_customers(db,'alice',result)['candidates']==[]


def test_identity_adapter_contract_validates_evidence_and_hides_upstream_errors(settings,monkeypatch):
    settings.api_key='identity-test-key';responses=[facts()]
    real_client=httpx.Client
    def handler(request):
        body=json.loads(request.content)
        assert body['response_format']=={'type':'json_object'}
        assert body['enable_thinking'] is False
        sent=json.loads(body['messages'][1]['content'])
        assert set(sent)=={'transcript','schema'} and sent['transcript']==TEXT
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(responses[0],ensure_ascii=False)}}], 'usage':{'total_tokens':77}})
    monkeypatch.setattr('sdr.audio_identity.httpx.Client',lambda **kw:real_client(transport=httpx.MockTransport(handler),**kw))
    result=identify_customer(TEXT,settings)
    assert result['tokens']==77 and len(result['source_sha256'])==64
    responses[0]={**facts(),'company':{'value':'幻觉企业','quote':'我们是晨光设计工作室'}}
    with pytest.raises(IdentityError,match='原文依据'):identify_customer(TEXT,settings)
    responses[0]={**facts(),'customer_id':'hallucinated-id'}
    with pytest.raises(IdentityError,match='格式异常'):identify_customer(TEXT,settings)
    def timeout(request):raise httpx.ReadTimeout('secret-upstream')
    monkeypatch.setattr('sdr.audio_identity.httpx.Client',lambda **kw:real_client(transport=httpx.MockTransport(timeout),**kw))
    with pytest.raises(IdentityError,match='超时') as e:identify_customer(TEXT,settings)
    assert 'secret' not in str(e.value)


def test_legacy_recordings_migrate_without_losing_analysis_references(team,monkeypatch,samples,tmp_path):
    app,admin,alice,bob=prepare(team,monkeypatch)
    c=customer(alice);tid=upload(alice,c['id']).json()['id'];app.state.audio.run(tid)
    row=alice.get('/api/transcriptions/'+tid).json()
    alice.post(f'/api/transcriptions/{tid}/confirm',json={'expected_revision':row['revision'],'transcript':TEXT})
    with app.state.store.connection() as db:
        existing=analyzed(app,'alice',c['id'],samples)
        db.execute('UPDATE analyses SET transcription_id=? WHERE id=?',(tid,existing['id']))
    copy=tmp_path/'legacy.sqlite'
    with sqlite3.connect(copy) as db, app.state.store.connection() as src:
        src.backup(db)
        sql=db.execute("SELECT sql FROM sqlite_master WHERE name='transcriptions'").fetchone()[0]
        # Recreate the previous release's table contract on an isolated database.
        db.execute('PRAGMA foreign_keys=OFF')
        for col in ('customer_mode','identity_status','identity_result','identity_error'):
            db.execute('ALTER TABLE transcriptions DROP COLUMN '+col)
        sql=db.execute("SELECT sql FROM sqlite_master WHERE name='transcriptions'").fetchone()[0]
        sql=sql.replace('CREATE TABLE transcriptions','CREATE TABLE legacy_audio',1).replace('customer_id TEXT REFERENCES','customer_id TEXT NOT NULL REFERENCES',1)
        db.execute(sql);db.execute('INSERT INTO legacy_audio SELECT * FROM transcriptions')
        db.execute('DROP TABLE transcriptions');db.execute('ALTER TABLE legacy_audio RENAME TO transcriptions')
    migrated=Store(copy)
    with migrated.connection() as db:
        audio=db.execute('SELECT * FROM transcriptions WHERE id=?',(tid,)).fetchone()
        assert audio['raw_text']==TEXT and audio['customer_id']==c['id'] and audio['customer_mode']=='existing'
        assert db.execute('SELECT transcription_id FROM analyses WHERE id=?',(existing['id'],)).fetchone()[0]==tid
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    Store(copy)  # repeat startup remains safe
