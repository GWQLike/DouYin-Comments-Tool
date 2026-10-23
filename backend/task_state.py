"""Shared task transitions; supervisors must preserve worker conclusions."""

ACTIVE = {'waiting_login', 'running', 'verifying'}
FINAL = {'completed', 'private', 'paused'}


def transition_fields(task, event, *, message=None, verify_only=False, **fields):
    status = task['status']
    if event == 'start':
        return {**fields, 'status': 'verifying' if verify_only else 'running',
                'error': task['error'] if verify_only else None}
    if event == 'request_stop':
        return {'stop_requested': True} if status in ACTIVE else {}
    if event in ('stop_settled', 'worker_exit'):
        if status not in ACTIVE:
            return {}
        return {'status': 'paused' if event == 'stop_settled' else 'partial',
                'error': task['error'] or message}
    if event in ('worker_pause', 'worker_failed'):
        if status not in ACTIVE | {'created'}:
            return {}
        return {'status': 'paused' if event == 'worker_pause' else 'partial',
                'error': task['error'] or message}
    if event not in ('finish', 'private', 'pause', 'fail', 'launch_failed', 'waiting', 'resume'):
        raise ValueError('Unknown task transition')
    if status in FINAL:
        return {}
    if event == 'resume' and status != 'waiting_login':
        return {}
    if verify_only:
        fields.pop('collection_complete', None)
        message = task['error'] or message
    if event == 'finish':
        status = 'private' if task['visibility'] == 'private' else 'completed' if task['collection_complete'] else 'partial'
        fields['verification_complete'] = not task['unknown_authors']
    else:
        status = {'private': 'private', 'pause': 'paused', 'fail': 'partial', 'launch_failed': 'failed',
                  'waiting': 'waiting_login', 'resume': 'verifying' if verify_only else 'running'}[event]
    return {**fields, 'status': status, 'error': message}
