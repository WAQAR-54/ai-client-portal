"""Magic-byte content verification, shared by every upload path in the app
(chat attachments, billing payment proofs, branding logo/favicon).
Extension/MIME-type strings are attacker-controlled and trivially faked (a
renamed .exe can claim to be .pdf, and Django/browsers never check) - this
instead reads the file's real header bytes via `filetype` (pure Python, no
libmagic system dependency) and:

1. Hard-rejects known executable/installer formats outright, no matter what
   extension the filename claims or what an admin's UsageLimit.
   allowed_file_extensions override says - closes the gap where that
   override had no safety floor at all (see governance/tests.py's
   UploadLimitOverrideTests for the override behavior itself, which this
   does not change - an admin can still permit an "exe" extension, but a
   file that is *actually* an executable is rejected regardless).
2. For extensions this app accepts that DO have a real magic signature
   (pdf/png/jpg/docx/xlsx), confirms the real content matches the claimed
   extension - not just that it isn't a blocked type.
3. For extensions with no magic signature at all (txt/csv/md/json -
   genuinely plain text, nothing to sniff), only the executable check
   above applies.
"""

import filetype
from django.utils.translation import gettext as _

_HEADER_BYTES = 8192  # enough for filetype's docx/xlsx zip-structure matchers, which look several KB in


class UploadContentRejected(Exception):
    pass


# filetype's own detected kinds that are executables/installers/formats
# capable of running code - blocked outright regardless of claimed
# extension or any allowlist override.
_BLOCKED_KINDS = {"exe", "elf", "crx", "cab", "deb", "rpm", "swf"}

# Real-world executable signatures filetype.py has no matcher for at all
# (no Mach-O, no script/shebang detection).
_EXTRA_EXECUTABLE_SIGNATURES = (
    b"\xfe\xed\xfa\xce",  # Mach-O 32-bit
    b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit
    b"\xce\xfa\xed\xfe",  # Mach-O 32-bit, reverse byte order
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit, reverse byte order
    b"\xca\xfe\xba\xbe",  # Mach-O fat binary / Java class
    b"#!",  # script with a shebang line (#!/bin/sh, #!/usr/bin/env python, ...)
)

# Extensions this app accepts that have a real, checkable magic signature -
# the detected kind must be one of these, or it's a content/extension
# mismatch. Extensions absent from this dict (txt, csv, md, json) are
# genuinely unsniffable plain text - only the executable check above
# applies to them.
_EXPECTED_KINDS = {
    "pdf": {"pdf"},
    "png": {"png"},
    "jpg": {"jpg"},
    "jpeg": {"jpg"},
    "docx": {"docx"},
    "xlsx": {"xlsx"},
}


def verify_file_content(uploaded_file, claimed_extension):
    """Raise UploadContentRejected if `uploaded_file`'s real bytes don't
    match what its filename claims, or look like an executable/script.
    Always restores the file's read position before returning (or raising)
    so callers can go on to save() it normally afterwards."""
    uploaded_file.seek(0)
    header = uploaded_file.read(_HEADER_BYTES)
    uploaded_file.seek(0)

    if any(header.startswith(sig) for sig in _EXTRA_EXECUTABLE_SIGNATURES):
        raise UploadContentRejected(_("This file looks like an executable or script, which isn't allowed."))

    kind = filetype.guess(bytes(header))
    if kind is not None and kind.extension in _BLOCKED_KINDS:
        raise UploadContentRejected(_("This file looks like an executable or script, which isn't allowed."))

    expected = _EXPECTED_KINDS.get(claimed_extension.lower())
    if expected is not None and (kind is None or kind.extension not in expected):
        raise UploadContentRejected(
            _("This file's content doesn't match its extension (.%(ext)s).") % {"ext": claimed_extension}
        )
