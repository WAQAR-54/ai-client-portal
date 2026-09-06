"""PII detection & redaction (governance/models.py::PIIRule,
ComplianceSettings) - scans an outgoing chat message against a short
fixed checklist of data types before it's ever sent to a model. Patterns
here are deliberately simple regex heuristics, not a real NLP/PII
classifier - they will miss creative formatting and can false-positive on
coincidental digit sequences. Good enough to catch the common case; not a
guarantee."""

import re

_PATTERNS = {
    # Pakistani CNIC: 13 digits, either dashed (12345-1234567-1) or bare.
    "national_id": re.compile(r"\b\d{5}-\d{7}-\d\b|\b\d{13}\b"),
    # 16 digits in four groups of four, optionally dash/space separated -
    # matches most major card number formats. No Luhn check, so a random
    # 16-digit number can false-positive.
    "credit_card": re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b"),
    # Pakistani mobile (03xx-xxxxxxx / +923xxxxxxxxx) or a generic
    # international/local 10-digit format.
    "phone_number": re.compile(
        r"\b(?:\+92|0)3\d{2}[- ]?\d{7}\b|\b\+?\d{1,3}[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    ),
}

REDACTED_PLACEHOLDER = "[REDACTED]"


class PIIBlocked(Exception):
    def __init__(self, kind_label):
        self.kind_label = kind_label
        super().__init__(f"Message blocked: contains {kind_label}")


def apply_pii_rules(content):
    """Returns `content`, possibly with matches replaced by
    REDACTED_PLACEHOLDER for any enabled "redact" rule. Raises PIIBlocked
    if an enabled "block" rule matches. A no-op (returns content
    unchanged) when PII scanning is off, or a rule's kind isn't in
    _PATTERNS - deliberately fails open rather than crashing the message
    a rule was mismatched or a pattern is missing."""
    from governance.models import ComplianceSettings, PIIRule

    if not ComplianceSettings.load().pii_scanning_enabled:
        return content

    for rule in PIIRule.objects.filter(is_enabled=True):
        pattern = _PATTERNS.get(rule.kind)
        if not pattern or not pattern.search(content):
            continue
        if rule.action == PIIRule.Action.BLOCK:
            raise PIIBlocked(rule.get_kind_display())
        if rule.action == PIIRule.Action.REDACT:
            content = pattern.sub(REDACTED_PLACEHOLDER, content)
        # WARN: content passes through unchanged.
    return content
