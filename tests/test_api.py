import pytest
from fastapi.testclient import TestClient
from sdr.app import create_app
from sdr.models import AnalyzeRequest
from sdr.store import Conflict
from conftest import signed_client, login, TEST_PASSWORD
from sdr.models import UserCreate


def test_idempotent_creation_and_payload_conflict(system,samples):
    store,_,_=system
    req=AnalyzeRequest(transcript=samples["assignment"]["transcript"],idempotency_key="shared-key").model_dump()
    row,created=store.create(req,"one","A");same,repeated=store.create(req,"one","A")
    assert created and not repeated and same["id"]==row["id"]
    with pytest.raises(Conflict):store.create(req,"two","A")


def test_exports_are_protected_by_data_mode(system,settings):
    store,engine,analyze=system;row=analyze("verified")
    app=create_app(settings)
    with signed_client(settings, app) as client:
        approved=client.post('/api/reviews/'+row['id'],json={"expected_revision":1,"action":"approve","reason":"核对该演示完整线索"})
        assert approved.status_code==200
        assert client.get('/api/exports/'+row['id']+'?kind=qualified').status_code==409
        export=client.get('/api/exports/'+row['id']).json()
        assert export['_meta']['demo'] is True
        assert export['company_name']=='青禾空间设计（杭州）有限公司'


def test_auth_and_no_secret_exposure(settings):
    settings.api_key='private-model-secret';settings.asr_key='private-asr-secret'
    app=create_app(settings)
    app.state.auth.create_user(UserCreate(username='admin',name='管理员',password=TEST_PASSWORD,role='admin'))
    with TestClient(app) as client:
        assert client.get('/api/analyses').status_code==401
        assert client.get('/api/analyses',headers={'X-Demo-Actor':'B','Authorization':'Bearer anything'}).status_code==401
        login(client)
        response=client.get('/api/config')
        assert response.status_code==200
        assert 'private-model-secret' not in response.text and 'private-asr-secret' not in response.text
        assert TEST_PASSWORD not in response.text and response.json()['actor']=='admin'


def test_cross_origin_writes_rejected(settings):
    with signed_client(settings) as client:
        response=client.post('/api/analyses',headers={'Origin':'https://evil.invalid'},json={})
        assert response.status_code==403


def test_missing_model_key_does_not_silent_fallback(settings,samples):
    with signed_client(settings) as client:
        response=client.post('/api/analyses',json={"transcript":samples['assignment']['transcript'],"idempotency_key":"llm-no-key","mode":"llm"})
        assert response.status_code==422


def test_csv_neutralizes_formula_injection(system,settings):
    store,_,analyze=system;row=analyze()
    with store.connection() as db:
        req=row['request'];req['title']='=HYPERLINK("https://example.org")'
        import json
        db.execute('UPDATE analyses SET request=? WHERE id=?',(json.dumps(req),row['id']))
    with signed_client(settings) as client:
        response=client.get('/api/export.csv')
        assert "'=HYPERLINK" in response.text


def test_formal_export_rechecks_source_expiry(system,settings,samples,tmp_path):
    """An old approval must not keep an expired registry source exportable."""
    import json
    from sdr.models import ReviewRequest
    from sdr.review import ReviewService
    catalog=json.loads(settings.entity_catalog.read_text())
    for entity in catalog:
        entity.update(data_mode='real',source_url='https://registry.example.test/'+entity['id'])
    settings.entity_catalog=tmp_path/'authorized-catalog.json'
    settings.entity_catalog.write_text(json.dumps(catalog,ensure_ascii=False))
    store,engine,_=system
    req=AnalyzeRequest(transcript=samples['verified']['transcript'],data_mode='real',mode='rules',idempotency_key='real-export-test').model_dump()
    row,_=store.create(req,'verified-real','A');row=engine.run(row['id'])
    ReviewService(store,settings).apply(row['id'],ReviewRequest(expected_revision=1,action='approve',reason='测试可信主体与客户关系已核验'),'A')
    with signed_client(settings) as client:
        assert client.get('/api/exports/'+row['id']+'?kind=qualified').status_code==200
        record=store.get(row['id'])
        from sdr.entities import resolve
        from sdr.models import Turn
        record['result']['entity']=resolve(record['result']['extraction']['fields'],[Turn.model_validate(t) for t in record['result']['turns']],settings,'real')
        assert record['result']['entity']['status']=='verified'
        record['result']['entity']['selected']['valid_until']='2000-01-01'
        with store.connection() as db:
            db.execute('UPDATE analyses SET result=? WHERE id=?',(json.dumps(record['result']),row['id']))
        assert client.get('/api/exports/'+row['id']+'?kind=qualified').status_code==409
        assert client.get('/api/exports/'+row['id']).status_code==200
