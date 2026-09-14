from metrics import total
def qualifies(base, candidate, base_score, candidate_score, ratio):
    return candidate_score >= base_score and total(candidate) <= total(base) * ratio
