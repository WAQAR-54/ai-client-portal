"""Fixed registry of billable regions - deliberately not a free-text field
anywhere (RegionalPrice.region_code is a CharField with no choices=, but
every place that creates/edits one goes through this list), so a currency
code can never be typo'd in from an admin form. REGIONS are on by default
for every new Plan (see billing/migrations); EXTRA_REGIONS are offered via
"Add region" on the Regional Pricing page, one at a time.

ROW ("Rest of world") is the fallback shown to a visitor whose IP doesn't
map to any of the other regions - see accounts/geo.py's region detection.
"""

REGIONS = [
    # (code, label, currency, flag emoji)
    ("PK", "Pakistan", "PKR", "\U0001f1f5\U0001f1f0"),
    ("SA", "Saudi Arabia", "SAR", "\U0001f1f8\U0001f1e6"),
    ("AE", "UAE", "AED", "\U0001f1e6\U0001f1ea"),
    ("ROW", "Rest of world", "USD", "\U0001f30d"),
]

EXTRA_REGIONS = [
    ("GB", "United Kingdom", "GBP", "\U0001f1ec\U0001f1e7"),
    ("QA", "Qatar", "QAR", "\U0001f1f6\U0001f1e6"),
    ("KW", "Kuwait", "KWD", "\U0001f1f0\U0001f1fc"),
]

ALL_REGIONS = REGIONS + EXTRA_REGIONS
REGION_BY_CODE = {code: (code, label, currency, flag) for code, label, currency, flag in ALL_REGIONS}


def region_choices():
    return [(code, label) for code, label, _currency, _flag in ALL_REGIONS]


def currency_for_region(region_code):
    entry = REGION_BY_CODE.get(region_code)
    return entry[2] if entry else "USD"


def region_for_country(country_code, active_codes):
    """Every region code in this registry (PK/SA/AE/GB/QA/KW) happens to
    already be the real ISO country code for that region, so detection is
    just "is this visitor's country one of the regions a SuperAdmin has
    actually priced" - active_codes (see billing/views.py's
    _active_region_codes) rather than ALL_REGIONS, so a visitor from a
    country whose region was never priced still gets a purchasable page
    (ROW) instead of silently landing on all-missing prices."""
    if country_code and country_code in active_codes:
        return country_code
    return "ROW"
