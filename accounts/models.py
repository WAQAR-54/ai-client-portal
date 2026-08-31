from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class Department(models.Model):
    name = models.CharField(max_length=150, unique=True)
    monthly_budget_cap = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Monthly AI spending cap for this department, in USD.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Team(models.Model):
    """A Manager's scope within a Department. Kept as its own model rather
    than overloading Department (which already means something else — the
    unit an Admin is scoped to) — a department can have many teams, each
    with its own Manager and member list."""

    name = models.CharField(max_length=150)
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="teams")
    manager = models.OneToOneField(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_team",
        help_text="Kept in sync with that user's `team` field whenever their role is set to Manager.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["department__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["department", "name"], name="unique_team_name_per_department"),
        ]

    def __str__(self):
        return f"{self.name} ({self.department.name})"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("role", User.Role.USER)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", User.Role.ADMIN)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    class Role(models.TextChoices):
        USER = "user", _("User")
        MANAGER = "manager", _("Manager")
        ADMIN = "admin", _("Admin")
        SUPERADMIN = "superadmin", _("SuperAdmin")

    username = None
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.USER)
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
        help_text="For an Admin, this is what scopes their access. For a SuperAdmin it's unused (unscoped).",
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
        help_text="A Manager's own team is tracked via Team.manager instead — this is for team MEMBERSHIP.",
    )
    has_seen_onboarding = models.BooleanField(
        default=False,
        help_text="Set once the first-login guided tour is completed or skipped. "
        '"Replay tour" in Settings resets this to False.',
    )
    preferred_language = models.CharField(
        max_length=10,
        choices=[("en", "English"), ("ur", "اردو"), ("ar", "العربية")],
        default="en",
        help_text="UI label language (not the AI's reply language, which follows "
        "the conversation naturally). Set from Settings; see "
        "accounts/middleware.py for how this is applied on every request.",
    )
    theme_preference = models.CharField(
        max_length=10,
        choices=[("light", "Light"), ("dark", "Dark"), ("system", "System")],
        default="system",
        help_text="UI color theme. 'System' follows the OS/browser preference "
        "automatically. Set from Settings; see base.html for how this is applied.",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.email

    @property
    def is_admin(self):
        """True for Admin AND SuperAdmin — this is "can see the Admin
        section at all" (used to gate nav visibility), not "is exactly
        Admin". Use `is_superadmin` where the SuperAdmin-only distinction
        actually matters (Plan Management, model/department management)."""
        return self.role in (self.Role.ADMIN, self.Role.SUPERADMIN)

    @property
    def is_superadmin(self):
        return self.role == self.Role.SUPERADMIN

    @property
    def is_manager(self):
        return self.role == self.Role.MANAGER
