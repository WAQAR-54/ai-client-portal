"""Global Search / Command Palette (Ctrl+K) - the data half. Composes results from each app's OWN
already-authorized queryset/scoping helper (chat's own conversation filter, governance's `_scope_users`,
billing's `scoped_invoices`, governance's media_service.list_items) rather than building a new
cross-model authorization layer - the same architectural point governance's existing admin-only
`global_search` (governance/views.py) already makes for its own narrower scope. No new permission
rule is introduced anywhere in this file; every category either reuses an existing scoped queryset
verbatim or is skipped entirely when the user doesn't hold the feature/role that already gates it.

The static "commands" half (Go to Dashboard, etc.) is rendered separately, straight into
templates/base.html, since which commands are visible is a small, fixed, permission-filtered list
that never needs a server round-trip - only the data categories below hit this view.
"""

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.http import require_GET

from chat.models import Conversation, Project
from governance.features import require_feature, user_has_feature

RECENT_CONVERSATIONS_LIMIT = 6
RESULTS_PER_CATEGORY = 10


@login_required
@require_feature("quick_switcher")
@require_GET
def global_search(request):
    """Backs the Ctrl+K command palette's data results. Gated on the same `quick_switcher` role
    feature the palette itself is gated on everywhere else (base.html, profile.html) - a role with
    the palette turned off never reaches this view at all, matching require_feature's own
    real-access-control (not just hidden-nav-item) contract."""
    query = request.GET.get("q", "").strip()
    context = {"query": query}

    if not query:
        context["recent_conversations"] = Conversation.objects.filter(user=request.user).order_by("-updated_at")[
            :RECENT_CONVERSATIONS_LIMIT
        ]
        return render(request, "search/_global_search_results.html", context)

    if len(query) < 2:
        return render(request, "search/_global_search_results.html", context)

    context["conversations"] = Conversation.objects.filter(user=request.user, title__icontains=query).order_by(
        "-updated_at"
    )[:RESULTS_PER_CATEGORY]

    if user_has_feature(request.user, "projects"):
        context["projects"] = Project.objects.filter(user=request.user, name__icontains=query).order_by("name")[
            :RESULTS_PER_CATEGORY
        ]

    if request.user.is_admin:
        from governance.views import _scope_users

        from accounts.models import User

        context["admin_users"] = _scope_users(
            request,
            User.objects.filter(
                Q(email__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query)
            ),
        ).order_by("email")[:RESULTS_PER_CATEGORY]

        from billing.views import scoped_invoices

        context["invoices"] = (
            scoped_invoices(request.user)
            .filter(Q(invoice_number__icontains=query) | Q(recipient_user__email__icontains=query))
            .select_related("recipient_user")
            .order_by("-issue_date", "-id")[:RESULTS_PER_CATEGORY]
        )
    else:
        from billing.models import Invoice

        context["invoices"] = Invoice.objects.filter(
            recipient_user=request.user, invoice_number__icontains=query
        ).order_by("-issue_date", "-id")[:RESULTS_PER_CATEGORY]

    if request.user.is_superadmin:
        from governance import media_service

        filters = {
            "q": query[:100],
            "owner": "",
            "source": "",
            "category": "",
            "ext": "",
            "date_from": None,
            "date_to": None,
            "size": "",
            "size_min": None,
            "size_max": None,
            # Orphans have no stable pk/URL to link to from a search result (token-based, scan-cache-
            # only) and aren't something a person searches for by name anyway - excluded, not just
            # capped, same reasoning governance's own media list gives its "referenced" filter.
            "reference": "referenced",
            "sort": "newest",
        }
        items, _total, _storage_error = media_service.list_items(filters, page=1, page_size=RESULTS_PER_CATEGORY)
        context["media_items"] = items

    return render(request, "search/_global_search_results.html", context)
