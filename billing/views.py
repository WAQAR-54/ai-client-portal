from decimal import Decimal, InvalidOperation

from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from accounts.geo import country_code_for_ip
from accounts.models import User
from accounts.permissions import SuperAdminRequiredMixin, role_required
from billing.models import RegionalPrice
from billing.regions import EXTRA_REGIONS, REGION_BY_CODE, REGIONS, region_for_country
from governance.audit import log_action
from governance.models import Plan


def _decimal_or_none(raw):
    """Same shape as governance/views.py::_parse_decimal, but also strips
    thousands separators (the regional prices this feeds are exactly the
    kind of number an admin types with commas, e.g. "8,900") - kept local
    to this app rather than importing governance's private (_-prefixed)
    helper across an app boundary."""
    raw = (raw or "").replace(",", "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _int_or_none(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _active_region_codes():
    """The default REGIONS plus any EXTRA_REGIONS a SuperAdmin has already
    added via "Add region" - detected by a RegionalPrice row existing for
    that code (created up front by add_region below, not lazily here), so
    this stays a plain read with no side effects."""
    default_codes = [code for code, *_ in REGIONS]
    extra_codes = [code for code, *_ in EXTRA_REGIONS]
    added_codes = list(
        RegionalPrice.objects.filter(region_code__in=extra_codes).values_list("region_code", flat=True).distinct()
    )
    return default_codes + [c for c in added_codes if c not in default_codes]


class RegionalPricingView(SuperAdminRequiredMixin, TemplateView):
    """SuperAdmin sets the exact price (and, per region, the extra-per-team
    fee) for every Plan - no auto-conversion between regions, matching the
    explicit product decision behind this whole feature. See billing app
    docstrings in models.py/regions.py for why extra_team_price lives on
    RegionalPrice (region-currency-specific) while teams_included lives on
    Plan (a plan-level policy, not currency-specific)."""

    template_name = "billing/regional_pricing.html"

    def get_context_data(self, **kwargs):
        plans = list(Plan.objects.order_by("-is_default", "name"))
        active_codes = _active_region_codes()

        # Lazily ensure a row exists for every (plan, active region) - a
        # newly-added Plan (or a region just added via add_region) always
        # has something to edit without needing a seed migration re-run.
        existing = {
            (rp.plan_id, rp.region_code): rp
            for rp in RegionalPrice.objects.filter(plan__in=plans, region_code__in=active_codes)
        }
        to_create = [
            RegionalPrice(plan=plan, region_code=code)
            for plan in plans
            for code in active_codes
            if (plan.id, code) not in existing
        ]
        if to_create:
            RegionalPrice.objects.bulk_create(to_create)
            existing = {
                (rp.plan_id, rp.region_code): rp
                for rp in RegionalPrice.objects.filter(plan__in=plans, region_code__in=active_codes)
            }

        region_labels = [REGION_BY_CODE[code] for code in active_codes]
        rows = []
        for plan in plans:
            cells = [existing[(plan.id, code)] for code in active_codes]
            rows.append({"plan": plan, "cells": cells, "missing": any(c.price is None for c in cells)})

        added_codes = {code for code, *_ in EXTRA_REGIONS} & set(active_codes)
        available_extra_regions = [r for r in EXTRA_REGIONS if r[0] not in added_codes]

        return super().get_context_data(**kwargs) | {
            "rows": rows,
            "region_labels": region_labels,
            "available_extra_regions": available_extra_regions,
            "any_missing": any(r["missing"] for r in rows),
        }


class PublicPricingView(TemplateView):
    """Unauthenticated pricing page - someone deciding whether to sign up,
    or a teammate sharing a link, should be able to see prices without an
    account (same reasoning as the /docs/ guides route in config/urls.py).

    Region defaults to a GeoIP guess from the visitor's IP (reuses
    accounts.geo's cached lookup - see billing.regions.region_for_country),
    but a `?region=<code>` query param always wins - that's what this
    page's own "switch to ROW/USD" link uses, and it makes the switched
    view a plain shareable/bookmarkable URL instead of hidden session
    state."""

    template_name = "billing/public_pricing.html"

    def get_context_data(self, **kwargs):
        active_codes = _active_region_codes()
        requested_region = self.request.GET.get("region", "").strip().upper()
        if requested_region in active_codes:
            region_code = requested_region
        else:
            ip_address = self.request.META.get("REMOTE_ADDR")
            region_code = region_for_country(country_code_for_ip(ip_address), active_codes)

        plans = list(Plan.objects.filter(is_active=True, is_demo=False).order_by("-is_default", "name"))
        prices = {rp.plan_id: rp for rp in RegionalPrice.objects.filter(plan__in=plans, region_code=region_code)}
        rows = [{"plan": plan, "price_row": prices.get(plan.id)} for plan in plans]

        other_regions = [_region_dict(code) for code in active_codes if code != region_code]

        return super().get_context_data(**kwargs) | {
            "region": _region_dict(region_code),
            "rows": rows,
            "other_regions": other_regions,
        }


def _region_dict(region_code):
    code, label, currency, flag = REGION_BY_CODE[region_code]
    return {"code": code, "label": label, "currency": currency, "flag": flag}


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def update_plan_regional_pricing(request, plan_id):
    plan = get_object_or_404(Plan, id=plan_id)
    plan.teams_included = _int_or_none(request.POST.get("teams_included"))
    plan.save(update_fields=["teams_included"])

    updated = {"teams_included": plan.teams_included}
    for code in _active_region_codes():
        rp, _created = RegionalPrice.objects.get_or_create(plan=plan, region_code=code)
        rp.price = _decimal_or_none(request.POST.get(f"price_{code}"))
        rp.extra_team_price = _decimal_or_none(request.POST.get(f"extra_team_price_{code}"))
        rp.save(update_fields=["price", "extra_team_price"])
        updated[code] = {"price": str(rp.price), "extra_team_price": str(rp.extra_team_price)}

    log_action(request.user, "billing.regional_pricing_update", plan, new_value=str(updated))
    return redirect("billing:regional_pricing")


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def add_region(request):
    # No log_action call here - there's no single Plan/model row this
    # action is "about" (it touches every Plan at once, and a region is a
    # registry entry, not a DB row) - every other log_action call in this
    # codebase has a real target object; forcing one in here would be
    # more misleading than just not logging a genuinely low-stakes action.
    region_code = request.POST.get("region_code", "").strip()
    valid_codes = {code for code, *_ in EXTRA_REGIONS}
    if region_code in valid_codes:
        RegionalPrice.objects.bulk_create(
            [
                RegionalPrice(plan=plan, region_code=region_code)
                for plan in Plan.objects.all()
                if not RegionalPrice.objects.filter(plan=plan, region_code=region_code).exists()
            ]
        )
    return redirect("billing:regional_pricing")
