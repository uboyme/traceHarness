def read(store, ref, offset, count):
    return store[ref][offset:offset+count]
