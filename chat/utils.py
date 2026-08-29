from django.utils import timezone


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
            return "Today"
        if day == yesterday:
            return "Yesterday"
        if day > week_ago:
            return "Previous 7 Days"
        if day > month_ago:
            return "Previous 30 Days"
        return day.strftime("%B %Y")

    for conversation in conversations:
        label = bucket_for(conversation)
        if label not in bucket_index:
            bucket_index[label] = []
            buckets.append((label, bucket_index[label]))
        bucket_index[label].append(conversation)

    return buckets
