def read(catalog, plan):
    name, source, revision = plan
    return catalog.entries[name][2]
