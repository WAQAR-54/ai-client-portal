"""Step 1 of the ModelConfig -> providers.ProviderModel migration
(expand-migrate-contract - see governance.models.Plan.allowed_provider_
models and chat.models.Message.provider_model_used, the parallel fields
this command populates).

Populates the NEW parallel fields from the OLD ones - does NOT touch or
remove chat.models.ModelConfig or the old allowed_models/model_used
fields, and does NOT change chat/router.py or chat/views.py, which keep
reading the old fields exclusively until step 3 of the migration cuts
them over (see the module docstring on chat/providers.py's transitional
get_provider() string branch). Safe to run against production at any
point before that cutover: every write here lands on a field nothing
else reads yet.

Every referenced ModelConfig row gets a ProviderModel counterpart
(matched by provider+model_id if one already exists, created otherwise)
with is_enabled/tier/pricing carried over EXACTLY as they are on
ModelConfig - never reset, unlike a fresh provider sync's own new-model
default of is_enabled=False. This is existing admin state being carried
forward, not a new discovery, and grandfathering requires nobody's
effective access to shift as a side effect of this migration.

The dangerous case this handles explicitly: a ModelConfig whose
`provider` value doesn't match any seeded Provider row (currently
impossible given ModelConfig.Provider is a closed choice of exactly
openai/anthropic and both are always seeded - see providers/migrations/
0002_seed_providers.py - but checked and reported rather than assumed).
A model already retired at the provider itself (its API no longer lists
it) is NOT a problem here either way: this command creates its
ProviderModel from ModelConfig's own historical record regardless of
whether a live sync would still find it, so history keeps resolving.

Defaults to a dry run (prints the full mapping, writes nothing). Pass
--apply to actually save. Idempotent - re-running after --apply just
reports "already mapped" for everything, no duplicate ProviderModel
rows and no changes to the parallel fields' already-correct contents.
"""

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Step 1: populate Plan.allowed_provider_models / Message.provider_model_used "
        "from chat.models.ModelConfig (dry run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually write changes (default: dry run).")

    def handle(self, *args, **options):
        from chat.models import Message, ModelConfig
        from governance.models import Plan
        from providers.models import Provider, ProviderModel

        apply = options["apply"]
        self.stdout.write(self.style.WARNING("DRY RUN - no writes will happen" if not apply else "APPLYING CHANGES"))
        self.stdout.write("")

        plan_model_ids = set(Plan.objects.values_list("allowed_models__id", flat=True)) - {None}
        message_model_ids = set(Message.objects.values_list("model_used_id", flat=True)) - {None}
        referenced_ids = plan_model_ids | message_model_ids
        all_model_configs = ModelConfig.objects.filter(id__in=referenced_ids).order_by("provider", "model_name")

        self.stdout.write(f"ModelConfig values referenced by Plan.allowed_models: {len(plan_model_ids)}")
        self.stdout.write(f"Distinct ModelConfig values referenced by Message.model_used: {len(message_model_ids)}")
        self.stdout.write(f"Union needing a ProviderModel mapping: {all_model_configs.count()}")
        self.stdout.write("")

        self.stdout.write("== ModelConfig -> ProviderModel mapping ==")
        # ModelConfig.id -> ProviderModel: the real, saved object once one
        # exists (already did, or apply=True just created it); None means
        # "would be created here on --apply" so dry-run reporting below can
        # still show what WOULD happen, not a false "UNMAPPED".
        mapping = {}
        pending_creates = {}  # ModelConfig.id -> (provider_row, kwargs) for dry-run display only
        unmapped = []

        with transaction.atomic():
            for mc in all_model_configs:
                provider_row = Provider.objects.filter(slug=mc.provider).first()
                if provider_row is None:
                    unmapped.append(mc)
                    self.stdout.write(
                        self.style.ERROR(
                            f"  NO PROVIDER MATCH: ModelConfig(id={mc.id}, provider={mc.provider!r}, "
                            f"model_name={mc.model_name!r}) - skipped, will NOT be mapped."
                        )
                    )
                    continue

                existing = ProviderModel.objects.filter(provider=provider_row, model_id=mc.model_name).first()
                if existing:
                    self.stdout.write(
                        f"  already mapped: ModelConfig(provider={mc.provider}, model_name={mc.model_name!r}) "
                        f"-> ProviderModel(id={existing.id}, is_enabled={existing.is_enabled})"
                    )
                    mapping[mc.id] = existing
                    continue

                create_kwargs = {
                    "provider": provider_row,
                    "model_id": mc.model_name,
                    "display_name": mc.display_name,
                    "tier": mc.tier,
                    # Carried over exactly, not reset - existing admin
                    # state, not a fresh discovery (grandfathering).
                    "is_enabled": mc.is_enabled,
                    "is_new": False,
                    "is_retired": False,
                    "input_price_per_mtok": mc.input_cost_per_1m,
                    "output_price_per_mtok": mc.output_cost_per_1m,
                }
                self.stdout.write(
                    f"  {'creating' if apply else 'would create'}: ModelConfig(provider={mc.provider}, "
                    f"model_name={mc.model_name!r}, is_enabled={mc.is_enabled}, tier={mc.tier}) -> "
                    f"ProviderModel(provider={provider_row.name}, model_id={mc.model_name!r}, "
                    f"is_enabled={mc.is_enabled})"
                )
                if apply:
                    mapping[mc.id] = ProviderModel.objects.create(**create_kwargs)
                else:
                    pending_creates[mc.id] = create_kwargs

            self.stdout.write("")
            self.stdout.write("== Plan.allowed_provider_models ==")
            plans_affected = 0
            for plan in Plan.objects.all().order_by("name"):
                old_qs = plan.allowed_models.all()
                old_ids = list(old_qs.values_list("id", flat=True))
                if not old_ids:
                    continue
                plans_affected += 1
                old_names = list(old_qs.values_list("model_name", flat=True))
                new_names = []
                for mc_id in old_ids:
                    if mc_id in mapping:
                        new_names.append(mapping[mc_id].model_id)
                    elif mc_id in pending_creates:
                        new_names.append(f"{pending_creates[mc_id]['model_id']} (pending)")
                    else:
                        new_names.append("UNMAPPED")
                self.stdout.write(f"  {plan.name}: {old_names} -> {new_names}")
                if apply:
                    pms = [mapping[mc_id] for mc_id in old_ids if mc_id in mapping]
                    plan.allowed_provider_models.set(pms)
            self.stdout.write(f"Plans affected: {plans_affected}")

            self.stdout.write("")
            self.stdout.write("== Message.provider_model_used ==")
            messages_with_model = Message.objects.filter(model_used__isnull=False)
            message_count = messages_with_model.count()
            self.stdout.write(f"Messages with model_used set: {message_count}")
            distinct_used = sorted(message_model_ids)
            for mc_id in distinct_used:
                count = messages_with_model.filter(model_used_id=mc_id).count()
                if mc_id in mapping:
                    target = f"ProviderModel(id={mapping[mc_id].id}, model_id={mapping[mc_id].model_id!r})"
                elif mc_id in pending_creates:
                    target = f"{pending_creates[mc_id]['model_id']!r} (pending creation)"
                else:
                    target = "UNMAPPED"
                self.stdout.write(f"  ModelConfig id={mc_id}: {count} message(s) -> {target}")

            if apply:
                updated = 0
                for mc_id, pm in mapping.items():
                    updated += messages_with_model.filter(model_used_id=mc_id).update(provider_model_used=pm)
                self.stdout.write(f"  updated: {updated}")
                still_null = messages_with_model.filter(provider_model_used__isnull=True).count()
                if still_null:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  WARNING: {still_null} message(s) still have no provider_model_used - "
                            "see unmapped ModelConfig(s) above."
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.SUCCESS("  every message with model_used now has provider_model_used set.")
                    )

            if not apply:
                transaction.set_rollback(True)

        self.stdout.write("")
        if unmapped:
            self.stdout.write(
                self.style.ERROR(
                    f"{len(unmapped)} ModelConfig value(s) could not be mapped (no matching Provider) - see above."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("Every referenced ModelConfig value mapped to a ProviderModel."))

        if not apply:
            self.stdout.write(
                self.style.WARNING("Dry run complete - nothing was written. Re-run with --apply to save.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("Applied."))
