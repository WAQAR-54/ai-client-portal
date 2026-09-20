"""SuperAdmin-only Server Media screens (see governance/media_service.py for the rules).

A media browser must never become a backdoor to private files: every view here is SuperAdmin-only
on the server, a file is addressed by (source, primary key) and looked up through the database,
never by a path, and the only mutation offered is deleting a file no record refers to.
"""

import re
from datetime import date, datetime, timezone as dt_timezone

from django.contrib import messages as django_messages
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from accounts.models import User
from accounts.permissions import SuperAdminRequiredMixin, role_required
from governance import media_service as media
from governance.models import AuditLog

DELETE_CONFIRMATION_WORD = "DELETE"
RESCAN_COOLDOWN_SECONDS = 30
AUDIT_DEDUPE_SECONDS = 300
VIEWS = ("files", "orphans", "missing")
SIZE_FILTERS = {"1mb": 1024**2, "5mb": 5 * 1024**2, "10mb": 10 * 1024**2, "50mb": 50 * 1024**2}
IMAGE_CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}
SAFE_EXT = re.compile(r"^[a-z0-9]{1,10}$")


def _audit(actor, action_type, target_id, old_value="", new_value=""):
    """Media has no model of its own, so the audit row names the item by source:pk (or a file-name
    digest) and records size/category only - never a file name, path or content."""
    AuditLog.objects.create(
        actor=actor,
        action_type=action_type,
        target_type="MediaFile",
        target_id=str(target_id)[:100],
        old_value=str(old_value),
        new_value=str(new_value),
    )


def _audit_once(actor, action_type, target_id, new_value=""):
    """Looking at or downloading a private file is worth a record, but not one per click: the same
    actor doing the same thing to the same item inside a few minutes is written once."""
    key = f"media:audit:{actor.pk}:{action_type}:{target_id}"
    try:
        if not cache.add(key, 1, AUDIT_DEDUPE_SECONDS):
            return
    except Exception:  # noqa: BLE001 - no cache: audit every time rather than skip the record
        pass
    _audit(actor, action_type, target_id, new_value=new_value)


def _parse_date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _filters_from_request(request):
    get = request.GET
    source = get.get("source", "")
    category = get.get("category", "")
    ext = get.get("ext", "").strip().lower().lstrip(".")
    return {
        "q": get.get("q", "").strip()[:100],
        "owner": get.get("owner", "").strip()[:100],
        "source": source if source in media.SOURCES else "",
        "category": category if category in media.CATEGORIES else "",
        "ext": ext if SAFE_EXT.match(ext) else "",
        "date_from": _parse_date(get.get("date_from", "")),
        "date_to": _parse_date(get.get("date_to", "")),
        "size": get.get("size", "") if get.get("size", "") in SIZE_FILTERS else "",
        "size_min": SIZE_FILTERS.get(get.get("size", "")),
    }


def _with_times(entries):
    """Scan entries carry a POSIX mtime and a storage-relative name; the template wants a datetime
    and a display-safe name (the storage-relative path itself is never sent to the page)."""
    return [
        {
            **entry,
            "when": datetime.fromtimestamp(entry["mtime"], tz=dt_timezone.utc),
            "display": media.safe_display_name(entry["name"]),
            "category": media.category_of(entry["name"]),
        }
        for entry in entries
    ]


def _parse_iso(raw):
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _page_number(request):
    try:
        return max(1, int(request.GET.get("page", "1")))
    except ValueError:
        return 1


def _querystring_without_page(request):
    qd = request.GET.copy()
    qd.pop("page", None)
    return qd.urlencode()


class MediaDashboardView(SuperAdminRequiredMixin, TemplateView):
    template_name = "governance/media.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        view = request.GET.get("view", "files")
        view = view if view in VIEWS else "files"
        scan = media.get_scan()
        filters = _filters_from_request(request)
        page = _page_number(request)
        items, total, storage_error = [], 0, None
        if view == "files":
            items, total, storage_error = media.list_items(filters, page)
        page_size = media.PAGE_SIZE
        pages = max(1, -(-total // page_size))
        disk = media.disk_usage()
        context.update(
            {
                "view": view,
                "items": items,
                "total": total,
                "page": page,
                "pages": pages,
                "has_previous": page > 1,
                "has_next": page < pages and page < media.MAX_PAGE,
                "querystring_without_page": _querystring_without_page(request),
                "querystring_without_view": self._querystring_without(request, "view", "page"),
                "filters": filters,
                "raw": request.GET,
                "storage_error": storage_error or scan.get("error"),
                "scan": scan,
                "disk": disk,
                "sources": [(s, media.SOURCE_LABELS[s]) for s in media.SOURCES],
                "categories": media.CATEGORIES,
                "size_filters": list(SIZE_FILTERS),
                "recent": _with_times(scan.get("recent", [])),
                "large": _with_times(scan.get("large", [])),
                "scanned_at": _parse_iso(scan.get("scanned_at")),
                "orphans": _with_times(scan.get("orphans", [])) if view == "orphans" else [],
                "missing": (
                    [
                        {"display": media.safe_display_name(n), "category": media.category_of(n)}
                        for n in scan.get("missing", [])
                    ]
                    if view == "missing"
                    else []
                ),
                "delete_word": DELETE_CONFIRMATION_WORD,
            }
        )
        return context

    @staticmethod
    def _querystring_without(request, *keys):
        qd = request.GET.copy()
        for key in keys:
            qd.pop(key, None)
        return qd.urlencode()


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["POST"])
def media_rescan(request):
    """Re-walk the storage now. Rate-limited so the walk cannot be used to keep the disk busy."""
    try:
        allowed = cache.add("media:rescan:cooldown", 1, RESCAN_COOLDOWN_SECONDS)
    except Exception:  # noqa: BLE001
        allowed = True
    if allowed:
        media.get_scan(force=True)
        django_messages.success(request, _("Storage scanned."))
    else:
        django_messages.warning(request, _("A scan just ran. Wait a moment before scanning again."))
    return redirect("governance:media")


def _item_or_404(source, pk):
    item = media.get_item(source, pk)
    if item is None or item.state == "missing":
        raise Http404
    return item


def _hardened(response):
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    response["Cache-Control"] = "private, no-store"
    return response


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["GET"])
def media_download(request, source, pk):
    """The file, as an attachment and as opaque bytes, whatever its extension claims."""
    item = _item_or_404(source, pk)
    if not item.is_public:
        _audit_once(request.user, "media_download", f"{item.source}:{item.pk}", f"{item.category} {item.size or 0}B")
    response = FileResponse(
        default_storage.open(item.relative_name, "rb"),
        as_attachment=True,
        filename=item.display_name,
        content_type="application/octet-stream",
    )
    return _hardened(response)


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["GET"])
def media_preview(request, source, pk):
    """Inline view for the few formats that cannot run code in a browser, and only after the first
    bytes confirm the file is what its extension says. Everything else is download-only."""
    item = _item_or_404(source, pk)
    head = media.read_head(item)
    kind = media.sniff_preview_kind(item, head)
    if kind is None:
        return HttpResponse(
            _("This file cannot be previewed safely. Download it instead."), status=415, content_type="text/plain"
        )
    if not item.is_public:
        _audit_once(request.user, "media_preview", f"{item.source}:{item.pk}", f"{item.category} {item.size or 0}B")
    if kind == "text":
        text = head.decode("utf-8", errors="replace")
        if (item.size or 0) > media.TEXT_PREVIEW_BYTES:
            text += "\n\n… (preview truncated)"
        return _hardened(HttpResponse(text, content_type="text/plain; charset=utf-8"))
    content_type = "application/pdf" if kind == "pdf" else IMAGE_CONTENT_TYPES[item.ext]
    response = FileResponse(default_storage.open(item.relative_name, "rb"), content_type=content_type)
    response["Content-Disposition"] = 'inline; filename="preview"'
    return _hardened(response)


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["POST"])
def media_delete_orphan(request):
    """Delete ONE unreferenced file, after a typed confirmation. Never a bulk action, never a file
    that any record refers to: media_service.delete_orphan re-checks all of that at deletion time."""
    orphans_url = f"{reverse('governance:media')}?view=orphans"
    if request.POST.get("typed_word", "").strip() != DELETE_CONFIRMATION_WORD:
        django_messages.error(request, _("Type %(word)s to confirm the deletion.") % {"word": DELETE_CONFIRMATION_WORD})
        return redirect(orphans_url)
    name = media.orphan_name_for(request.POST.get("file", ""))
    if name is None:
        django_messages.error(request, _("That file is not in the orphan list. Scan again and retry."))
        return redirect(orphans_url)
    status, size = media.delete_orphan(name)
    digest = media.name_digest(name)
    if status == media.DELETE_OK:
        _audit(request.user, "media_orphan_deleted", digest, old_value=f"{media.category_of(name)} {size}B")
        django_messages.success(request, _("File deleted."))
    elif status == media.DELETE_REFERENCED:
        _audit(request.user, "media_delete_blocked", digest, new_value="still referenced")
        django_messages.error(request, _("That file is still in use by a record, so it was not deleted."))
    elif status == media.DELETE_MISSING:
        django_messages.warning(request, _("That file is already gone."))
    else:
        django_messages.error(request, _("That file name is not valid, so nothing was deleted."))
    return redirect(orphans_url)
