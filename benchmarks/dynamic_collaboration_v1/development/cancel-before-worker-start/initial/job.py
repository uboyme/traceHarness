import asyncio
class Job:
    def __init__(self):
        self.events, self.task = [], None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
    def start(self):
        self.events.append('admitted')
        self.task = asyncio.create_task(self._run())
    async def _run(self):
        self.entered.set()
        try: await self.release.wait()
        finally: self.events.append('settled')
    async def cancel(self):
        self.task.cancel()
        try: await self.task
        except asyncio.CancelledError: pass
