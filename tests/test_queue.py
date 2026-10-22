from sdr.queue import review_display, priority_key


def row(id,status,stage='primary',intent='high',created='2026-01-01',run='completed'):
    source={'status':run,'result':{'review':{'status':status,'stage':stage}}}
    return {'id':id,'intent':intent,'created_at':created,'review_display':review_display(source)}


def test_actionable_reviews_precede_resolved_and_deferred_secondary_is_not_urgent():
    rows=[row('approved','approved','complete'),row('deferred','deferred','secondary'),row('pending-new','pending',created='2026-02-01'),
          row('pending-old','pending'),row('secondary','pending','secondary'),row('error','pending',run='failed'),
          row('rejected','rejected','secondary'),row('none','not_required'),row('inreview','in_review')]
    ordered=sorted(rows,key=priority_key)
    assert [r['id'] for r in ordered]==['error','secondary','inreview','pending-old','pending-new','deferred','approved','rejected','none']
    assert len({r['review_display']['color'] for r in ordered})==8
    assert priority_key(row('high','pending',intent='high'))<priority_key(row('medium','pending',intent='medium',created='2025-01-01'))
