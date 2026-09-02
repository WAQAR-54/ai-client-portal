"""Mirrors this dev database's baseline ModelConfig/Plan rows (Demo, Basic,
Advanced, Full, and the 6 models they reference) onto whichever database
this command is run against - these were created ad-hoc through the admin
UI locally, which never syncs to another database on its own (a git push
moves code, never rows).

Kept as a management command, not a data migration - a migration runs
automatically as part of every `manage.py test` database build too, which
seeded these rows into the test DB and broke dozens of tests written
assuming an empty Plan/ModelConfig table at test start. A command only
runs when explicitly invoked.

Defaults to a dry run (prints what would be created/left alone, writes
nothing). Pass --apply to actually save. Idempotent either way - every
row is matched by its natural key (provider+model_name for ModelConfig,
name for Plan), so re-running after --apply just reports "already
exists" for everything with no changes.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

MODEL_CONFIGS = [
    {
        "provider": "openai",
        "model_name": "gpt-4o-mini",
        "display_name": "Insights- Model",
        "tier": "default",
        "input_cost_per_1m": Decimal("0.1500"),
        "output_cost_per_1m": Decimal("0.6000"),
        "is_enabled": True,
    },
    {
        "provider": "openai",
        "model_name": "m",
        "display_name": "",
        "tier": "default",
        "input_cost_per_1m": None,
        "output_cost_per_1m": None,
        "is_enabled": False,
    },
    {
        "provider": "openai",
        "model_name": "sora-2",
        "display_name": "Insights-Sora-Model",
        "tier": "default",
        "input_cost_per_1m": None,
        "output_cost_per_1m": None,
        "is_enabled": True,
    },
    {
        "provider": "openai",
        "model_name": "gpt-3.5-turbo",
        "display_name": "GPT-3.5 Turbo",
        "tier": "economy",
        "input_cost_per_1m": Decimal("0.5000"),
        "output_cost_per_1m": Decimal("1.5000"),
        "is_enabled": True,
    },
    {
        "provider": "openai",
        "model_name": "gpt-4o",
        "display_name": "GPT-4o",
        "tier": "premium",
        "input_cost_per_1m": Decimal("2.5000"),
        "output_cost_per_1m": Decimal("10.0000"),
        "is_enabled": True,
    },
    {
        "provider": "openai",
        "model_name": "gpt-5",
        "display_name": "GPT-5",
        "tier": "premium",
        "input_cost_per_1m": Decimal("5.0000"),
        "output_cost_per_1m": Decimal("15.0000"),
        "is_enabled": True,
    },
]

PLANS = [
    {
        "name": "Demo",
        "description": "",
        "is_demo": True,
        "demo_duration_days": 7,
        "daily_token_limit": 5000,
        "monthly_token_limit": 50000,
        "messages_per_session_limit": 20,
        "sessions_per_day_limit": 3,
        "monthly_budget_cap": Decimal("2.00"),
        "max_requests_per_period": 10,
        "period": "day",
        "max_context_tokens": 4000,
        "auto_upgrade_threshold_spend": None,
        "feature_flags": {
            "file_upload": False,
            "export": False,
            "priority_routing": False,
            "tools": False,
            "priority_queue": False,
            "long_context": False,
            "model_selection": True,
        },
        "is_active": True,
        "is_default": True,
        "is_visible_to_admins": True,
        "allowed_models": ["gpt-3.5-turbo"],
    },
    {
        "name": "Basic",
        "description": "",
        "is_demo": False,
        "demo_duration_days": None,
        "daily_token_limit": 50000,
        "monthly_token_limit": 1000000,
        "messages_per_session_limit": 100,
        "sessions_per_day_limit": 10,
        "monthly_budget_cap": Decimal("20.00"),
        "max_requests_per_period": 50,
        "period": "day",
        "max_context_tokens": 16000,
        "auto_upgrade_threshold_spend": None,
        "feature_flags": {
            "file_upload": True,
            "export": True,
            "priority_routing": False,
            "tools": False,
            "priority_queue": False,
            "long_context": False,
            "model_selection": True,
        },
        "is_active": True,
        "is_default": False,
        "is_visible_to_admins": True,
        "allowed_models": ["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o"],
    },
    {
        "name": "Advanced",
        "description": "",
        "is_demo": False,
        "demo_duration_days": None,
        "daily_token_limit": 200000,
        "monthly_token_limit": None,
        "messages_per_session_limit": None,
        "sessions_per_day_limit": None,
        "monthly_budget_cap": Decimal("50.00"),
        "max_requests_per_period": 200,
        "period": "day",
        "max_context_tokens": 64000,
        "auto_upgrade_threshold_spend": None,
        "feature_flags": {
            "file_upload": True,
            "export": True,
            "priority_routing": True,
            "tools": True,
            "priority_queue": False,
            "long_context": True,
            "model_selection": True,
        },
        "is_active": True,
        "is_default": False,
        "is_visible_to_admins": True,
        "allowed_models": ["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o", "gpt-5"],
    },
    {
        "name": "Full",
        "description": (
            "Every available model, no request-count/context ceiling that matters in " "practice, full feature set."
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
        "auto_upgrade_threshold_spend": None,
        "feature_flags": {
            "file_upload": True,
            "export": True,
            "priority_routing": True,
            "tools": True,
            "priority_queue": True,
            "long_context": True,
            "model_selection": True,
        },
        "is_active": True,
        "is_default": False,
        "is_visible_to_admins": True,
        "allowed_models": ["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o", "gpt-5"],
    },
]


class Command(BaseCommand):
    help = "Seed the baseline Demo/Basic/Advanced/Full plans and their models (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually write changes (default: dry run).")

    def handle(self, *args, **options):
        from chat.models import ModelConfig
        from governance.models import Plan

        apply = options["apply"]
        model_by_name = {}

        self.stdout.write("== ModelConfig ==")
        with transaction.atomic():
            for cfg in MODEL_CONFIGS:
                model_name = cfg["model_name"]
                existing = ModelConfig.objects.filter(provider=cfg["provider"], model_name=model_name).first()
                if existing:
                    self.stdout.write(f"  already exists: {cfg['provider']}/{model_name}")
                    model_by_name[model_name] = existing
                    continue
                self.stdout.write(f"  {'creating' if apply else 'would create'}: {cfg['provider']}/{model_name}")
                if apply:
                    model_by_name[model_name] = ModelConfig.objects.create(
                        provider=cfg["provider"],
                        model_name=model_name,
                        **{k: v for k, v in cfg.items() if k not in ("provider", "model_name")},
                    )
            if not apply:
                transaction.set_rollback(True)

        self.stdout.write("== Plan ==")
        with transaction.atomic():
            for plan_data in PLANS:
                plan_data = dict(plan_data)
                allowed_model_names = plan_data.pop("allowed_models")
                name = plan_data.pop("name")
                existing = Plan.objects.filter(name=name).first()
                if existing:
                    self.stdout.write(f"  already exists: {name}")
                    continue
                self.stdout.write(f"  {'creating' if apply else 'would create'}: {name}")
                if apply:
                    plan = Plan.objects.create(name=name, **plan_data)
                    plan.allowed_models.set([model_by_name[m] for m in allowed_model_names if m in model_by_name])
            if not apply:
                transaction.set_rollback(True)

        if not apply:
            self.stdout.write(self.style.WARNING("Dry run - nothing written. Re-run with --apply to save."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
