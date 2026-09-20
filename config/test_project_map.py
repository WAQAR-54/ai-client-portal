"""PROJECT_MAP.md only names files that exist.

Regression for: the map kept describing files that had been removed or moved (chat/tasks.py,
billing/admin.py, templates/billing/*.html living in the app) - a stale map sends the next person to
edit the wrong file. Names only, no content check: this proves the PATHS are real, not that the
prose beside them is right."""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS = (
    ".py",
    ".html",
    ".md",
    ".js",
    ".json",
    ".css",
    ".yml",
    ".yaml",
    ".txt",
    ".example",
    ".toml",
    ".sh",
    ".conf",
)
# Not files: a naming pattern and a media sub-directory that exists only at runtime.
NOT_FILES = {"_xxx_table.html", "branding/"}


def referenced_paths():
    text = (ROOT / "PROJECT_MAP.md").read_text(encoding="utf-8")
    text = re.sub(r"~~`[^`\n]+`~~", "", text)  # struck through = documented as "no longer exists"
    # A line that says a file "exist nahi karti" is deliberately naming something that is gone.
    text = "\n".join(line for line in text.splitlines() if not re.search(r"exist nahi", line, re.IGNORECASE))
    found = set()
    for token in re.findall(r"`([^`\n]+)`", text):
        base = token.strip().split("::")[0].split(" ")[0].split("(")[0]
        if base in NOT_FILES or any(ch in base for ch in "*<>{}$") or base.startswith(("http", "/")):
            continue
        if base.endswith(EXTENSIONS) or (base.endswith("/") and "/" in base):
            found.add(base)
    return sorted(found)


class ProjectMapTests(SimpleTestCase):
    def test_every_file_the_map_names_exists(self):
        missing = []
        for base in referenced_paths():
            direct = ROOT / base
            if direct.exists():
                continue
            if "/" not in base and any(ROOT.rglob(base)):  # a bare file name: it must exist somewhere
                continue
            missing.append(base)
        self.assertEqual(missing, [], "PROJECT_MAP.md names files that do not exist - fix or strike them through")

    def test_the_map_covers_the_final_phase_and_uses_the_status_vocabulary(self):
        text = (ROOT / "PROJECT_MAP.md").read_text(encoding="utf-8")
        for needle in (
            "accounts/management/commands/verify_backup.py",
            "accounts/test_transport_security.py",
            "governance/test_media_bulk.py",
            "governance/test_final_object_sweep.py",
            "chat/test_extraction_limits.py",
        ):
            self.assertIn(needle, text)
            self.assertTrue((ROOT / needle).exists(), needle)
        for status in ("IMPLEMENTED", "VERIFIED", "PARTIALLY VERIFIED", "UNVERIFIED", "BLOCKED", "KNOWN LIMITATION"):
            self.assertIn(status, text)
        for phase in ("Phase 1", "Phase 2", "Phase 3", "Phase 4C", "Phase 5", "Phase 6", "Final phase"):
            self.assertIn(phase, text)
        self.assertNotIn("Bulk delete route **nahi hai**", text)  # stale once bulk delete shipped

    def test_the_map_covers_the_phase_6_areas(self):
        text = (ROOT / "PROJECT_MAP.md").read_text(encoding="utf-8")
        for needle in (
            "governance/media_service.py",
            "chat/context_window.py",
            "governance/task_monitor.py",
            "chat/ai_metrics.py",
            "config/client_errors.py",
            "governance/test_object_authorization.py",
        ):
            self.assertIn(needle, text)
            self.assertTrue((ROOT / needle).exists(), needle)
