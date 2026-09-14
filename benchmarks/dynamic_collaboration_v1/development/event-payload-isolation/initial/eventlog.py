class MemoryLog:
    def __init__(self):
        self.rows = []
    def append(self, payload):
        self.rows.append(dict(payload))
        return self.rows[-1]
    def read(self):
        return list(self.rows)
