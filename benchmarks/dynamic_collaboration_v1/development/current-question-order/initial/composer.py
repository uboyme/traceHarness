def compose(history, current, reference):
    history.append({'role': 'user', 'content': current})
    history.append({'role': 'user', 'content': reference, 'kind': 'untrusted_reference'})
    return history
