class Book:
    def __init__(self): self.held = set()
    def reserve(self, request_id):
        if request_id in self.held: raise ValueError('duplicate reservation')
        self.held.add(request_id)
    def release(self, request_id): self.held.remove(request_id)
