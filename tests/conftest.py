from pathlib import Path
import json
import pytest

from sdr.config import ROOT, Settings
from sdr.engine import Engine
from sdr.models import AnalyzeRequest
from sdr.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "test.sqlite", api_key="", entity_url="", asr_key="", entity_provider="off")


@pytest.fixture
def samples():
    return {s["id"]: s for s in json.loads((ROOT / "data/samples.json").read_text())}


@pytest.fixture
def system(settings, samples):
    store = Store(settings.db_path)
    engine = Engine(store, settings)
    def analyze(sample_id="verified", **kwargs):
        s = samples[sample_id]
        request = AnalyzeRequest(transcript=s["transcript"], title=s["title"], mode="rules", data_mode=s["data_mode"],
                                 idempotency_key="test-" + sample_id + "-" + str(len(store.listing())), **kwargs).model_dump()
        record, _ = store.create(request, sample_id, "A")
        return engine.run(record["id"])
    yield store, engine, analyze
    engine.pool.shutdown(wait=True)


from contextlib import contextmanager
from fastapi.testclient import TestClient
from sdr.app import create_app
from sdr.models import UserCreate

TEST_PASSWORD = 'test-only-long-password-123'


def login(client, username='admin', password=TEST_PASSWORD):
    response = client.post('/api/auth/login', json={'username':username, 'password':password})
    assert response.status_code == 200, response.text
    client.headers['x-csrf-token'] = response.json()['csrf']
    return response


@contextmanager
def signed_client(settings, app=None):
    app = app or create_app(settings)
    app.state.auth.create_user(UserCreate(username='admin',name='管理员',password=TEST_PASSWORD,role='admin'))
    with TestClient(app) as client:
        login(client)
        yield client


@pytest.fixture
def team(settings,monkeypatch):
    # Existing workflow tests isolate this additional paid model call.
    # Role-specific tests install the real adapter or their own response fixture.
    monkeypatch.setattr('sdr.audio.classify_roles',lambda text,*a,**kw:{
        'transcript':'待确认：'+text.replace('\n',' '),'turns':[],
        'uncertain_count':1,'method':'text_context','model':'test-fixture'})
    app=create_app(settings)
    auth=app.state.auth
    auth.create_user(UserCreate(username='admin',name='管理员',password=TEST_PASSWORD,role='admin'))
    for uid in ('alice','bob'):
        auth.create_user(UserCreate(username=uid,name=uid.title(),password=TEST_PASSWORD), 'admin')
    with TestClient(app) as admin:
        login(admin)
        alice,bob=TestClient(app),TestClient(app)
        login(alice,'alice');login(bob,'bob')
        yield app,admin,alice,bob
        alice.close();bob.close()
