from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class Department(models.Model):
    class RegionRestriction(models.TextChoices):
        NONE = "none", _("No restrictions")
        EU_ONLY = "eu_only", _("Only EU-hosted models allowed")

    name = models.CharField(max_length=150, unique=True)
    monthly_budget_cap = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Monthly AI spending cap for this department, in USD.",
    )
    # Compliance Routing (governance/plans.py::region_allowed_provider_model_ids)
    # - a hard ceiling on which providers this department's requests can
    # ever reach, checked on top of whatever a user's Plan/team already
    # allows, never widening it. EU_ONLY is the only restricted state for
    # now (matches the one real requirement this shipped for); more region
    # choices land as Provider.Region grows real providers outside US/EU.
    region_restriction = models.CharField(
        max_length=10, choices=RegionRestriction.choices, default=RegionRestriction.NONE
    )

    class RetentionPeriod(models.TextChoices):
        DAYS_30 = "30", _("30 days")
        DAYS_90 = "90", _("90 days")
        YEARS_7 = "2555", _("7 years")
        FOREVER = "forever", _("Forever")

    # How long a conversation from a user in this department is kept
    # before governance/tasks.py::sweep_conversation_retention deletes it
    # (whole Conversation, cascading to its Messages) - measured from the
    # conversation's own last activity (Conversation.updated_at), not its
    # creation date, so an old conversation someone keeps returning to
    # never gets swept out from under them.
    retention_period = models.CharField(max_length=10, choices=RetentionPeriod.choices, default=RetentionPeriod.FOREVER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def retention_days(self):
        """None means "forever" (never swept) - the field's own FOREVER
        value isn't a valid int, so callers should always go through this
        rather than int()'ing retention_period directly."""
        if self.retention_period == self.RetentionPeriod.FOREVER:
            return None
        return int(self.retention_period)


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
    disabled_models = models.ManyToManyField(
        "chat.ModelConfig",
        blank=True,
        related_name="disabled_for_teams",
        help_text="Models this team's Manager has restricted for their team, on top of whatever "
        "each member's Plan already allows - this only ever narrows access, never grants beyond "
        "the Plan (see governance/plans.py:effective_allowed_model_ids).",
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
    # Email-OTP MFA. Only meaningful for User/Manager - Admin/SuperAdmin are
    # ALWAYS required to complete MFA regardless of this flag's value (see
    # accounts/mfa.py::user_requires_mfa), so it's never toggled for them and
    # this field just sits at its default True/False for those roles without
    # being read. A User/Manager can turn it on/off themselves from Settings.
    mfa_enabled = models.BooleanField(default=False)

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
