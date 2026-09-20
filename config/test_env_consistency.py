"""Every environment variable the code reads is documented in .env.example, and vice versa.

Names only: this never looks at a value. It found 22 variables the code read that nobody could
have known to set (GUNICORN_*, the Celery limits, the cookie flags, ...)."""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = ("venv", "archive", "node_modules", "staticfiles", "media", "logs")
NAME = r"([A-Z][A-Z0-9_]{2,})"
READS = re.compile(
    rf"""\benv(?:\.(?:int|bool|list|json|float|str|db|url))?\(\s*["']{NAME}["']"""
    rf"""|os\.environ(?:\.get)?[\[(]\s*["']{NAME}["']"""
    rf"""|os\.getenv\(\s*["']{NAME}["']"""
)
# Read through a dynamic name (f"DEMO_{role}_EMAIL"), so the scan above cannot see them.
READ_DYNAMICALLY = {
    "DEMO_ADMIN_EMAIL",
    "DEMO_ADMIN_PASSWORD",
    "DEMO_MANAGER_EMAIL",
    "DEMO_MANAGER_PASSWORD",
    "DEMO_USER_EMAIL",
    "DEMO_USER_PASSWORD",
}


def names_read_by_code():
    found = {}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in SKIP_DIRS or "migrations" in rel.parts or path.name.startswith("test"):
            continue
        for match in READS.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            found.setdefault(next(g for g in match.groups() if g), set()).add(rel.as_posix())
    return found


def example_lines():
    return (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()


class EnvironmentConsistencyTests(SimpleTestCase):
    def documented(self):
        return {m.group(1) for line in example_lines() if (m := re.match(rf"\s*#?\s*{NAME}=", line))}

    def test_every_variable_the_code_reads_is_documented(self):
        missing = {name: sorted(files) for name, files in names_read_by_code().items() if name not in self.documented()}
        self.assertEqual(missing, {}, "read by the code but absent from .env.example")

    def test_every_documented_variable_is_read_somewhere(self):
        unread = self.documented() - set(names_read_by_code()) - READ_DYNAMICALLY - {"DATABASE_URL"}
        self.assertEqual(sorted(unread), [], "documented in .env.example but never read (stale or misspelt?)")

    def test_no_variable_is_assigned_twice(self):
        active = [m.group(1) for line in example_lines() if (m := re.match(rf"{NAME}=", line))]
        self.assertEqual(sorted({n for n in active if active.count(n) > 1}), [])

    def test_the_example_holds_no_real_looking_secret(self):
        placeholder = re.compile(r"^(change-?me.*|example.*|your[-_].*|xxx.*|<.*>|.{0,12})$", re.IGNORECASE)
        suspicious = []
        for line in example_lines():
            match = re.match(r"\s*#?\s*([A-Z0-9_]*(?:SECRET|PASSWORD|KEY|TOKEN(?!S)|DSN)[A-Z0-9_]*)=(.+)", line)
            if match and match.group(2).strip() and not placeholder.match(match.group(2).strip()):
                suspicious.append(match.group(1))  # the name only: a value must never reach a test log
        self.assertEqual(suspicious, [])
