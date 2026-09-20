"""Server media management: what files the application stores and which record owns each.

Design rules (see docs/OPERATIONS.md):

* The list of files is DRIVEN BY THE DATABASE (chat attachments, generated media, payment
  proofs, branding), never by browsing the filesystem, so it is paginated and searchable in SQL
  and never loads a directory into memory. Users' files stay behind authorised views.
* The only filesystem walk is the storage SCAN (statistics, orphan candidates, missing files):
  bounded by a file count and a time budget, cached, and run on demand.
* Every operation goes through the configured storage (`default_storage`); nothing here builds a
  path from user input. A file is addressed by (source, primary key), never by a path.
* Visibility is reported, never changed: nothing here can make a private file public.
"""

import hashlib
import heapq
import mimetypes
import os
import posixpath
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage, default_storage
from django.db.models import F, Q

SOURCE_CHAT = "chat"  # a file a user attached to a message
SOURCE_GENERATED = "generated"  # an image/video/document the assistant produced
SOURCE_PROOF = "proof"  # a payment screenshot on an invoice
SOURCE_BRANDING = "branding"  # the logo / favicon (public: the login page needs them)
SOURCES = (SOURCE_CHAT, SOURCE_GENERATED, SOURCE_PROOF, SOURCE_BRANDING)
SOURCE_LABELS = {
    SOURCE_CHAT: "Chat upload",
    SOURCE_GENERATED: "Generated media",
    SOURCE_PROOF: "Payment proof",
    SOURCE_BRANDING: "Branding",
}
VISIBILITY = {
    SOURCE_CHAT: "USER-OWNED",
    SOURCE_GENERATED: "USER-OWNED",
    SOURCE_PROOF: "PRIVATE (billing)",
    SOURCE_BRANDING: "PUBLIC",
}

# Classification follows what the application itself accepts or produces, not what an extension looks
# like: uploads allow pdf/txt/csv/md/png/jpg/jpeg/docx/xlsx/json (settings.DEFAULT_ALLOWED_FILE_EXTENSIONS,
# content-checked in governance/uploads.py); generated files add pptx (chat/document_generation.py) and
# webp/gif images. Anything else - svg/html/exe, legacy office formats, video - is "other", so it is
# never presented as a safe document or image.
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
DOCUMENT_EXTS = {"pdf", "txt", "md", "csv", "json", "docx", "xlsx", "pptx"}
CATEGORIES = ("image", "document", "other")
CATEGORY_LABELS = {"image": "Image", "document": "Document", "other": "Other"}
SORTS = ("newest", "oldest", "largest", "smallest")
SORT_LABELS = {"newest": "Newest", "oldest": "Oldest", "largest": "Largest", "smallest": "Smallest"}
REFERENCE_FILTERS = ("referenced", "orphan")
SOURCE_ORPHAN = "orphan"  # a file on disk that no record refers to (found by the storage scan)
SOURCE_LABELS[SOURCE_ORPHAN] = "Orphan candidate"
VISIBILITY[SOURCE_ORPHAN] = "PRIVATE (no owner record)"  # it is not served anywhere; treat as private


def size_options():
    """[(key, label, min_bytes, max_bytes)] for the size filter, from settings (not hard-coded):
    Small < MEDIA_MEDIUM_MIN_BYTES <= Medium < MEDIA_LARGE_MIN_BYTES <= Large, plus the operational
    'large file' thresholds (MEDIA_LARGE_THRESHOLDS_MB). Only files with a recorded size can match
    (chat uploads, generated files and orphans); payment proofs and branding record none."""
    medium = int(getattr(settings, "MEDIA_MEDIUM_MIN_BYTES", 1024**2))
    large = int(getattr(settings, "MEDIA_LARGE_MIN_BYTES", 10 * 1024**2))
    options = [
        ("small", f"Small (under {_mb(medium)})", None, medium - 1),
        ("medium", f"Medium ({_mb(medium)} to {_mb(large)})", medium, large - 1),
        ("large", f"Large ({_mb(large)} and over)", large, None),
    ]
    for mb in getattr(settings, "MEDIA_LARGE_THRESHOLDS_MB", (50, 100, 500)):
        if mb * 1024**2 > large:
            options.append((f"ge{mb}", f"{mb} MB and over", mb * 1024**2, None))
    return options


def _mb(num_bytes):
    mb = num_bytes / 1024**2
    return f"{mb:g} MB"


# Inline preview is limited to formats that cannot run code in the browser and whose content is
# checked (magic bytes) before it is sent. SVG and HTML are never previewed: they can carry script.
RASTER_PREVIEW = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff", "gif": b"GIF8"}
TEXT_PREVIEW_EXTS = {"txt", "md", "csv", "json", "log"}
TEXT_PREVIEW_BYTES = 20_000

SCAN_CACHE_KEY = "media:scan:v1"
SCAN_TTL_SECONDS = 600
SCAN_MAX_FILES = 100_000
SCAN_TIME_BUDGET_SECONDS = 8.0
PAGE_SIZE = 25
MAX_PAGE = 400  # merging three sources costs O(offset): deeper than this, narrow the filters


def ext_of(name):
    return posixpath.splitext(name)[1].lstrip(".").lower()


def category_of(name):
    ext = ext_of(name)
    return "image" if ext in IMAGE_EXTS else "document" if ext in DOCUMENT_EXTS else "other"


def safe_display_name(name):
    """The file's own name with anything unusual replaced - shown and used in headers, never as a path."""
    base = posixpath.basename(str(name).replace("\\", "/"))
    cleaned = "".join(c if (c.isalnum() or c in "._- ()") else "_" for c in base)
    return cleaned[:120] or "file"


def name_digest(relative_name):
    """What is written to the audit log instead of a file name (names are user-chosen and can
    contain anything): a stable short hash, enough to correlate a record with a file."""
    return hashlib.sha256(relative_name.encode("utf-8")).hexdigest()[:16]


@dataclass
class MediaItem:
    source: str
    pk: int
    relative_name: str
    display_name: str
    ext: str
    category: str
    mime: str
    size: int | None
    owner: str
    related: str
    uploaded: datetime | None
    visibility: str
    state: str = "unknown"  # present | missing | unknown
    modified: datetime | None = None
    extra: dict = field(default_factory=dict)

    @property
    def source_label(self):
        return SOURCE_LABELS[self.source]

    @property
    def is_orphan(self):
        return self.source == SOURCE_ORPHAN

    @property
    def token(self):
        """Opaque id of an orphan (a digest of its storage name); orphans have no database id."""
        return self.extra.get("token", "")

    @property
    def category_label(self):
        return CATEGORY_LABELS[self.category]

    @property
    def can_preview(self):
        return self.state != "missing" and (
            self.ext in RASTER_PREVIEW or self.ext in TEXT_PREVIEW_EXTS or self.ext == "pdf"
        )

    @property
    def is_public(self):
        return self.visibility == "PUBLIC"


# ---------------------------------------------------------------------------------------------
# Database-driven listing
# ---------------------------------------------------------------------------------------------
def _category_q(prefix, category):
    if category == "image":
        exts = IMAGE_EXTS
    elif category == "document":
        exts = DOCUMENT_EXTS
    else:
        return None
    q = Q()
    for ext in exts:
        q |= Q(**{f"{prefix}__iendswith": f".{ext}"})
    return q


def _message_qs(source, filters):
    from chat.models import Message

    role = Message.Role.USER if source == SOURCE_CHAT else Message.Role.ASSISTANT
    qs = Message.objects.filter(role=role).exclude(attachment="").exclude(attachment__isnull=True)
    qs = qs.select_related("conversation", "conversation__user")
    q = filters.get("q")
    if q:
        qs = qs.filter(
            Q(attachment_original_name__icontains=q)
            | Q(attachment__icontains=q)
            | Q(conversation__user__email__icontains=q)
            | Q(conversation__title__icontains=q)
        )
    if filters.get("owner"):
        qs = qs.filter(conversation__user__email__icontains=filters["owner"])
    category = filters.get("category")
    if category in ("image", "document"):
        qs = qs.filter(_category_q("attachment", category))
    elif category == "other":
        qs = qs.exclude(_category_q("attachment", "image")).exclude(_category_q("attachment", "document"))
    if filters.get("ext"):
        qs = qs.filter(attachment__iendswith=f".{filters['ext']}")
    if filters.get("date_from"):
        qs = qs.filter(created_at__date__gte=filters["date_from"])
    if filters.get("date_to"):
        qs = qs.filter(created_at__date__lte=filters["date_to"])
    if filters.get("size_min") is not None:
        qs = qs.filter(attachment_size__gte=filters["size_min"])
    if filters.get("size_max") is not None:
        qs = qs.filter(attachment_size__lte=filters["size_max"])
    sort = filters.get("sort") or "newest"
    if sort == "oldest":
        return qs.order_by("created_at", "pk")
    if sort == "largest":  # files with no recorded size go last, in both size orders
        return qs.order_by(F("attachment_size").desc(nulls_last=True), "-created_at", "-pk")
    if sort == "smallest":
        return qs.order_by(F("attachment_size").asc(nulls_last=True), "-created_at", "-pk")
    return qs.order_by("-created_at", "-pk")


def _proof_qs(filters):
    from billing.models import Invoice

    qs = Invoice.objects.exclude(submitted_proof_image="").exclude(submitted_proof_image__isnull=True)
    qs = qs.select_related("recipient_user")
    q = filters.get("q")
    if q:
        qs = qs.filter(
            Q(invoice_number__icontains=q)
            | Q(submitted_proof_image__icontains=q)
            | Q(recipient_user__email__icontains=q)
        )
    if filters.get("owner"):
        qs = qs.filter(recipient_user__email__icontains=filters["owner"])
    category = filters.get("category")
    if category in ("image", "document"):
        qs = qs.filter(_category_q("submitted_proof_image", category))
    elif category == "other":
        qs = qs.exclude(_category_q("submitted_proof_image", "image")).exclude(
            _category_q("submitted_proof_image", "document")
        )
    if filters.get("ext"):
        qs = qs.filter(submitted_proof_image__iendswith=f".{filters['ext']}")
    if filters.get("date_from"):
        qs = qs.filter(submitted_at__date__gte=filters["date_from"])
    if filters.get("date_to"):
        qs = qs.filter(submitted_at__date__lte=filters["date_to"])
    if filters.get("size_min") is not None or filters.get("size_max") is not None:
        return qs.none()  # payment proofs record no size, so a size filter cannot match them
    if filters.get("sort") == "oldest":
        return qs.order_by("submitted_at", "pk")
    return qs.order_by("-submitted_at", "-pk")  # no size to sort by: newest first for the size sorts


def _branding():
    """The branding row if it exists. Never SiteBranding.load(): that INSERTs the row when it is
    missing, and looking at media (or running the read-only ops_verify) must not write."""
    from governance.models import SiteBranding

    return SiteBranding.objects.filter(pk=1).first()


def _branding_items(filters):
    if filters.get("owner") or filters.get("size_min") is not None or filters.get("size_max") is not None:
        return []
    branding = _branding()
    if branding is None:
        return []
    items = []
    for label, field_file in (("Logo", branding.logo), ("Favicon", branding.favicon)):
        if not field_file or not field_file.name:
            continue
        if filters.get("category") and category_of(field_file.name) != filters["category"]:
            continue
        if filters.get("ext") and ext_of(field_file.name) != filters["ext"]:
            continue
        if filters.get("date_from") or filters.get("date_to"):
            continue  # branding rows carry no upload date, so a date filter cannot match them
        q = (filters.get("q") or "").lower()
        if q and q not in field_file.name.lower() and q not in label.lower():
            continue
        items.append((label, field_file.name))
    return items


def _to_item(source, obj, extra_label=None):
    if source in (SOURCE_CHAT, SOURCE_GENERATED):
        name = obj.attachment.name
        conversation = obj.conversation
        related = f"Conversation #{conversation.pk}" + (" (deleted)" if conversation.is_deleted else "")
        display = safe_display_name(obj.attachment_original_name or name)
        return MediaItem(
            source, obj.pk, name, display, ext_of(name), category_of(name),
            mimetypes.guess_type(name)[0] or "application/octet-stream", obj.attachment_size,
            conversation.user.email if conversation.user_id else "(deleted user)", related,
            obj.created_at, VISIBILITY[source],
        )  # fmt: skip
    if source == SOURCE_PROOF:
        name = obj.submitted_proof_image.name
        return MediaItem(
            source, obj.pk, name, safe_display_name(name), ext_of(name), category_of(name),
            mimetypes.guess_type(name)[0] or "application/octet-stream", None,
            obj.recipient_user.email if obj.recipient_user_id else "(none)", f"Invoice {obj.invoice_number}",
            obj.submitted_at, VISIBILITY[source],
        )  # fmt: skip
    name = obj
    return MediaItem(
        SOURCE_BRANDING, 1 if extra_label == "Logo" else 2, name, safe_display_name(name), ext_of(name),
        category_of(name), mimetypes.guess_type(name)[0] or "application/octet-stream", None, "(organisation)",
        f"Site {extra_label.lower()}", None, VISIBILITY[SOURCE_BRANDING],
    )  # fmt: skip


def _fill_state(item, storage=None):
    """stat() the ONE file behind a row that is actually being shown (a page is <= 25 rows)."""
    storage = storage or default_storage
    try:
        if not storage.exists(item.relative_name):
            item.state = "missing"
            return item
        item.state = "present"
        if item.size is None:
            item.size = storage.size(item.relative_name)
        item.modified = storage.get_modified_time(item.relative_name)
        if item.uploaded is None:
            item.uploaded = item.modified
    except Exception:  # noqa: BLE001 - a broken storage must not break the page
        item.state = "unknown"
    return item


def _sort_key(sort):
    """(key function, reverse) for merging the per-source streams in one global order. Items whose
    date or size is unknown always sort last, whichever way the sort runs."""
    epoch = datetime.min.replace(tzinfo=dt_timezone.utc)
    if sort == "oldest":
        return (lambda i: (i.uploaded is None, i.uploaded or epoch)), False
    if sort == "largest":
        return (lambda i: (i.size is not None, i.size or 0, i.uploaded or epoch)), True
    if sort == "smallest":
        return (lambda i: (i.size is None, i.size or 0, -(i.uploaded or epoch).timestamp())), False
    return (lambda i: (i.uploaded is not None, i.uploaded or epoch)), True


def _orphan_items(filters):
    """Orphan candidates from the CACHED storage scan (never a fresh walk), filtered in memory. The scan
    keeps at most 500 of them; an orphan has no owner, related record or upload date, only what the disk
    says (its modification time), so a filter on those fields cannot match it."""
    if filters.get("owner") or filters.get("source"):
        return []
    scan = get_scan()
    if scan.get("error"):
        return []
    q = (filters.get("q") or "").lower()
    items = []
    for entry in scan.get("orphans", []):
        name = entry["name"]
        display = safe_display_name(name)
        if q and q not in display.lower() and q not in entry["reason"].lower():
            continue
        category = category_of(name)
        if filters.get("category") and category != filters["category"]:
            continue
        if filters.get("ext") and ext_of(name) != filters["ext"]:
            continue
        size = entry["size"]
        if filters.get("size_min") is not None and size < filters["size_min"]:
            continue
        if filters.get("size_max") is not None and size > filters["size_max"]:
            continue
        modified = datetime.fromtimestamp(entry["mtime"], tz=dt_timezone.utc)
        if filters.get("date_from") and modified.date() < filters["date_from"]:
            continue
        if filters.get("date_to") and modified.date() > filters["date_to"]:
            continue
        items.append(
            MediaItem(
                SOURCE_ORPHAN,
                0,
                name,
                display,
                ext_of(name),
                category,
                mimetypes.guess_type(name)[0] or "application/octet-stream",
                size,
                "No owner record",
                "No record refers to this file",
                modified,
                VISIBILITY[SOURCE_ORPHAN],
                state="present",
                modified=modified,
                extra={"token": entry["id"], "reason": entry["reason"]},
            )
        )
    return items


def list_items(filters, page=1, page_size=PAGE_SIZE):
    """(items_on_this_page, total_matching, storage_error) across every source in one global order.

    Referenced files come from the database (SQL search, filters, ordering and a bounded slice per
    source); orphan candidates come from the cached scan. `reference` limits the list to one of them."""
    reference = filters.get("reference") or ""
    if reference == "orphan":
        sources = []
    else:
        sources = [s for s in SOURCES if filters.get("source") in (None, "", s)]
    page = max(1, min(int(page or 1), MAX_PAGE))
    offset = (page - 1) * page_size
    key, reverse = _sort_key(filters.get("sort") or "newest")
    streams, total = [], 0
    for source in sources:
        if source in (SOURCE_CHAT, SOURCE_GENERATED):
            qs = _message_qs(source, filters)
            count = qs.count()
            rows = [_to_item(source, o) for o in qs[: offset + page_size]] if count else []
        elif source == SOURCE_PROOF:
            qs = _proof_qs(filters)
            count = qs.count()
            rows = [_to_item(source, o) for o in qs[: offset + page_size]] if count else []
        else:
            raw = _branding_items(filters)
            count = len(raw)
            rows = sorted((_to_item(source, name, label) for label, name in raw), key=key, reverse=reverse)
        total += count
        streams.append(rows)
    if reference != "referenced":
        orphans = sorted(_orphan_items(filters), key=key, reverse=reverse)
        total += len(orphans)
        streams.append(orphans)
    merged = heapq.merge(*streams, key=key, reverse=reverse)
    window = []
    for index, item in enumerate(merged):
        if index >= offset + page_size:
            break
        if index >= offset:
            window.append(item)
    storage_error = None
    try:
        for item in window:
            if not item.is_orphan:  # an orphan's size and existence come from the scan that found it
                _fill_state(item)
    except Exception:  # noqa: BLE001
        storage_error = "The storage backend could not be read."
    return window, total, storage_error


def get_orphan_item(token):
    """One orphan candidate by the digest the list gave out, or None. Only a file the storage scan
    flagged can be addressed this way, and the digest is never a path."""
    for entry in get_scan().get("orphans", []):
        if entry["id"] == token:
            name = entry["name"]
            item = MediaItem(
                SOURCE_ORPHAN,
                0,
                name,
                safe_display_name(name),
                ext_of(name),
                category_of(name),
                mimetypes.guess_type(name)[0] or "application/octet-stream",
                None,
                "No owner record",
                "No record refers to this file",
                None,
                VISIBILITY[SOURCE_ORPHAN],
                extra={"token": token, "reason": entry["reason"]},
            )
            return _fill_state(item)
    return None


def reference_status(item):
    """('referenced' | 'orphan' | 'missing', short text) - decided NOW, not from the cached scan."""
    if item.state == "missing":
        return "missing", "The record points at a file that is no longer on disk"
    if item.is_orphan:
        if is_referenced(item.relative_name):
            return "referenced", "This file is now in use by a record"
        return "orphan", item.extra.get("reason") or "No record refers to this file"
    return "referenced", item.related


def image_info(item):
    """(width, height) read from the file HEADER only (Pillow does not decode the pixels here), and
    only when the format Pillow finds matches the extension. None when it cannot be determined."""
    try:
        from PIL import Image

        with default_storage.open(item.relative_name, "rb") as handle:
            with Image.open(handle) as image:
                expected = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF", "webp": "WEBP"}.get(item.ext)
                if expected is None or image.format != expected:
                    return None
                return image.size
    except Exception:  # noqa: BLE001 - dimensions are a nicety, never a reason to fail the page
        return None


def get_item(source, pk):
    """One item by (source, pk), or None. The lookup key is never a path."""
    from billing.models import Invoice
    from chat.models import Message

    if source in (SOURCE_CHAT, SOURCE_GENERATED):
        role = Message.Role.USER if source == SOURCE_CHAT else Message.Role.ASSISTANT
        obj = (
            Message.objects.filter(pk=pk, role=role)
            .exclude(attachment="")
            .exclude(attachment__isnull=True)
            .select_related("conversation", "conversation__user")
            .first()
        )
        return _fill_state(_to_item(source, obj)) if obj else None
    if source == SOURCE_PROOF:
        obj = (
            Invoice.objects.filter(pk=pk)
            .exclude(submitted_proof_image="")
            .exclude(submitted_proof_image__isnull=True)
            .select_related("recipient_user")
            .first()
        )
        return _fill_state(_to_item(source, obj)) if obj else None
    if source == SOURCE_BRANDING and pk in (1, 2):
        branding = _branding()
        field_file = None if branding is None else (branding.logo if pk == 1 else branding.favicon)
        if field_file and field_file.name:
            return _fill_state(_to_item(source, field_file.name, "Logo" if pk == 1 else "Favicon"))
    return None


# ---------------------------------------------------------------------------------------------
# Serving (authorisation is the caller's job; this only decides HOW a file may be sent)
# ---------------------------------------------------------------------------------------------
def sniff_preview_kind(item, head):
    """'image' | 'pdf' | 'text' when the bytes really are what the extension says, else None.
    The extension and the stored MIME type are never trusted on their own."""
    if item.ext in RASTER_PREVIEW:
        return "image" if head.startswith(RASTER_PREVIEW[item.ext]) else None
    if item.ext == "pdf":
        return "pdf" if head.startswith(b"%PDF-") else None
    if item.ext in TEXT_PREVIEW_EXTS:
        if b"\x00" in head:
            return None
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            try:
                head[:-3].decode("utf-8")  # a multi-byte character may be cut by the read size
            except UnicodeDecodeError:
                return None
        return "text"
    return None


def read_head(item, size=TEXT_PREVIEW_BYTES):
    with default_storage.open(item.relative_name, "rb") as handle:
        return handle.read(size)


# ---------------------------------------------------------------------------------------------
# Storage scan: statistics, orphan candidates, missing files
# ---------------------------------------------------------------------------------------------
def referenced_names():
    from billing.models import Invoice
    from chat.models import Message

    names = set(
        Message.objects.exclude(attachment="").exclude(attachment__isnull=True).values_list("attachment", flat=True)
    )
    names |= set(
        Invoice.objects.exclude(submitted_proof_image="")
        .exclude(submitted_proof_image__isnull=True)
        .values_list("submitted_proof_image", flat=True)
    )
    branding = _branding()
    for field_file in (branding.logo, branding.favicon) if branding else ():
        if field_file and field_file.name:
            names.add(field_file.name)
    return names


def is_referenced(relative_name):
    """Fresh, exact check against every place a file can be referenced from (never the cache)."""
    return relative_name in referenced_names()


def _walk_filesystem(root, budget_end, max_files):
    """Yield (relative posix name, size, mtime) with a single stat per file; stops at the bounds."""
    stack, seen = [""], 0
    while stack:
        rel = stack.pop()
        try:
            with os.scandir(os.path.join(root, rel) if rel else root) as entries:
                for entry in entries:
                    sub = f"{rel}/{entry.name}" if rel else entry.name
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(sub)
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        yield sub, stat.st_size, stat.st_mtime
                        seen += 1
                        if seen >= max_files or time.monotonic() > budget_end:
                            return
        except OSError:
            continue


def _walk_generic(storage, budget_end, max_files):
    stack, seen = [""], 0
    while stack:
        rel = stack.pop()
        dirs, files = storage.listdir(rel)
        for d in dirs:
            stack.append(f"{rel}/{d}" if rel else d)
        for f in files:
            sub = f"{rel}/{f}" if rel else f
            yield sub, storage.size(sub), storage.get_modified_time(sub).timestamp()
            seen += 1
            if seen >= max_files or time.monotonic() > budget_end:
                return


def _orphan_reason(name):
    parts = name.split("/")
    if parts[0] == "chat_attachments":
        return "Chat file whose message or conversation no longer exists (deleted before file clean-up existed)"
    if parts[0] == "invoice_proofs":
        return "Payment proof whose invoice was deleted, or that was replaced"
    if parts[0] == "branding":
        return "Logo or favicon that was replaced"
    return "File in an unexpected location that no record refers to"


def disk_usage():
    """Filesystem capacity where the media lives; None when the storage is not a local disk."""
    storage = default_storage
    if not isinstance(storage, FileSystemStorage):
        return None
    try:
        usage = shutil.disk_usage(storage.location)
    except OSError:
        return None
    pct = usage.used / usage.total * 100 if usage.total else 0
    warn = getattr(settings, "MEDIA_DISK_WARN_PCT", 80)
    critical = getattr(settings, "MEDIA_DISK_CRITICAL_PCT", 90)
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": round(pct, 1),
        "state": "CRITICAL" if pct >= critical else "WARNING" if pct >= warn else "NORMAL",
    }


def run_scan(max_files=SCAN_MAX_FILES, time_budget=SCAN_TIME_BUDGET_SECONDS):
    """Walk the storage ONCE (bounded) and compare it with the database. Read-only; never deletes."""
    started = time.monotonic()
    storage = default_storage
    result = {
        "scanned_at": datetime.now(dt_timezone.utc).isoformat(),
        "error": None,
        "partial": False,
        "files": 0,
        "bytes": 0,
        "by_category": {c: {"files": 0, "bytes": 0} for c in CATEGORIES},
        "recent": [],
        "large": [],
        "orphans": [],
        "orphan_count": 0,
        "orphan_bytes": 0,
        "missing": [],
        "missing_count": 0,
    }
    try:
        walker = (
            _walk_filesystem(storage.location, started + time_budget, max_files)
            if isinstance(storage, FileSystemStorage)
            else _walk_generic(storage, started + time_budget, max_files)
        )
        referenced = referenced_names()
        on_disk = set()
        recent, large = [], []
        for name, size, mtime in walker:
            on_disk.add(name)
            result["files"] += 1
            result["bytes"] += size
            bucket = result["by_category"][category_of(name)]
            bucket["files"] += 1
            bucket["bytes"] += size
            entry = {"name": name, "size": size, "mtime": mtime}
            recent.append(entry)
            large.append(entry)
            if len(recent) > 400:
                recent = heapq.nlargest(10, recent, key=lambda e: e["mtime"])
            if len(large) > 400:
                large = heapq.nlargest(10, large, key=lambda e: e["size"])
            if name not in referenced:
                result["orphan_count"] += 1
                result["orphan_bytes"] += size
                if len(result["orphans"]) < 500:
                    result["orphans"].append({**entry, "id": name_digest(name), "reason": _orphan_reason(name)})
        result["partial"] = result["files"] >= max_files or time.monotonic() > started + time_budget
        result["recent"] = heapq.nlargest(10, recent, key=lambda e: e["mtime"])
        result["large"] = heapq.nlargest(10, large, key=lambda e: e["size"])
        if not result["partial"]:  # a partial walk cannot say what is missing
            gone = sorted(referenced - on_disk)
            result["missing_count"] = len(gone)
            result["missing"] = gone[:200]
    except NotImplementedError:
        result["error"] = "This storage backend cannot list files."
    except Exception:  # noqa: BLE001 - report, never crash the page
        result["error"] = "The storage could not be scanned."
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result


def get_scan(force=False):
    if not force:
        cached = cache.get(SCAN_CACHE_KEY)
        if cached is not None:
            return cached
    result = run_scan()
    try:
        cache.set(SCAN_CACHE_KEY, result, SCAN_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - no cache: the scan is simply recomputed next time
        pass
    return result


def clear_scan_cache():
    try:
        cache.delete(SCAN_CACHE_KEY)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------------------------
# Deleting an orphan
# ---------------------------------------------------------------------------------------------
def orphan_name_for(token):
    """The storage name of an orphan the last scan detected, looked up by its digest - the page
    never puts a path into a form, and only a file the scan flagged can be offered for deletion."""
    for entry in get_scan().get("orphans", []):
        if entry["id"] == token:
            return entry["name"]
    return None


DELETE_OK = "deleted"
DELETE_REFERENCED = "referenced"
DELETE_INVALID = "invalid"
DELETE_MISSING = "missing"
DELETE_STORAGE_ERROR = "storage_error"


def normalise_relative_name(raw):
    """A storage-relative name, or None when it could point outside the media directory."""
    if not raw or "\x00" in raw or "\\" in raw:
        return None
    name = posixpath.normpath(raw)
    if name.startswith(("/", "..")) or name in (".", "") or "/../" in f"/{name}/":
        return None
    return name


def token_is_referenced(token):
    """True when the digest belongs to a file some record refers to RIGHT NOW. Lets the delete flow say
    'this file is now in use' for a file that was an orphan when the page loaded but no longer is."""
    return any(name_digest(name) == token for name in referenced_names())


def delete_orphan(raw_name):
    """Delete ONE file that no record refers to. Returns (status, size).

    Everything is re-checked here, at the moment of deletion, whatever an earlier scan or page said:
    the name must stay inside the storage, NO record may reference it, and the file must exist."""
    name = normalise_relative_name(raw_name)
    if name is None:
        return DELETE_INVALID, 0
    storage = default_storage
    try:
        storage.path(name) if isinstance(storage, FileSystemStorage) else None  # safe_join: rejects escapes
    except Exception:  # noqa: BLE001 - SuspiciousFileOperation and friends
        return DELETE_INVALID, 0
    if is_referenced(name):
        return DELETE_REFERENCED, 0
    try:
        if not storage.exists(name):
            return DELETE_MISSING, 0
        size = storage.size(name)
        storage.delete(name)
    except Exception:  # noqa: BLE001 - reported as a fixed status; the raw error is for the log only
        return DELETE_STORAGE_ERROR, 0
    clear_scan_cache()
    return DELETE_OK, size
