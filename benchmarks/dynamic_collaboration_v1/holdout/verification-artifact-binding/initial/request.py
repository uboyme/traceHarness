def request(artifact_id, patch_sha, target, verifier_digest):
    return dict(artifact_id=artifact_id, patch_sha=patch_sha,
                target=target, verifier_digest=verifier_digest)
