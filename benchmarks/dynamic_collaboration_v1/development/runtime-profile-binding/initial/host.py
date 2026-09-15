from registry import resolve
def launch(profiles, profile_id, provider, model, tools, factory):
    profile = resolve(profiles, profile_id)
    return factory(provider, model, tuple(tools))
