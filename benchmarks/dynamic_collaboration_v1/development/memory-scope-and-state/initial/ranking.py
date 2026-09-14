def matches(row, query):
    return query.casefold() in row['text'].casefold()
