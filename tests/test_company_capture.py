from sdr.models import ReviewRequest, Extraction
from sdr.review import ReviewService
from sdr.entities import captured_entity


def test_capture_skips_external_lookup(system, settings, monkeypatch):
    settings.entity_url='https://example.invalid/should-not-be-called'
    def forbidden(*args,**kwargs):
        raise AssertionError('Company lookup must not run')
    monkeypatch.setattr('sdr.entities.httpx.get',forbidden)
    store,_,analyze=system
    row=analyze('assignment')
    assert row['status']=='completed'
    assert row['result']['lead']['company_name']=='木里木外'
    assert row['result']['entity']['status']=='captured'
    assert 'ENTITY_UNRESOLVED' not in row['result']['blockers']


def test_missing_name_and_manual_fill_without_evidence_or_reason(system,settings):
    empty=captured_entity(Extraction().model_dump()['fields'],'real')
    assert empty['status']=='missing' and empty['selected'] is None
    store,_,analyze=system
    row=analyze()
    service=ReviewService(store,settings)
    cleared=service.apply(row['id'],ReviewRequest(expected_revision=1,action='save',company_name=None),'A')
    assert cleared['result']['lead']['company_name'] is None
    assert 'ENTITY_UNRESOLVED' in cleared['result']['blockers']
    filled=service.apply(row['id'],ReviewRequest(expected_revision=2,action='approve',company_name='人工填写的公司'),'A')
    assert filled['result']['entity']['status']=='manual'
    assert filled['result']['entity']['entered_by']=='A'
    assert filled['result']['lead']['company_name']=='人工填写的公司'
    assert filled['result']['delivery_status']=='ready'
    assert filled['original']==row['original']
