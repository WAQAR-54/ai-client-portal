"""Turn a failed `manage.py test` run into GitHub annotations (diagnostics only).

Reads Django/unittest output on stdin and prints one ::error annotation per FAIL/ERROR
block: the test id and the last line of its traceback (the assertion or exception).
GitHub exposes annotations on the public checks API, so the reason a run failed can be
read without downloading the (authenticated) raw log.

This NEVER decides success or failure - the test step's own exit code does that. The
workflow only runs it after that step has already failed (`if: failure()`).
"""

import re
import sys

BLOCK = re.compile(r"^(?:FAIL|ERROR): (?P<test>.+?)\n-{20,}\n(?P<body>.*?)(?=\n={20,}\n|\nRan \d+ test)", re.S | re.M)
LIMIT = 15
TAIL = 12
RULE = re.compile(r"^[-=]{10,}$")


def summarise(output):
    findings = []
    for match in BLOCK.finditer(output):
        lines = [line for line in match.group("body").splitlines() if line.strip() and not RULE.match(line)]
        reason = lines[-1].strip() if lines else "(no traceback)"
        test = match.group("test").split(" (")[0].strip()
        findings.append((test, reason))
    return findings


def _clean(text):
    """One line, and no character that GitHub would read as the start of a workflow command."""
    return text.replace("%", "%25").replace("\r", " ").replace("\n", " ")[:500]


def annotate(findings, output=""):
    """Annotations for the failing tests. When there are none but the run still failed (a crash
    before or after the tests, e.g. a database that could not be created or dropped), the last
    lines of the output are shown instead - that is where the reason is."""
    lines = []
    for test, reason in findings[:LIMIT]:
        lines.append(f"::error title=failed test {test}::{_clean(reason)}")
    if len(findings) > LIMIT:
        lines.append(f"::error title=failed tests::{len(findings) - LIMIT} more not shown")
    if not findings:
        tail = [line for line in output.splitlines() if line.strip()][-TAIL:]
        lines.append("::error title=no failing test found in the re-run::the run failed outside any test; last lines:")
        lines += [f"::error title=re-run output {number}::{_clean(line)}" for number, line in enumerate(tail, 1)]
    return lines


if __name__ == "__main__":
    text = sys.stdin.read()
    print("\n".join(annotate(summarise(text), text)))
