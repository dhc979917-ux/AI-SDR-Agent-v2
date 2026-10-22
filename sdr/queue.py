"""One priority definition shared by queue API and all UI badges."""

STATES = {
    'exception': (0, '分析异常', 'red'),
    'secondary': (1, '待独立复核', 'purple'),
    'in_review': (2, '审核中', 'blue'),
    'pending': (3, '待审核', 'amber'),
    'deferred': (4, '待补证', 'orange'),
    'processing': (5, '分析中', 'cyan'),
    'approved': (6, '已通过', 'green'),
    'rejected': (7, '已排除', 'rose'),
    'cancelled': (8, '已停止', 'gray'),
    'not_required': (9, '无需审核', 'gray'),
}


def review_display(row):
    result = row.get('result') or {}
    review = result.get('review') or {}
    state = review.get('status', 'pending')
    if row['status'] == 'cancelled':
        state = 'cancelled'
    elif row['status'] in ('queued', 'running'):
        state = 'processing'
    elif row['status'] == 'interrupted' or row['status'] == 'failed' and state not in ('approved','rejected','deferred'):
        state = 'exception'
    elif 'INTENT_ABNORMAL' in result.get('blockers', []) and state not in ('rejected', 'deferred'):
        state = 'exception'
    elif review.get('stage') == 'secondary' and state in ('pending','in_review'):
        state = 'secondary'
    rank, label, color = STATES[state]
    return {'state':state, 'rank':rank, 'label':label, 'color':color}


def priority_key(summary):
    return (summary['review_display']['rank'], {'high':0,'medium':1,None:2,'low':3}[summary['intent']],
            summary['created_at'], summary['id'])
