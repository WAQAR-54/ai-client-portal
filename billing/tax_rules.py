"""Country -> tax policy, keyed by the same ISO country codes as
billing/regions.py's region registry - a department picks its billing
country from this same fixed list (see country_choices below), mirroring
RegionalPrice.region_code's "no freeform text" convention.

Real published VAT/sales-tax rates as of this writing. A country not in
TAX_RULES (anything outside the regions this product currently prices in)
falls back to DEFAULT_TAX_RULE rather than raising - it still needs an
invoice, just with no tax charged automatically until someone sets
DepartmentBillingProfile.custom_tax_rate by hand.
"""

from decimal import Decimal

from billing.regions import ALL_REGIONS

DEFAULT_TAX_RULE = {
    "tax_label": "Tax",
    "tax_rate": Decimal("0"),
    "tax_id_label": "Tax ID",
}

TAX_RULES = {
    "PK": {"tax_label": "Sales Tax", "tax_rate": Decimal("17"), "tax_id_label": "NTN"},
    "SA": {"tax_label": "VAT", "tax_rate": Decimal("15"), "tax_id_label": "VAT Number"},
    "AE": {"tax_label": "VAT", "tax_rate": Decimal("5"), "tax_id_label": "TRN"},
    "GB": {"tax_label": "VAT", "tax_rate": Decimal("20"), "tax_id_label": "VAT Number"},
    "QA": {"tax_label": "VAT", "tax_rate": Decimal("0"), "tax_id_label": "Tax ID"},
    "KW": {"tax_label": "VAT", "tax_rate": Decimal("0"), "tax_id_label": "Tax ID"},
}


def tax_rule_for_country(country_code):
    return TAX_RULES.get(country_code, DEFAULT_TAX_RULE)


def country_choices():
    """Billing-country choices for DepartmentBillingProfile.country - the
    same fixed list as the pricing regions, plus a catch-all "Other" for a
    client outside every priced region (0% tax by default, same as
    DEFAULT_TAX_RULE, until someone sets a custom rate)."""
    return [(code, label) for code, label, _currency, _flag in ALL_REGIONS] + [("OTHER", "Other")]
