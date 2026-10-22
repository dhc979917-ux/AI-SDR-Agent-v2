import time
from fastapi.testclient import TestClient
from sdr.auth import COOKIE
from sdr.models import AnalyzeRequest
from sdr.store import dumps
from conftest import login, TEST_PASSWORD


def customer(client,name='客户甲',owner=None):
    r=client.post('/api/customers',json={'name':name,'owner_id':owner})
    assert r.status_code==201,r.text
    return r.json()


def analyzed(app, actor, cid, samples, key='test-analysis', sample='verified'):
    req=AnalyzeRequest(transcript=samples[sample]['transcript'],title=key,mode='rules',data_mode='synthetic',
                       customer_id=cid,idempotency_key=key).model_dump()
    row,_=app.state.store.create(req,key,actor,authorize=True)
    return app.state.engine.run(row['id'])


def transfer(admin,c,owner):
    return admin.post('/api/admin/assignments',json={'items':[{'id':c['id'],'revision':c['revision']}],
                                                  'owner_id':owner,'reason':'区域调整转交客户'})


def test_employee_customer_isolation_across_every_read_and_write(team,samples):
    app,admin,alice,bob=team
    a=customer(alice,'Alice专属客户');b=customer(bob,'Bob专属客户')
    ra=analyzed(app,'alice',a['id'],samples,'alice-call')
    rb=analyzed(app,'bob',b['id'],samples,'bob-call')
    aid,bid=ra['id'],rb['id']
    assert [c['id'] for c in alice.get('/api/customers').json()]==[a['id']]
    assert [r['id'] for r in alice.get('/api/analyses').json()]==[aid]
    assert alice.get('/api/metrics').json()['total']==1
    assert 'bob-call' not in alice.get('/api/export.csv').text
    for path in (f'/api/analyses/{bid}',f'/api/exports/{bid}',f'/api/customers/{b["id"]}/events'):
        assert alice.get(path).status_code==404
    assert alice.post('/api/reviews/'+bid,json={'expected_revision':1,'action':'defer','reason':'尝试跨客户审核'}).status_code==404
    assert alice.post('/api/analyses',json={**rb['request'],'idempotency_key':'cross-customer'}).status_code==404
    assert alice.post('/api/customers',json={'name':'越权客户','owner_id':'bob'}).status_code==403
    assert len(admin.get('/api/analyses').json())==2
    with app.state.store.connection() as db:
        for row,actor in ((ra,'alice'),(rb,'bob')):
            db.execute('INSERT INTO feedback(id,analysis_id,kind,payload,actor,created_at) VALUES(?,?,?,?,?,?)',
                       ('f-'+actor,row['id'],'field_correction',dumps({'reason':actor}),actor,row['created_at']))
    assert [f['actor'] for f in alice.get('/api/feedback').json()]==['alice']
    assert alice.post('/api/feedback/f-alice/publish').status_code==403
    for path in ('/api/admin/users','/api/admin/events'):
        assert alice.get(path).status_code==403
    assert transfer(alice,a,'bob').status_code==403


def test_transfer_revokes_reads_and_stale_batch_atomically(team,samples):
    app,admin,alice,bob=team
    a=customer(alice);b=customer(alice,'第二位客户')
    ra=analyzed(app,'alice',a['id'],samples,'first-call')
    rb=analyzed(app,'alice',b['id'],samples,'second-call')
    payload={'items':[{'id':r['id'],'revision':1} for r in (rb,ra)],'action':'defer','reason':'等待补充共同证据'}
    preview=alice.post('/api/reviews/batch/preview',json=payload)
    assert preview.status_code==200
    assert transfer(admin,a,'bob').status_code==200
    assert alice.get('/api/analyses/'+ra['id']).status_code==404
    assert bob.get('/api/analyses/'+ra['id']).status_code==200
    commit=alice.post('/api/reviews/batch/commit',json={'preview_id':preview.json()['id']})
    assert commit.status_code==404
    assert app.state.store.get(rb['id'])['revision']==1
    assert alice.post('/api/reviews/batch/preview',json=payload).status_code==404
    # Replaying an old idempotency key cannot disclose a transferred record.
    assert alice.post('/api/analyses',json=ra['request']).status_code==404
    events=bob.get('/api/customers/'+a['id']+'/events').json()
    assert any(e['action']=='customer_assigned' and e['before_value']['owner_id']=='alice' for e in events)


def test_assignment_conflict_rolls_back_all_customers(team):
    app,admin,alice,bob=team
    a=customer(alice);b=customer(alice,'第二客户')
    assert transfer(admin,a,'bob').status_code==200
    response=admin.post('/api/admin/assignments',json={'items':[{'id':b['id'],'revision':1},{'id':a['id'],'revision':1}],
                                                    'owner_id':None,'reason':'批量回收到公共池'})
    assert response.status_code==409
    assert alice.get('/api/customers').json()[0]['id']==b['id']
    assert transfer(admin,b,None).status_code==200
    assert alice.get('/api/customers').json()==[]
    assert bob.get('/api/customers').json()[0]['id']==a['id']


def test_sessions_csrf_logout_password_reset_and_disable(team):
    app,admin,alice,bob=team
    with TestClient(app) as anonymous:
        for path in ('/api/config','/api/customers','/api/transcriptions','/api/export.csv','/api/metrics'):
            assert anonymous.get(path).status_code==401
    token=alice.cookies.get(COOKIE)
    response=alice.post('/api/customers',json={'name':'跨站'},headers={'Origin':'https://evil.example'})
    assert response.status_code==403
    assert alice.post('/api/customers',json={'name':'伪造'},headers={'X-CSRF-Token':'wrong'}).status_code==403
    assert admin.patch('/api/admin/users/admin',json={'active':False,'reason':'不能停用自己'}).status_code==409
    assert admin.patch('/api/admin/users/alice',json={'password':'new-password-for-alice','reason':'员工申请重置密码'}).status_code==200
    assert alice.get('/api/auth/me').status_code==401
    assert alice.post('/api/auth/login',json={'username':'alice','password':TEST_PASSWORD}).status_code==401
    login(alice,'alice','new-password-for-alice')
    cookie=alice.cookies.get(COOKIE)
    assert cookie!=token
    assert admin.patch('/api/admin/users/alice',json={'active':False,'reason':'员工离职停用账号'}).status_code==200
    assert alice.get('/api/customers').status_code==401
    assert alice.post('/api/auth/login',json={'username':'alice','password':'new-password-for-alice'}).status_code==401
    assert bob.post('/api/auth/logout').status_code==200
    assert bob.get('/api/auth/me').status_code==401
    with app.state.store.connection() as db:
        assert not any(TEST_PASSWORD in r['password_hash'] for r in db.execute('SELECT * FROM users'))
        assert not any(cookie==r['token_hash'] for r in db.execute('SELECT * FROM sessions'))


def test_password_change_invalidates_other_devices_and_expiration(team):
    app,admin,alice,bob=team
    other=TestClient(app);login(other,'alice')
    assert alice.post('/api/auth/password',json={'current_password':'incorrect','new_password':'strong-new-password'}).status_code==422
    assert alice.post('/api/auth/password',json={'current_password':TEST_PASSWORD,'new_password':'strong-new-password'}).status_code==200
    assert alice.get('/api/auth/me').status_code==401
    assert other.get('/api/auth/me').status_code==401
    with app.state.store.connection() as db:
        db.execute('UPDATE sessions SET expires_at=? WHERE user_id=?',(time.time()-1,'bob'))
    assert bob.get('/api/auth/me').status_code==401
    other.close()


def test_login_throttles_and_cookie_is_httponly(team):
    app,admin,alice,bob=team
    c=TestClient(app)
    for _ in range(5):
        assert c.post('/api/auth/login',json={'username':'missing','password':'bad'}).status_code==401
    assert c.post('/api/auth/login',json={'username':'missing','password':'bad'}).status_code==429
    response=login(c,'alice')
    cookie=response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert response.json()['user']['role']=='employee' and 'password_hash' not in response.text
    c.close()


def test_old_records_migrate_to_unassigned_pool_without_changing_evidence(system,settings):
    from sdr.store import Store
    store,engine,analyze=system
    old=analyze()
    with store.connection() as db:
        db.execute('UPDATE analyses SET customer_id=NULL WHERE id=?',(old['id'],))
    migrated=Store(settings.db_path).get(old['id'])
    again=Store(settings.db_path).get(old['id'])
    assert migrated['original']==old['original'] and migrated['revision']==old['revision']
    assert migrated['owner']==old['owner'] and migrated['customer_id']==again['customer_id']
    with store.connection() as db:
        assert db.execute('SELECT owner_id FROM customers WHERE id=?',(migrated['customer_id'],)).fetchone()[0] is None
