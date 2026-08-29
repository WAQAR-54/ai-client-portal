from django.db import migrations

PLAN_SEED = [
    {
        "name": "Demo",
        "description": "7-day trial with the cheapest model and low caps. Auto-assigned to every new user.",
        "is_demo": True,
        "demo_duration_days": 7,
        "daily_token_limit": 5000,
        "monthly_token_limit": 50000,
        "messages_per_session_limit": 20,
        "sessions_per_day_limit": 3,
        "monthly_budget_cap": "2.00",
        "feature_flags": {"file_upload": False, "export": False, "priority_routing": False},
        "is_active": True,
        "is_default": True,
        "is_visible_to_admins": True,
        # Cheapest available model only. Preference order matches the
        # provider's own naming for a low-cost tier; falls back gracefully
        # if none of these exist yet in this environment (see apply_seed).
        "model_names": ["gpt-3.5-turbo", "gpt-4o-mini", "claude-3-5-haiku-20241022"],
    },
    {
        "name": "Standard",
        "description": "Mid-tier plan for confirmed regular users.",
        "is_demo": False,
        "demo_duration_days": None,
        "daily_token_limit": 50000,
        "monthly_token_limit": 1000000,
        "messages_per_session_limit": 100,
        "sessions_per_day_limit": 10,
        "monthly_budget_cap": "20.00",
        "feature_flags": {"file_upload": True, "export": True, "priority_routing": False},
        "is_active": True,
        "is_default": False,
        "is_visible_to_admins": True,
        "model_names": ["gpt-3.5-turbo", "gpt-4o-mini", "gpt-4o", "claude-3-5-haiku-20241022", "claude-sonnet-4-5"],
    },
    {
        "name": "Premium",
        "description": "Flagship models, no session caps, full feature set.",
        "is_demo": False,
        "demo_duration_days": None,
        "daily_token_limit": 200000,
        "monthly_token_limit": None,
        "messages_per_session_limit": None,
        "sessions_per_day_limit": None,
        "monthly_budget_cap": "50.00",
        "feature_flags": {"file_upload": True, "export": True, "priority_routing": True},
        "is_active": True,
        "is_default": False,
        "is_visible_to_admins": True,
        "model_names": [],  # filled in with every currently-enabled model, see apply_seed
    },
]


def apply_seed(apps, schema_editor):
    Plan = apps.get_model("governance", "Plan")
    ModelConfig = apps.get_model("chat", "ModelConfig")
    UserPlanAssignment = apps.get_model("governance", "UserPlanAssignment")
    User = apps.get_model("accounts", "User")

    all_enabled_names = set(ModelConfig.objects.filter(is_enabled=True).values_list("model_name", flat=True))

    plans_by_name = {}
    for spec in PLAN_SEED:
        model_names = spec["model_names"] or list(all_enabled_names)
        plan, _ = Plan.objects.update_or_create(
            name=spec["name"],
            defaults={
                "description": spec["description"],
                "is_demo": spec["is_demo"],
                "demo_duration_days": spec["demo_duration_days"],
                "daily_token_limit": spec["daily_token_limit"],
                "monthly_token_limit": spec["monthly_token_limit"],
                "messages_per_session_limit": spec["messages_per_session_limit"],
                "sessions_per_day_limit": spec["sessions_per_day_limit"],
                "monthly_budget_cap": spec["monthly_budget_cap"],
                "feature_flags": spec["feature_flags"],
                "is_active": spec["is_active"],
                "is_default": spec["is_default"],
                "is_visible_to_admins": spec["is_visible_to_admins"],
            },
        )
        matched = ModelConfig.objects.filter(model_name__in=model_names)
        plan.allowed_models.set(matched)
        plans_by_name[spec["name"]] = plan

    # Backfill: every user that predates this migration gets a plan
    # assignment too (so they show up correctly in the admin UI instead of
    # "no plan"), but explicitly NOT the default (Demo) plan - that plan's
    # low caps and 7-day countdown are meant for brand-new signups, not for
    # silently capping an already-active account with zero warning. Premium
    # is the closest match to "no limit at all", which is what these
    # accounts actually had before Plans existed.
    grandfather_plan = plans_by_name.get("Premium")
    if grandfather_plan:
        for user in User.objects.all():
            UserPlanAssignment.objects.get_or_create(user=user, defaults={"plan": grandfather_plan})


def reverse_seed(apps, schema_editor):
    Plan = apps.get_model("governance", "Plan")
    Plan.objects.filter(name__in=[spec["name"] for spec in PLAN_SEED]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("governance", "0004_plan_upgraderequest_userplanassignment"),
        ("chat", "0003_conversation_deleted_at_conversation_is_deleted_and_more"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(apply_seed, reverse_seed),
    ]
