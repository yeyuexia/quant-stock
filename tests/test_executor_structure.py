"""Structural invariants for executor.py that guard against subtle load-time bugs.

Regression history:
- feda38a: new tasks appended helper functions after `if __name__ == "__main__": main()`.
  When run as a script, main() fires at module-load time BEFORE those appended
  functions get defined → NameError on _process_slices and friends. Tests that
  `import executor` never triggered it because __main__ only runs in script mode.
"""
import ast
import os


EXECUTOR_PATH = os.path.join(os.path.dirname(__file__), "..", "executor.py")


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
        return False
    if len(test.comparators) != 1 or not isinstance(test.comparators[0], ast.Constant):
        return False
    return test.comparators[0].value == "__main__"


def test_main_guard_follows_all_function_definitions():
    """`if __name__ == "__main__": main()` must be the LAST top-level statement
    of executor.py (or at least follow every function/class definition).

    If a task appends a new def after this guard, running `python3 executor.py`
    will NameError on that function at load time because main() fires first.
    Tests that only `import` the module never trigger this — script invocation
    is what breaks. This check catches it without needing a subprocess."""
    src = open(EXECUTOR_PATH).read()
    tree = ast.parse(src)

    main_guard_idx = None
    last_def_idx = None

    for i, node in enumerate(tree.body):
        if _is_main_guard(node):
            main_guard_idx = i
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            last_def_idx = i

    assert main_guard_idx is not None, "no `if __name__ == \"__main__\"` guard found"
    assert last_def_idx is not None, "no function/class definitions found"
    assert main_guard_idx > last_def_idx, (
        f"`if __name__ == \"__main__\"` block at body index {main_guard_idx} "
        f"appears BEFORE the last function/class definition at index {last_def_idx}. "
        f"When `python3 executor.py` runs, main() fires at load time and any "
        f"function defined after the guard is NameError-unresolved. Move the "
        f"guard to the end of the file."
    )


def test_main_function_defined_before_guard():
    """Sanity: `main` must be defined before the `if __name__` guard tries to call it."""
    src = open(EXECUTOR_PATH).read()
    tree = ast.parse(src)

    main_def_idx = None
    main_guard_idx = None
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_def_idx = i
        if _is_main_guard(node):
            main_guard_idx = i

    assert main_def_idx is not None, "no `def main()` found"
    assert main_guard_idx is not None, "no `__main__` guard found"
    assert main_def_idx < main_guard_idx, (
        "`def main()` must be defined before the `__main__` guard that calls it"
    )
