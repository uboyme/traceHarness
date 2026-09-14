class Catalog:
    def __init__(self): self.entries = {}
    def bind(self, name, source, revision, content):
        self.entries[name] = (source, revision, content)
    def plan(self, name):
        source, revision, _ = self.entries[name]
        return name, source, revision
