from django.utils import timezone
from django.utils.translation import gettext as _


def group_conversations(conversations):
    """Bucket an already `-updated_at`-ordered iterable of conversations into
    ChatGPT/Claude-style sidebar sections: Today, Yesterday, Previous 7 Days,
    Previous 30 Days, then one bucket per calendar month for anything older.
    Returns an ordered list of (label, [conversations]) tuples, newest-first
    within each bucket (relies on the caller's ordering)."""
    now = timezone.localtime()
    today = now.date()
    yesterday = today - timezone.timedelta(days=1)
    week_ago = today - timezone.timedelta(days=7)
    month_ago = today - timezone.timedelta(days=30)

    buckets = []
    bucket_index = {}

    def bucket_for(conversation):
        day = timezone.localtime(conversation.updated_at).date()
        if day == today:
            return _("Today")
        if day == yesterday:
            return _("Yesterday")
        if day > week_ago:
            return _("Previous 7 Days")
        if day > month_ago:
            return _("Previous 30 Days")
        # Older than 30 days: a literal month/year label (e.g. "March 2026").
        # Left in English regardless of UI language - localizing this would
        # need Django's own date-formatting machinery (strftime doesn't
        # respect the active language), and it's a rare case (only shows
        # once conversations are over a month old) not worth the added
        # complexity for this pass.
        return day.strftime("%B %Y")

    for conversation in conversations:
        label = bucket_for(conversation)
        if label not in bucket_index:
            bucket_index[label] = []
            buckets.append((label, bucket_index[label]))
        bucket_index[label].append(conversation)

    return buckets
