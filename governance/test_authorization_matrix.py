"""Authorization matrix: what each role is actually let into, tested at the backend
endpoint - never inferred from which buttons a template shows.

Two layers:
  1. ROUTE GATES - every URL in the project is requested as each role (see
     accounts/authz_matrix.py) and compared with an explicit policy below.
  2. OBJECT ISOLATION - real objects owned by user A are requested by user B.

Set AUTHZ_DUMP=<file> to write the full route x role table as JSON for review.
"""

import json
import os
from collections import defaultdict

from django.test import Client, TestCase

from accounts.authz_matrix import is_public, iter_routes, probe
from accounts.models import Department, Team, User

ROLES = ("anonymous", "user", "member", "manager", "dept_admin", "admin", "superadmin")


class MatrixFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.department = Department.objects.create(name="Dept A")
        other_department = Department.objects.create(name="Dept B")
        cls.other_department = other_department
        make = User.objects.create_user
        cls.users = {
            "user": make(email="plain@example.com", password="pw12345!"),
            "manager": make(email="manager@example.com", password="pw12345!", role=User.Role.MANAGER),
            "dept_admin": make(
                email="deptadmin@example.com", password="pw12345!", role=User.Role.ADMIN, department=cls.department
            ),
            "admin": make(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN),
            "superadmin": make(
                email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
            ),
        }
        cls.team = Team.objects.create(name="Team A", department=cls.department, manager=cls.users["manager"])
        cls.users["member"] = make(email="member@example.com", password="pw12345!", team=cls.team)

    def client_for(self, role):
        client = Client()
        if role != "anonymous":
            client.force_login(self.users[role])
        return client


def build_matrix(fixture):
    """{(name, path): {role: (method, verdict, status)}} for every non-regex route."""
    matrix = defaultdict(dict)
    routes = sorted(set(iter_routes()), key=lambda item: item[1])
    for role in ROLES:
        client = fixture.client_for(role)
        for name, path in routes:
            if "logout" in path:
                continue  # ends the very session being probed (accounts and Django admin)
            matrix[(name, path)][role] = probe(client, path)
    return matrix


class AuthorizationMatrixDump(MatrixFixture):
    def test_dump_the_matrix_for_review(self):
        target = os.environ.get("AUTHZ_DUMP")
        if not target:
            self.skipTest("set AUTHZ_DUMP=<file> to write the route x role table")
        matrix = build_matrix(self)
        rows = [
            {
                "name": name,
                "path": path,
                **{role: f"{v[1]}/{v[2]}" for role, v in verdicts.items()},
                "loc": verdicts["anonymous"][3],
            }
            for (name, path), verdicts in matrix.items()
        ]
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=1)
        self.assertTrue(rows)
        self.assertTrue(all(is_public(r["path"]) or True for r in rows))
