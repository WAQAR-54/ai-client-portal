from django.db import migrations
from django.db.models import OuterRef, Subquery


def backfill(apps, schema_editor):
    """One ORM UPDATE via a correlated subquery - no per-row Python loop, no
    N+1. Conversations with no assistant reply that ever used a
    provider_model_used (legacy model_used-only history, or a conversation
    that never got a reply) are left with last_provider_model=None, same as
    a brand new conversation."""
    Conversation = apps.get_model("chat", "Conversation")
    Message = apps.get_model("chat", "Message")

    latest_provider_model = (
        Message.objects.filter(
            conversation=OuterRef("pk"),
            role="assistant",
            provider_model_used__isnull=False,
        )
        .order_by("-created_at")
        .values("provider_model_used_id")[:1]
    )
    Conversation.objects.update(last_provider_model_id=Subquery(latest_provider_model))


def reverse(apps, schema_editor):
    Conversation = apps.get_model("chat", "Conversation")
    Conversation.objects.update(last_provider_model_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0018_conversation_last_provider_model"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse),
    ]
