from projection import active
from ranking import matches
def search(events, project, query):
    return [r for r in active(events) if matches(r, query)]
