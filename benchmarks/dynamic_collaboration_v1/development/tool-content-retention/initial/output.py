from codec import digest
def present(text, limit, store):
    if len(text) <= limit: return {'text': text}
    ref = digest(text)
    store[ref] = text[:limit]
    return {'preview': text[:limit], 'ref': ref}
