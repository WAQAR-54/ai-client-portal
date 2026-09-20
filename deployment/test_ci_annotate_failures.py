"""deployment/ci_annotate_failures.py turns a failed test run's output into annotations."""

import importlib.util
from pathlib import Path

from django.test import SimpleTestCase

_SPEC = importlib.util.spec_from_file_location("ci_annotate", Path(__file__).with_name("ci_annotate_failures.py"))
ci_annotate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci_annotate)

SAMPLE = """Found 3 test(s).
======================================================================
FAIL: test_a (app.tests.Thing.test_a)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "x.py", line 1, in test_a
    self.assertEqual(1, 2)
AssertionError: 1 != 2

======================================================================
ERROR: test_b (app.tests.Other.test_b)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "y.py", line 9, in test_b
    boom()
django.db.utils.OperationalError: connection refused

----------------------------------------------------------------------
Ran 3 tests in 1.0s

FAILED (failures=1, errors=1)
"""


class AnnotateFailuresTests(SimpleTestCase):
    def test_each_failing_test_becomes_one_annotation_with_its_reason(self):
        lines = ci_annotate.annotate(ci_annotate.summarise(SAMPLE))
        self.assertEqual(len(lines), 2)
        self.assertIn("::error title=failed test test_a::AssertionError: 1 != 2", lines[0])
        self.assertIn("test_b::django.db.utils.OperationalError: connection refused", lines[1])

    def test_a_crash_outside_any_test_shows_the_last_lines_of_the_output(self):
        output = (
            "Found 3 test(s).\nRan 3 tests in 1s\n\nOK\nDestroying test database\n"
            "OperationalError: database is being accessed by other users\n"
        )
        lines = ci_annotate.annotate(ci_annotate.summarise(output), output)
        self.assertIn("no failing test found in the re-run", lines[0])
        self.assertTrue(any("being accessed by other users" in line for line in lines))
        self.assertLessEqual(len(lines), ci_annotate.TAIL + 1)

    def test_workflow_command_characters_in_a_message_cannot_break_out(self):
        findings = [("t", "bad 100% of\nnew line")]
        line = ci_annotate.annotate(findings)[0]
        self.assertNotIn("\n", line)
        self.assertIn("100%25", line)

    def test_it_is_bounded(self):
        many = [(f"t{i}", "r") for i in range(40)]
        self.assertEqual(len(ci_annotate.annotate(many)), ci_annotate.LIMIT + 1)
