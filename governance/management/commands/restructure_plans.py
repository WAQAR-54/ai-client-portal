"""Extends the Plan/Tier system with per-request-count and per-request-
context-token caps (see governance/models.py's new fields). Renames the
existing Standard/Premium plans to Basic/Advanced IN PLACE (same row, same
id) rather than deleting and recreating them, so every UserPlanAssignment
FK pointing at them keeps resolving correctly with zero data loss. Demo is
updated in place too. Full is created fresh (it's genuinely new).

Defaults to a dry run (prints exactly what would change, writes nothing).
Pass --apply to actually save. Re-running after --apply is a no-op (every
target value is idempotent - the second run just prints the same values
already in place with no diff).
"""

from django.core.management.base import BaseCommand
from django.db import transaction

# New field values per plan. allowed_models is deliberately untouched here -
# the existing M2M rows (already correct per-plan) are reused as-is per the
# spec ("reuse the existing relation").
PLAN_UPDATES = {
    "Demo": {
        "max_requests_per_period": 10,
        "period": "day",
        "max_context_tokens": 4000,
        "new_feature_flags": {"tools": False, "priority_queue": False, "long_context": False},
    },
    "Standard": {
        "rename_to": "Basic",
        "max_requests_per_period": 50,
        "period": "day",
        "max_context_tokens": 16000,
        "new_feature_flags": {"tools": False, "priority_queue": False, "long_context": False},
    },
    "Premium": {
        "rename_to": "Advanced",
        "max_requests_per_period": 200,
        "period": "day",
        "max_context_tokens": 64000,
        "new_feature_flags": {"tools": True, "priority_queue": False, "long_context": True},
    },
}

FULL_PLAN_DEFAULTS = {
    "description": (
        "Every available model, no request-count/context ceiling that matters in practice, full feature set."
    ),
    "is_demo": False,
    "demo_duration_days": None,
    "daily_token_limit": None,
    "monthly_token_limit": None,
    "messages_per_session_limit": None,
    "sessions_per_day_limit": None,
    "monthly_budget_cap": None,
    "max_requests_per_period": 1000,
    "period": "day",
    "max_context_tokens": 128000,
    "feature_flags": {
        "file_upload": True,
        "export": True,
        "priority_routing": True,
        "tools": True,
        "priority_queue": True,
        "long_context": True,
    },
    "is_active": True,
    "is_default": False,
    "is_visible_to_admins": True,
}


class Command(BaseCommand):
    help = (
        "Extend Plan with per-request-count/context caps; rename Standard->Basic, "
        "Premium->Advanced; create Full. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write the changes. Without this flag, only prints a preview.",
        )

    def handle(self, *args, **options):
        from chat.models import ModelConfig
        from governance.models import Plan, UserPlanAssignment

        apply = options["apply"]
        self.stdout.write(self.style.WARNING("DRY RUN - no writes will happen" if not apply else "APPLYING CHANGES"))
        self.stdout.write("")

        with transaction.atomic():
            for old_name, spec in PLAN_UPDATES.items():
                plan = Plan.objects.filter(name=old_name).first()
                if plan is None:
                    self.stdout.write(self.style.ERROR(f"Plan {old_name!r} not found - skipping (nothing to rename)."))
                    continue

                new_name = spec.get("rename_to", old_name)
                assignment_count = UserPlanAssignment.objects.filter(plan=plan).count()
                merged_flags = {**plan.feature_flags, **spec["new_feature_flags"]}

                self.stdout.write(f"{old_name!r} (id={plan.id}, {assignment_count} user(s) assigned) ->")
                if new_name != old_name:
                    self.stdout.write(f"  name:                    {old_name!r} -> {new_name!r}")
                self.stdout.write(
                    f"  max_requests_per_period: {plan.max_requests_per_period} -> {spec['max_requests_per_period']}"
                )
                self.stdout.write(f"  period:                  {plan.period!r} -> {spec['period']!r}")
                self.stdout.write(
                    f"  max_context_tokens:      {plan.max_context_tokens} -> {spec['max_context_tokens']}"
                )
                self.stdout.write(f"  feature_flags:           {plan.feature_flags} -> {merged_flags}")
                self.stdout.write("")

                if apply:
                    plan.name = new_name
                    plan.max_requests_per_period = spec["max_requests_per_period"]
                    plan.period = spec["period"]
                    plan.max_context_tokens = spec["max_context_tokens"]
                    plan.feature_flags = merged_flags
                    plan.save(
                        update_fields=[
                            "name",
                            "max_requests_per_period",
                            "period",
                            "max_context_tokens",
                            "feature_flags",
                        ]
                    )

            full_existing = Plan.objects.filter(name="Full").first()
            if full_existing:
                self.stdout.write(f"'Full' (id={full_existing.id}) already exists - leaving as-is.")
            else:
                self.stdout.write("'Full' -> would be created fresh with:")
                for k, v in FULL_PLAN_DEFAULTS.items():
                    self.stdout.write(f"  {k}: {v}")
                self.stdout.write("")
                if apply:
                    full_plan = Plan.objects.create(name="Full", **FULL_PLAN_DEFAULTS)
                    full_plan.allowed_models.set(ModelConfig.objects.filter(is_enabled=True))
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Created 'Full' (id={full_plan.id}) with "
                            f"{full_plan.allowed_models.count()} allowed model(s)."
                        )
                    )

            if not apply:
                # Rolling back the atomic block even though nothing was
                # written in dry-run mode - defensive, in case a future edit
                # to this command accidentally writes something under
                # dry-run; guarantees dry-run is always truly read-only.
                transaction.set_rollback(True)

        if apply:
            self.stdout.write(self.style.SUCCESS("Applied. Re-run without --apply any time to verify current state."))
        else:
            self.stdout.write(
                self.style.WARNING("Dry run complete - nothing was written. Re-run with --apply to save.")
            )
