"""Read immutable Git images only after matching the original CAS Patch bytes."""

from traceh.api.workspace_edits import FileEdit, FileImage
from traceh.artifacts.errors import ArtifactGitError
from traceh.artifacts.git_patch import GitPatchBuilder


async def materialize_patch(artifact, root, limits):
    manifest = artifact.manifest
    git = GitPatchBuilder()
    if (await git._workspace_identity(root))[2] != manifest.repository_fingerprint:
        raise ArtifactGitError("artifact-repository-mismatch")

    async def read(*args, cap):
        return await git._run_required(root, *args, index_file=None, max_output_bytes=cap)

    patch = await read(
        "diff-tree",
        "--no-commit-id",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        manifest.base_revision,
        manifest.candidate_tree,
        "--",
        cap=limits.max_patch_bytes,
    )
    if patch != artifact.content:
        raise ArtifactGitError("artifact-tree-patch-mismatch")

    async def image(tree, path):
        raw = await read(
            "ls-tree", "-z", tree, "--", f":(literal){path}", cap=limits.max_path_bytes + 256
        )
        if not raw:
            return FileImage(None, "000000", None)
        entries = raw.split(b"\0")
        if len(entries) != 2 or entries[-1]:
            raise ArtifactGitError("artifact-tree-entry-invalid")
        header, actual_path = entries[0].split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split(" ")
        if (
            actual_path.decode("utf-8") != path
            or kind != "blob"
            or mode not in {"100644", "100755"}
        ):
            raise ArtifactGitError("artifact-special-git-object-rejected")
        content = await read("cat-file", "blob", oid, cap=limits.max_file_bytes)
        return FileImage(content, mode, oid)

    edits = []
    total = 0
    for path in manifest.changed_paths:
        before = await image(manifest.base_revision, path)
        after = await image(manifest.candidate_tree, path)
        total += len(before.content or b"") + len(after.content or b"")
        if total > 2 * limits.max_total_file_bytes:
            raise ArtifactGitError("artifact-total-size-exceeded")
        edits.append(FileEdit(path, before, after))
    return tuple(edits)
