def provision(book, created, request_id, create):
    book.reserve(request_id)
    try: return create(request_id)
    except BaseException:
        book.release(request_id)
        raise
