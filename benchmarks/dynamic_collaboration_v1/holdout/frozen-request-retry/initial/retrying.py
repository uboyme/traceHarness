class Transient(Exception): pass
async def run(build, send, attempts):
    for index in range(attempts):
        try: return await send(build())
        except Transient:
            if index + 1 == attempts: raise
