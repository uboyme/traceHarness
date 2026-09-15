"""A read-heavy comprehension task over a real, context-window-sized codebase.

The corpus is a bounded slice of this repository: the engine packages plus the
oldest 30 decision records, deliberately without any whole-project summary. No
single file answers the question, so the answer has to be assembled from many
sources - which is the condition under which "one reader" and "several readers"
can actually differ. Nothing here scores anything: the deliverable is checked
mechanically (citations resolve, nothing invented), not judged for prose.
"""

import hashlib
import subprocess
from pathlib import Path

DELIVERABLE = "ARCHITECTURE.md"
#: Packages left out so the tree fits the frozen initial-tree bound (256 files,
#: 8 MiB). Dropping whole subsystems keeps the remaining tree coherent; trimming
#: files inside a package would not.
EXCLUDED_PACKAGES = ("tui", "cli", "inspector", "plugins", "evolution", "workflow", "evaluation")
ADR_COUNT = 30
#: Generic skeleton. It fixes the shape of the answer without naming a single
#: subsystem, flow or decision, so neither arm is handed part of the answer.
SECTIONS = (
    "## What this codebase is for",
    "## Subsystems",
    "## How work flows through it",
    "## Boundaries and invariants",
    "## What I could not determine",
)
MIN_DOC_CHARS = 4_000
#: One reply must be able to carry the whole deliverable. Round 1's 60,000
#: characters were roughly 18,000 output tokens and truncated the tool-call JSON;
#: round 2's 12,000 was below what the model naturally produced, so it died
#: rewriting its own document. Observed lengths are 16,202 and 22,231 characters,
#: which is why the reply ceiling is raised alongside this bound rather than the
#: bound being cut again.
MAX_DOC_CHARS = 24_000
MIN_CITED_FILES = 25
MIN_CITED_PACKAGES = 8
MIN_ANCHORED = 12

REQUIREMENT = (
    "This tree is a real codebase you have not seen before. A new engineer asks: what is "
    "this project, what are its parts, and how does work flow through it? Read the source "
    f"and the decision records under docs/adr, then write your answer to {DELIVERABLE} at "
    "the tree root.\n\n"
    f"{DELIVERABLE} must contain these top-level sections, in this order: "
    + ", ".join(SECTIONS)
    + ". Every claim about the code must carry a citation: put the exact relative path in "
    "backticks, for example `src/traceh/example.py`, and when you point at something "
    "specific, anchor it as `src/traceh/example.py:SymbolName`, "
    "`src/traceh/example.py:120` or `src/traceh/example.py:120-140`. "
    "The deliverable must cite at least "
    f"{MIN_CITED_FILES} distinct files from at least {MIN_CITED_PACKAGES} different "
    f"packages, including at least {MIN_ANCHORED} anchored citations, and must be between "
    f"{MIN_DOC_CHARS} and {MAX_DOC_CHARS} characters.\n\n"
    "Every cited path, symbol and line must exist: a single invented or unchecked citation "
    "fails the deliverable. There is no summary document in this tree, so the answer has to "
    "be assembled from many sources. Say what you could not determine instead of guessing, "
    f"and describe only the reading you actually did. Do not modify any existing file; "
    f"{DELIVERABLE} is the only file you may add."
)
PUBLIC_REQUIREMENT = (
    "The fixed check verifies mechanically, not by judging prose: every existing file is "
    f"unchanged and {DELIVERABLE} is the only addition; the required sections are present; "
    "every backticked path-like citation resolves to a file in this tree; every anchored "
    "citation's symbol occurs in that file or its line number exists; and the citation "
    "counts above are met. Any citation that does not resolve fails the check."
)


def corpus_paths(repository: Path) -> tuple[str, ...]:
    """The frozen file list, derived from the repository's tracked files."""

    tracked = subprocess.run(
        ("git", "ls-files"),
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    present = [name for name in tracked if name and (repository / name).is_file()]
    code = [
        name
        for name in present
        if name.startswith("src/traceh/")
        and name.endswith(".py")
        and (name.count("/") == 2 or name.split("/")[2] not in EXCLUDED_PACKAGES)
    ]
    adrs = sorted(name for name in present if name.startswith("docs/adr/"))[:ADR_COUNT]
    if not code or len(adrs) != ADR_COUNT:
        raise ValueError("comprehension-corpus-unexpected")
    return tuple(sorted(code) + adrs)


def tree_digest(files: dict[str, bytes]) -> str:
    """One digest over the whole frozen corpus, recomputed by the fixed check."""

    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(hashlib.sha256(files[name]).digest())
    return digest.hexdigest()


def checks(digest: str) -> str:
    """The fixed verification program, run inside the frozen image.

    The corpus is discovered from the tree rather than listed here: a verifier
    argument is bounded, and one digest over every non-deliverable file's name
    and content already refuses any modification, addition or deletion.
    """

    return (
        "import hashlib,re\n"
        "from pathlib import Path\n"
        f"expected={digest!r}\n"
        f"sections={list(SECTIONS)!r}\n"
        "files={p.as_posix() for p in Path('.').rglob('*') if p.is_file() "
        "and not {'.git','__pycache__'}&set(p.parts)}\n"
        f"assert {DELIVERABLE!r} in files,'deliverable missing'\n"
        f"corpus=sorted(files-{{{DELIVERABLE!r}}})\n"
        "d=hashlib.sha256()\n"
        "for n in corpus:\n"
        "    d.update(n.encode('utf-8'));d.update(hashlib.sha256(Path(n).read_bytes()).digest())\n"
        "assert d.hexdigest()==expected,'existing files were modified, added or removed'\n"
        f"doc=Path({DELIVERABLE!r}).read_text(encoding='utf-8')\n"
        f"assert {MIN_DOC_CHARS}<=len(doc)<={MAX_DOC_CHARS},len(doc)\n"
        "at=-1\n"
        "for head in sections:\n"
        "    nxt=doc.find(head)\n"
        "    assert nxt>at,('section missing or out of order',head)\n"
        "    at=nxt\n"
        "cited=set();anchored=0;bad=[]\n"
        "for span in re.findall(r'`([^`\\n]{3,200})`',doc):\n"
        "    body,sep,suffix=span.partition(':')\n"
        "    body=body.strip()\n"
        "    if '/' not in body or not body.endswith(('.py','.md')):\n"
        "        continue\n"
        "    if body not in set(corpus):\n"
        "        bad.append(span);continue\n"
        "    cited.add(body)\n"
        "    if not sep:\n"
        "        continue\n"
        "    text=Path(body).read_text(encoding='utf-8',errors='replace')\n"
        "    suffix=suffix.strip()\n"
        "    lines=text.count(chr(10))+1\n"
        "    if re.fullmatch(r'[0-9]+',suffix):\n"
        "        ok=0<int(suffix)<=lines\n"
        "    elif re.fullmatch(r'[0-9]+-[0-9]+',suffix):\n"
        "        a,b=[int(v) for v in suffix.split(chr(45))]\n"
        "        ok=0<a<=b<=lines\n"
        "    else:\n"
        "        ok=bool(suffix) and suffix in text\n"
        "    anchored+=1 if ok else 0\n"
        "    if not ok:\n"
        "        bad.append(span)\n"
        "assert not bad,('citations that do not resolve',sorted(set(bad))[:10])\n"
        f"assert len(cited)>={MIN_CITED_FILES},('distinct files cited',len(cited))\n"
        "packages={c.split('/')[2] for c in cited if c.startswith('src/traceh/') "
        "and c.count('/')>2}\n"
        f"assert len(packages)>={MIN_CITED_PACKAGES},('packages cited',sorted(packages))\n"
        f"assert anchored>={MIN_ANCHORED},('anchored citations',anchored)\n"
        "print('ok',len(cited),len(packages),anchored)\n"
    )
