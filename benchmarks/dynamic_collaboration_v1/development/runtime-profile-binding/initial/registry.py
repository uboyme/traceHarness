def resolve(profiles, profile_id):
    return profiles.get(profile_id, next(iter(profiles.values())))
