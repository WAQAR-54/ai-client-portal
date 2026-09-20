"""The CI pipeline must gate on real exit codes, never on log text.

History: a commit was once gated locally with `grep "^OK"` on the test output and shipped
a failing test. These tests read .github/workflows/ci.yml and pin the properties that
make the pipeline trustworthy: failing lint/tests/migration checks stop the deploy, the
gates are plain commands (so their exit status is what counts), and the informational
post-deploy steps can never trigger the rollback."""

import re
from pathlib import Path
from unittest import skipIf

from django.test import SimpleTestCase

try:  # PyYAML is a local-only dependency: the CI test job installs requirements.txt alone
    import yaml
except ImportError:
    yaml = None

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
# "success" decided from output text: grep/awk/sed on results, or piping a gate into tee/head/tail.
_LOG_STRING_SUCCESS = re.compile(
    r"grep\s+.*(FAILED|\bOK\b|passed|Ran )|\|\s*(tee|head|tail)\b|\|\|\s*true", re.IGNORECASE
)


def _load():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job):
    return _load()["jobs"][job]["steps"]


def _step(job, name_part):
    return next(s for s in _steps(job) if name_part in s.get("name", ""))


@skipIf(yaml is None, "PyYAML is not installed (it only runs where developers edit the workflow)")
class CiGateTests(SimpleTestCase):
    def test_deploy_only_runs_after_lint_and_test_pass(self):
        deploy = _load()["jobs"]["deploy"]
        self.assertEqual(set(deploy["needs"]), {"lint", "test"})
        self.assertIn("refs/heads/main", deploy["if"])
        self.assertIn("push", deploy["if"])  # never from a pull request

    def test_the_gates_are_plain_commands_so_the_exit_code_decides(self):
        expected = {
            ("lint", "flake8"): "flake8",
            ("lint", "black"): "black --check .",
            ("lint", "pip-audit"): "pip-audit -r requirements.txt",
            ("test", "system checks"): "python manage.py check",
            ("test", "missing migrations"): "python manage.py makemigrations --check --dry-run",
            ("test", "Run tests"): "python manage.py test",
        }
        for (job, name_part), command in expected.items():
            step = _step(job, name_part)
            self.assertEqual(step["run"].strip(), command, step["name"])
            self.assertNotIn("continue-on-error", step, step["name"])

    def test_no_gate_decides_success_from_log_text(self):
        for job in ("lint", "test"):
            for step in _steps(job):
                self.assertIsNone(_LOG_STRING_SUCCESS.search(step.get("run", "")), f"{job}: {step.get('name')}")

    def test_dependencies_are_installed_fresh_from_requirements_txt(self):
        """A clean install on the runner is what catches a dependency missing from requirements.txt
        (the defusedxml incident); a cached venv or extra dev tools would hide it."""
        install = _step("test", "Install dependencies")["run"]
        self.assertEqual(install.strip(), "pip install -r requirements.txt")
        names = [step.get("name") for step in _steps("test")]
        self.assertLess(names.index("Install dependencies"), names.index("Django system checks"))

    def test_the_failure_diagnostics_step_only_reports_and_cannot_decide_the_outcome(self):
        names = [step.get("name") for step in _steps("test")]
        self.assertGreater(names.index("Failure diagnostics (re-run, annotations only)"), names.index("Run tests"))
        step = _step("test", "Failure diagnostics")
        self.assertEqual(step["if"], "failure()")  # only after a real failure
        self.assertTrue(step["continue-on-error"])  # and can never turn a green run red
        # GitHub runs `run:` scripts with -e: without `set +e` the failing re-run would stop the
        # script before the annotations were ever printed.
        self.assertTrue(step["run"].lstrip().startswith("set +e"))

    def test_the_health_check_gates_the_deploy_and_a_failure_rolls_back(self):
        health = _step("deploy", "Health check")
        self.assertNotIn("continue-on-error", health)
        self.assertIn("exit 1", health["run"])
        rollback = _step("deploy", "Roll back")
        self.assertEqual(rollback["if"], "failure()")

    def test_informational_post_deploy_steps_can_never_trigger_the_rollback(self):
        """continue-on-error keeps the job green, so `if: failure()` on the rollback never fires
        because of them."""
        for name_part in ("Smoke test", "Post-deploy verification"):
            self.assertTrue(_step("deploy", name_part)["continue-on-error"], name_part)

    def test_the_deploy_stamps_the_release_and_verification_checks_it(self):
        deploy_script = _step("deploy", "Deploy over SSH")["with"]["script"]
        self.assertIn('RELEASE_SHA="$(git rev-parse HEAD)" docker compose up -d --build', deploy_script)
        verify = _step("deploy", "Post-deploy verification")["with"]["script"]
        for service in ("web", "worker", "beat"):
            self.assertIn(service, verify)
        self.assertIn("printenv RELEASE_SHA", verify)
        self.assertIn("ops_verify", verify)

    def test_rollback_also_stamps_the_release_it_restores(self):
        self.assertIn(
            'RELEASE_SHA="$PREV_SHA" docker compose up -d --build', _step("deploy", "Roll back")["with"]["script"]
        )

    def test_the_verification_step_is_read_only(self):
        script = _step("deploy", "Post-deploy verification")["with"]["script"]
        for forbidden in ("migrate", "makemigrations", "flush", "down", "restart", "rm ", "delete", "DROP", "ALTER"):
            self.assertNotIn(forbidden, script, forbidden)
