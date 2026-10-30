"""Run only the tests that exercise the files you changed, in parallel, and print just the result.

Usage (from the repository root):

    .venv/bin/python tools/test_changed.py               # uncommitted changes + the last commit (HEAD~1..work tree)
    .venv/bin/python tools/test_changed.py --since main  # everything changed since a git ref (branch, tag, sha)
    .venv/bin/python tools/test_changed.py --list        # only show which test files would run, and why
    .venv/bin/python tools/test_changed.py --deep        # also tests reaching a changed module indirectly
    .venv/bin/python tools/test_changed.py -- -k colour  # extra pytest arguments go after "--"

The whole suite:

    .venv/bin/python -m pytest -n auto -q                # parallel (pytest-xdist, ~30 s on 4 cores)
    .venv/bin/python -m pytest -q                        # serial (~90 s)

How changed files map to test files:
  * a test file runs itself; tests/conftest.py, pyproject.toml, edit_os/__init__.py or base.yaml run everything;
  * a Python module (edit_os/, server/, tools/, tests/ helpers such as test_pipeline.py) runs the test files that
    import it, directly or through a test helper (an import graph built from the sources with `ast`); when none
    does (server/db.py), the test files that reach it through other modules. --deep always takes the latter, but
    pipeline.py imports most of the package, so that is usually ~70 of the ~77 files: run the full suite instead;
  * test files named after a changed file's stem (edit_os/legends.py -> tests/test_legends*.py) run too;
  * a journal style (edit_os/styles/jehp.yaml) runs tests/test_styles.py and the test files that name the journal
    ("jehp"); a data file in a package (edit_os/copyedit/data/*.txt) counts as a change to that package's modules;
  * any other file runs the test files that mention its path or file name. Docs and the like map to nothing.

Output is pytest's own (`-q --no-header -p no:warnings --tb=short`) with the progress dots dropped: the failures,
if any, and the summary line. The exit code is pytest's (0 when nothing needs testing).
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
PACKAGES = ("edit_os", "server")  # importable as dotted names from the root
FLAT_DIRS = ("tools", "tests")  # their modules are imported by bare name (sys.path / pytest rootdir)
RUN_ALL = {"tests/conftest.py", "pyproject.toml", "edit_os/__init__.py", "edit_os/styles/base.yaml"}
PROGRESS = re.compile(r"^[.sxXFEp]+\s*(\[\s*\d+%\])?$")


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def changed_files(since: str | None) -> list[str]:
    if since is None:
        base = "HEAD~1" if subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD~1"], cwd=ROOT,
                                          capture_output=True).returncode == 0 else "HEAD"
    else:
        base = since
    files = set(git("diff", "--name-only", base)) | set(git("ls-files", "--others", "--exclude-standard"))
    return sorted(f for f in files if not f.startswith(".venv"))  # the worktree's venv symlink is untracked


def module_files() -> dict[str, str]:
    """Dotted module name -> path relative to ROOT, for every module the tests can import."""
    mods = {}
    for pkg in PACKAGES:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, pkg)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if fn.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, fn), ROOT)
                    name = rel[:-3].replace(os.sep, ".")
                    mods[name[: -len(".__init__")] if name.endswith(".__init__") else name] = rel
    for d in FLAT_DIRS:
        for fn in os.listdir(os.path.join(ROOT, d)):
            if fn.endswith(".py"):
                mods.setdefault(fn[:-3], f"{d}/{fn}")
    return mods


def imports_of(rel: str, name: str, mods: dict[str, str]) -> set[str]:
    """Modules (keys of `mods`) that the file imports, plus the packages a dotted import loads on the way."""
    try:
        tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read(), rel)
    except (SyntaxError, UnicodeDecodeError):
        return set()
    is_pkg = rel.endswith("__init__.py")
    found = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = name.split(".")
                parts = parts if is_pkg else parts[:-1]
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(parts + ([base] if base else []))
            targets = [base] + [f"{base}.{a.name}" if base else a.name for a in node.names]
        for t in targets:
            while t:
                if t in mods:
                    found.add(t)
                t = t.rpartition(".")[0]
    found.discard(name)
    return found


def importers(mods: dict[str, str]) -> dict[str, set[str]]:
    """Module -> the modules that import it."""
    rev: dict[str, set[str]] = {m: set() for m in mods}
    for name, rel in mods.items():
        for dep in imports_of(rel, name, mods):
            rev[dep].add(name)
    return rev


def all_test_files() -> list[str]:
    return sorted(f"tests/{fn}" for fn in os.listdir(TESTS) if fn.startswith("test_") and fn.endswith(".py"))


def mentions(word: str, tests: list[str]) -> set[str]:
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", re.I)
    return {t for t in tests if pat.search(open(os.path.join(ROOT, t), encoding="utf-8").read())}


def reaching(starts: set[str], rev: dict[str, set[str]], mods: dict[str, str], tests: list[str],
             deep: bool) -> set[str]:
    """Test files importing one of `starts` directly or through test helpers (with `deep`, through anything)."""
    seen, todo = set(starts), list(starts)
    while todo:
        for imp in rev.get(todo.pop(), ()):
            if imp not in seen and (deep or mods[imp].startswith("tests/")):
                seen.add(imp)
                todo.append(imp)
    return {mods[m] for m in seen if mods[m] in tests}


def select(changed: list[str], deep: bool = False) -> tuple[set[str] | None, dict[str, list[str]]]:
    """Test files to run (None: all of them) and, per test file, the changed files that pulled it in."""
    tests = all_test_files()
    mods = module_files()
    by_path = {rel: name for name, rel in mods.items()}
    rev = importers(mods)
    why: dict[str, list[str]] = {}

    def add(found, cause):
        for t in found:
            why.setdefault(t, []).append(cause)

    for f in changed:
        if f in RUN_ALL:
            return None, {"(all)": [f]}
        if f.startswith("tests/") and os.path.basename(f).startswith("test_") and f.endswith(".py"):
            if os.path.exists(os.path.join(ROOT, f)):
                add([f], f)
        stem = os.path.splitext(os.path.basename(f))[0]
        starts = {by_path[f]} if f in by_path else set()
        if f.startswith("edit_os/styles/") and f.endswith(".yaml"):
            add(mentions(stem, tests) | {"tests/test_styles.py"}, f)
        elif not f.endswith(".py"):
            pkg_dir = os.path.dirname(f)
            while pkg_dir and not os.path.exists(os.path.join(ROOT, pkg_dir, "__init__.py")):
                pkg_dir = os.path.dirname(pkg_dir)
            if pkg_dir:  # package data: count it as a change to the package's own modules
                starts |= {by_path[p] for p in by_path if os.path.dirname(p) == pkg_dir}
            add(mentions(f, tests) | mentions(os.path.basename(f), tests), f)
        found = reaching(starts, rev, mods, tests, deep)
        if starts and not found:  # nothing imports it directly (server/db.py): the tests that reach it at all
            found = reaching(starts, rev, mods, tests, True)
        add(found, f)
        if f.endswith(".py") and not stem.startswith("test_") and stem != "__init__":
            add({t for t in tests if os.path.basename(t).startswith(f"test_{stem}")}, f)
    return set(why), why


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", help="git ref to diff against (default: HEAD~1, i.e. the last commit + uncommitted)")
    ap.add_argument("--list", action="store_true", help="show the selected test files and why; run nothing")
    ap.add_argument("--deep", action="store_true", help="also tests that reach a changed module indirectly")
    ap.add_argument("--serial", action="store_true", help="run without pytest-xdist")
    args = ap.parse_args(argv)

    changed = changed_files(args.since)
    selected, why = select(changed, args.deep)
    if args.list:
        print(f"{len(changed)} changed file(s)")
        for t in sorted(why):
            print(f"  {t}  <- {', '.join(sorted(set(why[t])))}")
        return 0
    if selected is not None and not selected:
        print(f"no tests exercise the {len(changed)} changed file(s)" if changed else "no changed files")
        return 0
    targets = ["tests"] if selected is None else sorted(selected)
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:warnings", "--tb=short"]
    if not args.serial:
        import importlib.util
        if importlib.util.find_spec("xdist"):
            cmd += ["-n", "auto" if selected is None or len(targets) > 1 else "2"]  # one file: two workers start faster
    label = "all test files" if selected is None else f"{len(targets)} test file(s)"
    print(f"{len(changed)} changed file(s) -> {label}", flush=True)
    proc = subprocess.run(cmd + targets + extra, cwd=ROOT, capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        s = line.strip()
        if not s or PROGRESS.match(s) or s.startswith(("bringing up nodes", "created: ", "[gw")):
            continue
        print(line)
    if proc.returncode not in (0, 5) and proc.stderr.strip():
        print(proc.stderr.rstrip()[-4000:], file=sys.stderr)
    return 0 if proc.returncode == 5 else proc.returncode


if __name__ == "__main__":
    sys.exit(main())
