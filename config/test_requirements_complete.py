"""Every third-party module the project imports must be pinned in requirements.txt.

Why: commit 8f57030 imported `defusedxml` without listing it. It worked on the
developer machine only because pip-audit pulled it in transitively, so nothing
local failed - CI (a clean install) was the first to notice, after the commit was
already pushed. This test makes that mistake fail locally, where transitive
dependencies of dev tooling cannot mask it.

Everything must be covered by requirements.txt: that is what the production image AND the
CI test job install (requirements-dev.txt is only the lint job). A test that needs a
dev-only library has to import it inside try/except ImportError and skip without it.
Imports guarded that way are optional and are not checked.
"""

import ast
import re
import sys
from importlib import metadata
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"venv", ".venv", "node_modules", "staticfiles", "media", "archive", "migrations", "__pycache__", ".git"}


def _normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(filename):
    names = set()
    path = ROOT / filename
    if not path.exists():
        return names
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        names.add(_normalise(re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0]))
    return names


def _first_party():
    return {p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()} | {
        p.stem for p in ROOT.glob("*.py")
    }


def _optional_import_nodes(tree):
    """Imports inside a `try:` whose handlers catch ImportError: the code copes without them."""
    optional = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
            isinstance(h.type, ast.Name)
            and h.type.id in {"ImportError", "ModuleNotFoundError"}
            or isinstance(h.type, ast.Tuple)
            and any(isinstance(e, ast.Name) and e.id in {"ImportError", "ModuleNotFoundError"} for e in h.type.elts)
            for h in node.handlers
        ):
            for child in node.body:
                optional |= {id(n) for n in ast.walk(child)}
    return optional


def _imports(path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    optional = _optional_import_nodes(tree)
    found = set()
    for node in ast.walk(tree):
        if id(node) in optional:
            continue
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


class RequirementsCompleteTests(SimpleTestCase):
    def test_every_imported_third_party_module_is_pinned(self):
        prod = _requirement_names("requirements.txt")
        module_to_dists = {m: {_normalise(d) for d in dists} for m, dists in metadata.packages_distributions().items()}
        first_party, stdlib = _first_party(), set(sys.stdlib_module_names)
        problems = {}
        for path in ROOT.rglob("*.py"):
            if SKIP_DIRS & set(path.relative_to(ROOT).parts):
                continue
            for module in _imports(path):
                if module in stdlib or module in first_party or module == "__future__":
                    continue
                dists = module_to_dists.get(module)
                if dists is None:  # not installed here at all: cannot be mapped, report it
                    problems.setdefault(module, []).append(str(path.relative_to(ROOT)))
                elif not (dists & prod):
                    problems.setdefault(module, []).append(str(path.relative_to(ROOT)))
        self.assertEqual(
            problems,
            {},
            "imported but not pinned in requirements.txt (pin it, or import it inside try/except ImportError): "
            + ", ".join(f"{m} (in {v[0]})" for m, v in sorted(problems.items())),
        )

    def test_the_guard_itself_would_have_caught_defusedxml(self):
        """Prove the mapping works: defusedxml resolves to a distribution, and that distribution
        is (now) pinned. Without the pin in requirements.txt the test above would fail."""
        dists = {_normalise(d) for d in metadata.packages_distributions().get("defusedxml", [])}
        self.assertEqual(dists, {"defusedxml"})
        self.assertIn("defusedxml", _requirement_names("requirements.txt"))
