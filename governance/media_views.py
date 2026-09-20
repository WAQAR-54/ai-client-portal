"""SuperAdmin-only Server Media screens (see governance/media_service.py for the rules).

A media browser must never become a backdoor to private files: every view here is SuperAdmin-only
on the server, a file is addressed by (source, primary key) - or, for a file no record refers to, by
an opaque digest of the scan - and never by a path, and the only mutation offered is deleting a file
no record refers to. Viewing a file never changes who can see it.
"""

import logging
import re
from datetime import date, datetime, timezone as dt_timezone
from urllib.parse import urlencode

from django.contrib import messages as django_messages
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from accounts.models import User
from accounts.permissions import SuperAdminRequiredMixin, role_required
from governance import media_service as media
from governance.models import AuditLog

logger = logging.getLogger(__name__)

RESCAN_COOLDOWN_SECONDS = 30
AUDIT_DEDUPE_SECONDS = 300
VIEWS = ("files", "missing")
IMAGE_CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}
SAFE_EXT = re.compile(r"^[a-z0-9]{1,10}$")
# The only query parameters the list understands - also the only ones a "back to the list" link keeps.
LIST_PARAMS = ("q", "owner", "source", "category", "ext", "size", "reference", "sort", "date_from", "date_to", "page")


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


def _audit_id(item):
    return f"orphan:{item.token}" if item.is_orphan else f"{item.source}:{item.pk}"


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
    sort = get.get("sort", "")
    reference = get.get("reference", "")
    if request.GET.get("view") == "orphans":  # the old "Orphan candidates" tab is now a filter
        reference = "orphan"
    size_key = get.get("size", "")
    size = next((o for o in media.size_options() if o[0] == size_key), None)
    return {
        "q": get.get("q", "").strip()[:100],
        "owner": get.get("owner", "").strip()[:100],
        "source": source if source in media.SOURCES else "",
        "category": category if category in media.CATEGORIES else "",
        "ext": ext if SAFE_EXT.match(ext) else "",
        "date_from": _parse_date(get.get("date_from", "")),
        "date_to": _parse_date(get.get("date_to", "")),
        "size": size[0] if size else "",
        "size_min": size[2] if size else None,
        "size_max": size[3] if size else None,
        "reference": reference if reference in media.REFERENCE_FILTERS else "",
        "sort": sort if sort in media.SORTS else "newest",
    }


def _filters_active(filters):
    return (
        any(
            filters[k] for k in ("q", "owner", "source", "category", "ext", "date_from", "date_to", "size", "reference")
        )
        or filters["sort"] != "newest"
    )


def _empty_kind(filters):
    """Which empty state fits: only orphan candidates asked for (none exist), some filter narrowing the
    list (nothing matches), or nothing stored at all."""
    others = any(filters[k] for k in ("q", "owner", "source", "category", "ext", "date_from", "date_to", "size"))
    if filters["reference"] == "orphan" and not others:
        return "orphans"
    return "filtered" if _filters_active(filters) else "none"


def _clean_params(source_params):
    """The list's own parameters only, with bounded values: safe to put back into a link."""
    return [(k, str(source_params.get(k))[:100]) for k in LIST_PARAMS if source_params.get(k)]


def _list_url(params=(), **changes):
    """URL of the media list keeping `params` (minus `page`) and applying `changes` ('' removes)."""
    merged = {k: v for k, v in params if k != "page"}
    for key, value in changes.items():
        if value in ("", None):
            merged.pop(key, None)
        else:
            merged[key] = str(value)
    query = urlencode(merged)
    return reverse("governance:media") + (f"?{query}" if query else "")


def _back_url(raw_query):
    """The list URL a detail page or delete form came from, rebuilt from allow-listed parameters only
    (never a caller-supplied URL: that would be an open redirect)."""
    params = _clean_params(_qd(raw_query))
    return _list_url(params)


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


class MediaDashboardView(SuperAdminRequiredMixin, TemplateView):
    template_name = "governance/media.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        view = request.GET.get("view", "files")
        view = view if view in VIEWS else "files"
        scan = media.get_scan()
        filters = _filters_from_request(request)
        params = _clean_params(request.GET)
        page = _page_number(request)
        items, total, storage_error = [], 0, None
        if view == "files":
            items, total, storage_error = media.list_items(filters, page)
        pages = max(1, -(-total // media.PAGE_SIZE))
        has_previous, has_next = page > 1, page < pages and page < media.MAX_PAGE
        current = [(k, v) for k, v in params if k != "page"]
        context.update(
            {
                "view": view,
                "items": items,
                "total": total,
                "page": page,
                "pages": pages,
                "prev_url": _list_url(params, page=page - 1) if has_previous else "",
                "next_url": _list_url(params, page=page + 1) if has_next else "",
                "filters": filters,
                "raw": request.GET,
                "filters_active": _filters_active(filters),
                "empty_kind": _empty_kind(filters),
                "back_query": urlencode(_clean_params(request.GET)),
                "storage_error": storage_error or scan.get("error"),
                "scan": scan,
                "disk": media.disk_usage(),
                "sources": [(s, media.SOURCE_LABELS[s]) for s in media.SOURCES],
                "size_options": media.size_options(),
                "sort_options": [(s, media.SORT_LABELS[s]) for s in media.SORTS],
                "type_chips": [
                    {
                        "label": label,
                        "url": _list_url(current, category=value),
                        "active": filters["category"] == value,
                    }
                    for label, value in (
                        ("All", ""),
                        ("Images", "image"),
                        ("Documents", "document"),
                        ("Other", "other"),
                    )
                ],
                "orphans_url": _list_url([], reference="orphan"),
                "missing_url": _list_url([], view="missing"),
                "files_url": _list_url([]),
                "clear_url": reverse("governance:media"),
                "recent": _with_times(scan.get("recent", [])),
                "large": _with_times(scan.get("large", [])),
                "scanned_at": _parse_iso(scan.get("scanned_at")),
                "orphan_cap": len(scan.get("orphans", [])),
                "missing": (
                    [
                        {"display": media.safe_display_name(n), "category": media.category_of(n)}
                        for n in scan.get("missing", [])
                    ]
                    if view == "missing"
                    else []
                ),
            }
        )
        return context


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
    return redirect(_back_url(request.POST.get("back", "")))


def _item_or_404(source, pk):
    item = media.get_item(source, pk)
    if item is None or item.state == "missing":
        raise Http404
    return item


def _orphan_or_404(token):
    item = media.get_orphan_item(token)
    if item is None or item.state == "missing":
        raise Http404
    return item


def _hardened(response):
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    response["Cache-Control"] = "private, no-store"
    return response


def _storage_unavailable():
    return HttpResponse(_("The file could not be read from storage."), status=503, content_type="text/plain")


def _download_response(request, item):
    """The file, as an attachment and as opaque bytes, whatever its extension claims."""
    try:
        handle = default_storage.open(item.relative_name, "rb")
    except Exception:  # noqa: BLE001 - the raw storage error stays in the log, not on the page
        logger.exception("media download could not open a file")
        return _storage_unavailable()
    if not item.is_public:
        _audit_once(request.user, "media_download", _audit_id(item), f"{item.category} {item.size or 0}B")
    response = FileResponse(
        handle, as_attachment=True, filename=item.display_name, content_type="application/octet-stream"
    )
    return _hardened(response)


def _preview_response(request, item):
    """Inline view for the few formats that cannot run code in a browser, and only after the first
    bytes confirm the file is what its extension says. Everything else is download-only."""
    try:
        head = media.read_head(item)
    except Exception:  # noqa: BLE001
        logger.exception("media preview could not read a file")
        return _storage_unavailable()
    kind = media.sniff_preview_kind(item, head)
    if kind is None:
        return HttpResponse(
            _("This file cannot be previewed safely. Download it instead."), status=415, content_type="text/plain"
        )
    if not item.is_public:
        _audit_once(request.user, "media_preview", _audit_id(item), f"{item.category} {item.size or 0}B")
    if kind == "text":
        text = head.decode("utf-8", errors="replace")
        if (item.size or 0) > media.TEXT_PREVIEW_BYTES:
            text += "\n\n… (preview truncated)"
        return _hardened(HttpResponse(text, content_type="text/plain; charset=utf-8"))
    content_type = "application/pdf" if kind == "pdf" else IMAGE_CONTENT_TYPES[item.ext]
    try:
        handle = default_storage.open(item.relative_name, "rb")
    except Exception:  # noqa: BLE001
        logger.exception("media preview could not open a file")
        return _storage_unavailable()
    response = FileResponse(handle, content_type=content_type)
    response["Content-Disposition"] = 'inline; filename="preview"'
    return _hardened(response)


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["GET"])
def media_download(request, source, pk):
    return _download_response(request, _item_or_404(source, pk))


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["GET"])
def media_preview(request, source, pk):
    return _preview_response(request, _item_or_404(source, pk))


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["GET"])
def media_download_orphan(request, token):
    return _download_response(request, _orphan_or_404(token))


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["GET"])
def media_preview_orphan(request, token):
    return _preview_response(request, _orphan_or_404(token))


# ---------------------------------------------------------------------------------------------
# The "View" page: preview + facts about one file
# ---------------------------------------------------------------------------------------------
def _detail_response(request, item):
    """Renders what a SuperAdmin may know about ONE file. Image and PDF previews are separate requests
    to the hardened preview endpoint (an <img>/<iframe> pointing at bytes that were checked against
    their extension); text is read here, escaped by the template, and cut at TEXT_PREVIEW_BYTES. A file
    that fails the check, or has no safe preview, gets 'Preview not available' and a download."""
    preview_kind, text, truncated, dimensions, error = None, "", False, None, ""
    try:
        head = media.read_head(item)
        preview_kind = media.sniff_preview_kind(item, head)
    except Exception:  # noqa: BLE001
        logger.exception("media view could not read a file")
        error = _("The file could not be read from storage.")
    if preview_kind == "text":
        text = head.decode("utf-8", errors="replace")
        truncated = (item.size or 0) > media.TEXT_PREVIEW_BYTES
        if not item.is_public:  # image/PDF previews are audited by their own request; text is shown here
            _audit_once(request.user, "media_preview", _audit_id(item), f"{item.category} {item.size or 0}B")
    elif preview_kind == "image":
        dimensions = media.image_info(item)
    status, status_text = media.reference_status(item)
    if item.is_orphan:
        urls = {
            "preview": reverse("governance:media_preview_orphan", args=[item.token]),
            "download": reverse("governance:media_download_orphan", args=[item.token]),
        }
    else:
        urls = {
            "preview": reverse("governance:media_preview", args=[item.source, item.pk]),
            "download": reverse("governance:media_download", args=[item.source, item.pk]),
        }
    return render(
        request,
        "governance/media_detail.html",
        {
            "item": item,
            "preview_kind": preview_kind,
            "text": text,
            "truncated": truncated,
            "dimensions": dimensions,
            "error": error,
            "reference_state": status,
            "reference_text": status_text,
            "urls": urls,
            "back_url": _back_url(request.GET.get("back", "")),
            "back_query": urlencode(_clean_params(_qd(request.GET.get("back", "")))),
        },
    )


def _qd(raw):
    return QueryDict(raw or "", mutable=False)


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["GET"])
def media_view(request, source, pk):
    return _detail_response(request, _item_or_404(source, pk))


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["GET"])
def media_view_orphan(request, token):
    return _detail_response(request, _orphan_or_404(token))


# ---------------------------------------------------------------------------------------------
# Deleting a file no record refers to
# ---------------------------------------------------------------------------------------------
@role_required(User.Role.SUPERADMIN)
@require_http_methods(["POST"])
def media_delete_orphan(request):
    """Delete ONE file that no record refers to. The browser asks "Delete file?" first, but that dialog
    is a courtesy, not the protection: this view authenticates and authorises, finds the exact file
    from the scan by its opaque digest, and media_service.delete_orphan then re-checks - now, not from
    the page the admin was looking at - that the file still exists and that NO record refers to it.
    There is no bulk route and no way to name a path."""
    back = _back_url(request.POST.get("back", ""))
    token = request.POST.get("file", "")
    reference = getattr(request, "id", "") or ""
    ref_text = f" ({_('reference')} {reference})" if reference else ""
    try:
        name = media.orphan_name_for(token)
        if name is None:
            if media.token_is_referenced(token):
                _audit(request.user, "media_delete_blocked", token, new_value="still referenced")
                django_messages.error(request, _("This file is now in use and cannot be deleted."))
            else:
                django_messages.warning(request, _("This file no longer exists. It may already have been deleted."))
            return redirect(back)
        status, size = media.delete_orphan(name)
    except Exception:  # noqa: BLE001 - never show a raw storage or database error
        logger.exception("media delete failed")
        django_messages.error(request, _("Something went wrong and the file was not deleted.") + ref_text)
        return redirect(back)
    digest = media.name_digest(name)
    if status == media.DELETE_OK:
        _audit(request.user, "media_orphan_deleted", digest, old_value=f"{media.category_of(name)} {size}B")
        django_messages.success(request, _("File deleted."))
    elif status == media.DELETE_REFERENCED:
        _audit(request.user, "media_delete_blocked", digest, new_value="still referenced")
        django_messages.error(request, _("This file is now in use and cannot be deleted."))
    elif status == media.DELETE_MISSING:
        django_messages.warning(request, _("This file no longer exists. It may already have been deleted."))
    elif status == media.DELETE_STORAGE_ERROR:
        logger.error("media delete: the storage refused to remove a file")
        django_messages.error(request, _("The storage could not remove this file. Nothing was changed.") + ref_text)
    else:
        django_messages.error(request, _("That file could not be identified, so nothing was deleted."))
    return redirect(back)


# ---------------------------------------------------------------------------------------------
# Bulk delete (JSON; the page sends small chunks and shows progress between them)
# ---------------------------------------------------------------------------------------------
BULK_LOCK_SECONDS = 120
_OPERATION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _json_error(message, status):
    return JsonResponse({"ok": False, "error": str(message)}, status=status)


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["POST"])
def media_bulk_delete(request):
    """Delete the safe files among the identifiers sent. The browser's selection is not trusted: this view
    authenticates and authorises, refuses an oversized request whole, refuses to run the same step twice, and
    media_service.bulk_delete then resolves each opaque id and re-checks references file by file. One audit row
    per request (counts only - never a name, path or content)."""
    ids = request.POST.getlist("ids")
    operation = request.POST.get("op", "")
    chunk = request.POST.get("chunk", "0")[:6]
    if not ids:
        return _json_error(_("Nothing was selected."), 400)
    if len(ids) > media.BULK_MAX_PER_REQUEST:
        return _json_error(
            _("Too many files in one request (the limit is %(limit)s).") % {"limit": media.BULK_MAX_PER_REQUEST}, 400
        )
    if not _OPERATION_ID.match(operation):
        return _json_error(_("This request is not valid."), 400)
    lock_key = f"media:bulk:{request.user.pk}:{operation}:{chunk}"
    try:
        first_time = cache.add(lock_key, 1, BULK_LOCK_SECONDS)
    except Exception:  # noqa: BLE001 - no cache: deleting is idempotent, so run rather than refuse
        first_time = True
    if not first_time:
        return _json_error(_("This step was already submitted."), 409)
    try:
        result = media.bulk_delete(ids)
    except Exception:  # noqa: BLE001 - never show a raw storage or database error
        logger.exception("media bulk delete failed")
        reference = getattr(request, "id", "") or ""
        suffix = f" ({_('reference')} {reference})" if reference else ""
        return _json_error(_("Something went wrong and this step was not completed.") + suffix, 500)
    _audit(
        request.user,
        "media_bulk_delete",
        f"bulk:{operation[:12]}",
        old_value=(
            f"selected={result.selected} deleted={result.deleted} skipped_referenced={result.skipped_referenced} "
            f"skipped_missing={result.skipped_missing} failed={result.failed}"
        ),
        new_value=f"freed={result.freed_bytes}B step={chunk}",
    )
    return JsonResponse({"ok": True, **result.as_dict()})


@role_required(User.Role.SUPERADMIN)
@require_http_methods(["GET"])
def media_bulk_selectable(request):
    """What 'select all matching' means for the CURRENT filters, decided by the server: how many files match,
    how many of them are deletable (orphan candidates), and the identifiers of the first BULK_MAX_PER_OPERATION
    of those. The page never builds this list itself, so it cannot select outside its own filter."""
    filters = _filters_from_request(request)
    _items, total, _error = media.list_items(filters, page=1, page_size=1)
    orphans = [] if filters["reference"] == "referenced" else media._orphan_items(filters)
    limit = media.BULK_MAX_PER_OPERATION
    return JsonResponse(
        {
            "total": total,
            "eligible": len(orphans),
            "referenced": total - len(orphans),
            "ids": [item.bulk_id for item in orphans[:limit]],
            "limit": limit,
            "truncated": len(orphans) > limit,
        }
    )
