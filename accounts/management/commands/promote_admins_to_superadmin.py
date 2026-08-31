"""Grandfathers the introduction of the SuperAdmin/department-scoped-Admin
split (see the role hierarchy prompt): every user who currently holds the
"Admin" role has full, unrestricted access today. Introducing a
department-scoped Admin tier means every current Admin must land somewhere
in the new hierarchy without losing access — the explicit answer given was
"migrate every current Admin to SuperAdmin" (preserving their exact current
access level), with specific people manually demoted to department-scoped
Admin afterward, at the client's own pace, once the new UI exists.

Defaults to a dry run (prints exactly who would change, writes nothing).
Pass --apply to actually save. Re-running after --apply is a no-op — no
Admin-role users will remain to promote, so it just reports 0 changes.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import User


class Command(BaseCommand):
    help = "Promote every current Admin-role user to SuperAdmin. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write the changes. Without this flag, only prints a preview.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        self.stdout.write(self.style.WARNING("DRY RUN - no writes will happen" if not apply else "APPLYING CHANGES"))
        self.stdout.write("")

        with transaction.atomic():
            admins = User.objects.filter(role=User.Role.ADMIN).order_by("email")
            if not admins.exists():
                self.stdout.write("No Admin-role users found - nothing to promote.")
            for user in admins:
                self.stdout.write(
                    f"{user.email!r} (id={user.id}, department={user.department_id}) -> role: 'admin' -> 'superadmin'"
                )
                if apply:
                    user.role = User.Role.SUPERADMIN
                    user.save(update_fields=["role"])

            if not apply:
                # Rolling back the atomic block even though nothing was
                # written in dry-run mode - defensive, in case a future edit
                # to this command accidentally writes something under
                # dry-run; guarantees dry-run is always truly read-only.
                transaction.set_rollback(True)

        self.stdout.write("")
        if apply:
            self.stdout.write(self.style.SUCCESS("Applied. Re-run without --apply any time to verify current state."))
        else:
            self.stdout.write(
                self.style.WARNING("Dry run complete - nothing was written. Re-run with --apply to save.")
            )
