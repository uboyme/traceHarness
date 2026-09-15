def page(text, offset, count):
    if not 0 <= offset <= len(text) or count <= 0:
        raise ValueError('range')
    raw = text.encode('utf-8')
    end = min(offset + count, len(raw))
    return {'text': raw[offset:end].decode('utf-8', errors='ignore'),
            'next_offset': end if end < len(raw) else None}
