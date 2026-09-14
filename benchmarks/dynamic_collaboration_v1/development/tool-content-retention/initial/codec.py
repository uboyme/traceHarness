import hashlib
def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
