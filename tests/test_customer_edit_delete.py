from test_accounts import customer, analyzed

def test_customer_edit_delete_permissions_and_revisions(team):
    app,admin,alice,bob=team
    c=customer(alice,'原客户')
    path='/api/customers/'+c['id']
    body={'expected_revision':c['revision'],'name':'新客户','contact':'张先生','phone':'123','notes':'跟进中'}
    assert bob.patch(path,json=body).status_code==404
    assert bob.delete(path,params={'expected_revision':c['revision']}).status_code==404
    response=alice.patch(path,json=body)
    assert response.status_code==200,response.text
    after=response.json()
    assert after['name']=='新客户' and after['owner_id']=='alice'
    assert after['revision']==c['revision']+1
    assert alice.patch(path,json=body).status_code==409
    assert alice.delete(path,params={'expected_revision':c['revision']}).status_code==409
    assert any(e['action']=='customer_updated' for e in alice.get(path+'/events').json())
    assert alice.delete(path,params={'expected_revision':after['revision']}).status_code==200
    assert c['id'] not in [x['id'] for x in alice.get('/api/customers').json()]
    with app.state.store.connection() as db:
        assert db.execute("SELECT count(*) FROM workspace_events WHERE action='customer_deleted'").fetchone()[0]==1

def test_customer_linked_analysis_blocks_deletion_and_edit_keeps_history(team,samples):
    app,admin,alice,bob=team
    c=customer(alice)
    row=analyzed(app,'alice',c['id'],samples)
    path='/api/customers/'+c['id']
    assert admin.delete(path,params={'expected_revision':c['revision']}).status_code==409
    assert admin.patch(path,json={'expected_revision':c['revision'],'name':'更正名称'}).status_code==200
    assert app.state.store.get(row['id'])['result']==row['result']
    assert alice.patch(path,json={'expected_revision':c['revision']+1,'name':'  '}).status_code==409
