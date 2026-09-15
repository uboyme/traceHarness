from catalog import lookup
def read(catalog, reference):
    plugin = lookup(catalog, reference['plugin'])
    return plugin['resources'][reference['path']]
