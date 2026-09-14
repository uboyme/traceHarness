from hashing import digest
def receipt(name, report):
    return {'variant_id': name, 'report': report, 'sha256': digest(report)}
