def record(created, request_id, resource):
    created[request_id] = resource
    return resource
