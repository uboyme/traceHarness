class Queue:
    def __init__(self, ids): self.states = {key: 'pending' for key in ids}
    async def claim(self, key, checkpoint):
        if self.states.get(key) != 'pending': return False
        await checkpoint()
        self.states[key] = 'claimed'
        return True
