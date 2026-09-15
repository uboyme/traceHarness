from hashing import digest
def compare(planned, receipts):
    return [(name, receipt['report']) for name, receipt in zip(planned, receipts)]
