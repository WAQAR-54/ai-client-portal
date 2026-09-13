"""Data migration only - applies real capability numbers to plans named
exactly "Demo"/"Basic"/"Advanced"/"Full", matching the tiering the user's
own reference "Plan Capabilities & Limits" mockup asked for. Deliberately
a plain UPDATE keyed by exact name, never a CREATE - this must stay a
harmless no-op on any deployment that doesn't already have plans with
these exact names (real pricing/plan data is not something to invent on
someone else's production database), and is fully re-editable afterward
from the Capability Limits and Plan Management admin pages this session
also shipped.

Tiering applied (image/document reads, Grok media generation - the
mockup's own suggested numbers; "research" here is on/off only, since
this app doesn't have a numeric per-month research cap the way the
mockup's own column suggested - see this migration's own note below):

              file_upload  doc_gen  research  agent_mode  image/mo  doc/mo  media/mo  self_checkout
Demo          on           off      off       off         10        5       0         (n/a - demo)
Basic         on           on       off       off         100       50      0         on
Advanced      on           on       on        on          500       300     30        off (contact us)
Full          on           on       on        on          None      None    150       off (contact us)

Advanced/Full are self_checkout_enabled=False - per the user's own
explicit fix for a real gap flagged this round ("lowest tier self-serve,
higher tiers should be a sales conversation instead of instant checkout")
- picking either from the Plans page creates an Upgrade Request instead
of an instant invoice.
"""

from django.db import migrations

_PLAN_CONFIG = {
    "Demo": {
        "flags": {
            "file_upload": True,
            "document_generation": False,
            "research": False,
            "media_generation": False,
            "agent_mode": False,
        },
        "monthly_image_reads_limit": 10,
        "monthly_document_reads_limit": 5,
        "monthly_media_generation_limit": 0,
        "self_checkout_enabled": True,
    },
    "Basic": {
        "flags": {
            "file_upload": True,
            "document_generation": True,
            "research": False,
            "media_generation": False,
            "agent_mode": False,
        },
        "monthly_image_reads_limit": 100,
        "monthly_document_reads_limit": 50,
        "monthly_media_generation_limit": 0,
        "self_checkout_enabled": True,
    },
    "Advanced": {
        "flags": {
            "file_upload": True,
            "document_generation": True,
            "research": True,
            "media_generation": True,
            "agent_mode": True,
        },
        "monthly_image_reads_limit": 500,
        "monthly_document_reads_limit": 300,
        "monthly_media_generation_limit": 30,
        "self_checkout_enabled": False,
    },
    "Full": {
        "flags": {
            "file_upload": True,
            "document_generation": True,
            "research": True,
            "media_generation": True,
            "agent_mode": True,
        },
        "monthly_image_reads_limit": None,
        "monthly_document_reads_limit": None,
        "monthly_media_generation_limit": 150,
        "self_checkout_enabled": False,
    },
}


def configure_known_plans(apps, schema_editor):
    Plan = apps.get_model("governance", "Plan")
    ProviderModel = apps.get_model("providers", "ProviderModel")
    anthropic_model = ProviderModel.objects.filter(provider__adapter_type="anthropic", is_enabled=True).first()
    grok_model = ProviderModel.objects.filter(provider__slug="grok", is_enabled=True).first()

    for name, config in _PLAN_CONFIG.items():
        plan = Plan.objects.filter(name=name).first()
        if plan is None:
            continue
        plan.feature_flags = {**plan.feature_flags, **config["flags"]}
        plan.monthly_image_reads_limit = config["monthly_image_reads_limit"]
        plan.monthly_document_reads_limit = config["monthly_document_reads_limit"]
        plan.monthly_media_generation_limit = config["monthly_media_generation_limit"]
        plan.self_checkout_enabled = config["self_checkout_enabled"]
        plan.save()

        # Same auto-enable-the-required-model behavior as the admin pages
        # (governance/views.py::_auto_enabled_provider_model_ids) - a plan
        # configured here with research/media_generation on must actually
        # be able to use them.
        if config["flags"].get("research") and anthropic_model:
            plan.allowed_provider_models.add(anthropic_model)
        if config["monthly_media_generation_limit"] and grok_model:
            plan.allowed_provider_models.add(grok_model)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("governance", "0028_plan_self_checkout_enabled"),
        ("providers", "0011_providermodel_supports_vision"),
    ]

    operations = [
        migrations.RunPython(configure_known_plans, noop_reverse),
    ]
