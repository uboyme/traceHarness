def active(events):
    rows = {}
    for event in events:
        if event['kind'] == 'approved':
            rows[event['id']] = dict(event)
    return list(rows.values())
