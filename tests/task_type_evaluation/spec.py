"""Five task-type cases built on one pinned astroid tree (plan S0-E).

Every case starts from the verified upstream tree of SWE-bench Lite
``pylint-dev__astroid-1196`` so the code base is held constant and only the task
structure differs. A case is defined by exact text edits:

* ``inject`` turns the pristine tree into the Agent's initial tree;
* ``reference`` turns the pristine tree into a tree that must pass the verifier;
* ``hidden`` names new host-only test files under ``hidden/<case>/`` plus pristine
  upstream test files re-injected for regression protection.

``expected`` is the pre-registered Single/Multi expectation that the experiment
checks, never a design target. Nothing here is copied into an initial tree
except ``inject``.
"""

UPSTREAM = "pylint-dev__astroid-1196"

FIX_PROTECTION = {
    "protected_roots": ["tests"],
    "writable_test_patterns": ["tests/unittest_*.py", "tests/test_*.py"],
    "protected_files": ["setup.cfg"],
    "import_roots": ["."],
}

READ_MODULES = [
    "astroid/brain/brain_crypt.py",
    "astroid/brain/brain_functools.py",
    "astroid/brain/brain_random.py",
    "astroid/brain/brain_re.py",
    "astroid/brain/brain_ssl.py",
    "astroid/brain/brain_subprocess.py",
    "astroid/brain/brain_type.py",
    "astroid/brain/brain_uuid.py",
]

CASES = [
    {
        "case_id": "a1-small-hashlib-blake2",
        "category": "small",
        "expected": "single: lower cost at equal quality",
        "requirement": (
            "pylint now reports false `unexpected-keyword-arg` errors for calls such as "
            "`hashlib.blake2b(digest_size=16)` and `hashlib.blake2s(key=b'k')`. Inspecting "
            "astroid's model of the `hashlib` module shows the BLAKE2 constructors getting the "
            "same `value=''` signature as md5. Restore the real BLAKE2 constructor parameters "
            "(keyword-only `digest_size`, `key`, `salt`, `person`, ... with their defaults) "
            "without changing the signatures of the other hash algorithms."
        ),
        "inject": [
            {
                "file": "astroid/brain/brain_hashlib.py",
                "old": (
                    '        template % {"name": hashfunc, "digest": \'b""\', '
                    '"signature": signature}\n'
                    "        for hashfunc, signature in algorithms_with_signature.items()\n"
                ),
                "new": (
                    '        template % {"name": hashfunc, "digest": \'b""\', '
                    '"signature": signature}\n'
                    "        for hashfunc in algorithms_with_signature\n"
                ),
            }
        ],
        "reference": [],
        "hidden": {
            "new": ["tests/test_task_hashlib_blake2.py"],
            "pristine": ["tests/unittest_brain.py"],
        },
        **FIX_PROTECTION,
    },
    {
        "case_id": "a2-parallel-read-brain-registry",
        "category": "parallel-read",
        "expected": "multi: possibly shorter wall time at similar tokens",
        "requirement": (
            "Produce an inventory of how eight astroid brain plugins register themselves. "
            "Do not modify any existing file; only create `FINDINGS.json` at the repository "
            "root. It must be one JSON object whose keys are exactly these module paths: "
            + ", ".join(f"`{m}`" for m in READ_MODULES)
            + ". Each value is an object with exactly three keys. `functions`: the sorted "
            "names of functions defined directly in the module body (not nested, not inside "
            "`if`/`try` blocks). `transforms`: one object per call to `register_transform` "
            "anywhere in the module, with `node_class` (the name of the node class passed as "
            "the first argument; for `nodes.Call` write `Call`) and `transform` (the name of "
            "the function passed as the second argument; if that argument is "
            "`inference_tip(f)` write `f`; if it is a lambda write `<lambda>`); order does not "
            "matter. `module_extenders`: the module names (string literals) passed to "
            "`register_module_extender` in this module; order does not matter. Every entry "
            "must be supported by the source; an invented function or registration fails."
        ),
        "inject": [],
        "reference": [],
        "reference_findings": READ_MODULES,
        "hidden": {"new": ["tests/test_task_findings.py"], "pristine": []},
        "protected_roots": ["astroid", "tests"],
        "writable_test_patterns": [],
        "protected_files": ["setup.cfg"],
        "import_roots": ["."],
    },
    {
        "case_id": "a3-parallel-fix-three-brains",
        "category": "parallel-write",
        "expected": "multi: possibly shorter wall time, higher tokens",
        "requirement": (
            "Three unrelated inference regressions were reported against astroid's standard "
            "library plugins. (1) `uuid.UUID('{12345678-1234-5678-1234-567812345678}').int` "
            "is reported by pylint as `no-member`. (2) After `from random import sample`, a "
            "call like `sample(['a', 'b', 'c'], 2)` is no longer inferred as a list, although "
            "`random.sample(...)` still is. (3) In `with subprocess.Popen(cmd) as proc: "
            "proc.communicate()`, `proc` is inferred as `None`. Fix all three; each lives in a "
            "different module and they do not depend on each other. Keep existing behaviour "
            "otherwise."
        ),
        "inject": [
            {
                "file": "astroid/brain/brain_uuid.py",
                "old": 'lambda node: node.qname() == "uuid.UUID"',
                "new": 'lambda node: node.qname() == "uuid.SafeUUID"',
            },
            {
                "file": "astroid/brain/brain_random.py",
                "old": '    if isinstance(func, Name):\n        return func.name == "sample"\n',
                "new": (
                    '    if isinstance(func, Name):\n        return func.name == "random_sample"\n'
                ),
            },
            {
                "file": "astroid/brain/brain_subprocess.py",
                "old": "        def __enter__(self): return self\n",
                "new": "        def __enter__(self): return None\n",
            },
        ],
        "reference": [],
        "hidden": {
            "new": ["tests/test_task_three_brains.py"],
            "pristine": ["tests/unittest_brain.py"],
        },
        **FIX_PROTECTION,
    },
    {
        "case_id": "a4-investigate-negative-subscript",
        "category": "investigate-then-fix",
        "expected": "uncertain",
        "requirement": (
            "Users report that pylint stopped understanding negative subscripts on literal "
            "sequences: astroid infers nothing for `[1, 2, 3][-1]` or `(1, 2)[-2]`, while "
            "positive indexes and slices of the same literals still infer correctly. Find the "
            "cause in astroid's inference and fix it. Out-of-range indexes must still fail "
            "inference gracefully rather than crash."
        ),
        "inject": [
            {
                "file": "astroid/nodes/node_classes.py",
                "old": (
                    "        if isinstance(index, Const):\n            return elts[index.value]\n"
                ),
                "new": (
                    "        if isinstance(index, Const):\n"
                    "            position = (\n"
                    "                index.value if index.value >= 0 else len(elts) - index.value\n"
                    "            )\n"
                    "            return elts[position]\n"
                ),
            }
        ],
        "reference": [],
        "hidden": {
            "new": ["tests/test_task_negative_subscript.py"],
            "pristine": ["tests/unittest_inference.py"],
        },
        **FIX_PROTECTION,
    },
    {
        "case_id": "a5-dependent-sample-arguments",
        "category": "strong-dependency",
        "expected": "single: better (steps change the same function in order)",
        "requirement": (
            "Extend astroid's inference of `random.sample` in three steps that build on each "
            "other: (1) accept `k` passed by keyword, e.g. `random.sample([1, 2, 3], k=2)`; "
            "(2) accept the population passed by keyword, e.g. "
            "`random.sample(population=[1, 2], k=1)`; (3) accept any `k` expression that "
            "infers to a single integer constant, e.g. `n = 2; random.sample(items, n)`, not "
            "only a literal. The inferred value must remain a list of `k` elements taken from "
            "the literal population. Calls that cannot be satisfied (missing arguments, `k` "
            "larger than the population, non-integer `k`, non-literal population) must keep "
            "falling back to normal inference without raising."
        ),
        "inject": [],
        "reference": [
            {
                "file": "astroid/brain/brain_random.py",
                "old": (
                    "def infer_random_sample(node, context=None):\n"
                    "    if len(node.args) != 2:\n"
                    "        raise UseInferenceDefault\n"
                    "\n"
                    "    length = node.args[1]\n"
                    "    if not isinstance(length, Const):\n"
                    "        raise UseInferenceDefault\n"
                    "    if not isinstance(length.value, int):\n"
                    "        raise UseInferenceDefault\n"
                    "\n"
                    "    inferred_sequence = helpers.safe_infer(node.args[0], context=context)\n"
                ),
                "new": (
                    "def _sample_arguments(node):\n"
                    "    keywords = {\n"
                    "        keyword.arg: keyword.value for keyword in node.keywords or ()\n"
                    "    }\n"
                    '    if None in keywords or set(keywords) - {"population", "k"}:\n'
                    "        raise UseInferenceDefault\n"
                    "    positional = list(node.args)\n"
                    "    if len(positional) > 2:\n"
                    "        raise UseInferenceDefault\n"
                    '    names = ("population", "k")\n'
                    "    values = dict(zip(names, positional))\n"
                    "    for name, value in keywords.items():\n"
                    "        if name in values:\n"
                    "            raise UseInferenceDefault\n"
                    "        values[name] = value\n"
                    "    if set(values) != set(names):\n"
                    "        raise UseInferenceDefault\n"
                    '    return values["population"], values["k"]\n'
                    "\n"
                    "\n"
                    "def infer_random_sample(node, context=None):\n"
                    "    population, k = _sample_arguments(node)\n"
                    "    length = helpers.safe_infer(k, context=context)\n"
                    "    if not isinstance(length, Const):\n"
                    "        raise UseInferenceDefault\n"
                    "    if not isinstance(length.value, int) or isinstance(length.value, bool):\n"
                    "        raise UseInferenceDefault\n"
                    "\n"
                    "    inferred_sequence = helpers.safe_infer(population, context=context)\n"
                ),
            }
        ],
        "hidden": {
            "new": ["tests/test_task_sample_arguments.py"],
            "pristine": ["tests/unittest_brain.py"],
        },
        **FIX_PROTECTION,
    },
]
