import environ
from django.core.management.base import BaseCommand

from accounts.models import User

env = environ.Env()


class Command(BaseCommand):
    help = "Create or update one demo account per role (admin/manager/user) from DEMO_* vars in .env."

    def handle(self, *args, **options):
        roles = [
            (User.Role.ADMIN, "DEMO_ADMIN_EMAIL", "DEMO_ADMIN_PASSWORD"),
            (User.Role.MANAGER, "DEMO_MANAGER_EMAIL", "DEMO_MANAGER_PASSWORD"),
            (User.Role.USER, "DEMO_USER_EMAIL", "DEMO_USER_PASSWORD"),
        ]

        for role, email_var, password_var in roles:
            email = env(email_var, default="")
            password = env(password_var, default="")
            if not email or not password:
                self.stdout.write(self.style.WARNING(f"Skipping {role}: {email_var}/{password_var} not set in .env"))
                continue

            # Deliberately not get_or_create(): its internal creation path
            # would INSERT the row (with password="") before we ever get a
            # chance to hash one in, briefly persisting an unhashed/blank
            # password. Building the instance in memory first and saving
            # only once, after set_password(), means the row never exists
            # in the DB without a real hash.
            try:
                user = User.objects.get(email=email)
                created = False
            except User.DoesNotExist:
                user = User(email=email)
                created = True
            user.role = role
            user.set_password(password)
            if role == User.Role.ADMIN:
                user.is_staff = True
                user.is_superuser = True
            user.is_active = True
            user.save()

            action = "Created" if created else "Updated"
            self.stdout.write(self.style.SUCCESS(f"{action} {role} account: {email}"))
