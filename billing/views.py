from decimal import Decimal, InvalidOperation

from django.contrib import messages as django_messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from accounts.geo import country_code_for_ip
from accounts.models import Department, User
from accounts.permissions import AdminRequiredMixin, SuperAdminRequiredMixin, role_required
from billing.invoicing import InvoiceGenerationError, generate_invoice_for_department
from billing.models import DepartmentBillingProfile, Invoice, OrganizationBillingProfile, RegionalPrice
from billing.regions import EXTRA_REGIONS, REGION_BY_CODE, REGIONS, region_for_country
from billing.tax_rules import country_choices, tax_rule_for_country
from governance.audit import log_action
from governance.features import RequireFeatureMixin, require_feature
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
    """SuperAdmin sets the exact price (and, per region, the extra-per-seat
    fee) for every Plan - no auto-conversion between regions, matching the
    explicit product decision behind this whole feature. See billing app
    docstrings in models.py/regions.py for why extra_seat_price lives on
    RegionalPrice (region-currency-specific) while seats_included lives on
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


def _get_scoped_department_or_403(request, department_id):
    """Same rule as governance/views.py's own _get_scoped_department_or_403
    (not imported directly - that one is private to that module, and this
    app already reimplements its small POST-parsing helpers locally rather
    than reaching across an app boundary for _-prefixed functions): a
    department's billing profile is content an Admin operates within their
    own department, not SuperAdmin-only structure."""
    department = get_object_or_404(Department, id=department_id)
    if request.user.role == User.Role.ADMIN and department.id != request.user.department_id:
        raise PermissionDenied("That department is outside your scope.")
    return department


class OrganizationBillingSettingsView(SuperAdminRequiredMixin, TemplateView):
    """SuperAdmin-only: the operator's own payment/account details, shown
    on every invoice footer (Milestone 5) - a singleton, same pattern as
    governance's SecuritySettings/ComplianceSettings pages."""

    template_name = "billing/organization_billing.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {"profile": OrganizationBillingProfile.load()}


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def update_organization_billing_profile(request):
    profile = OrganizationBillingProfile.load()
    profile.bank_name = request.POST.get("bank_name", "").strip()
    profile.account_title = request.POST.get("account_title", "").strip()
    profile.account_number = request.POST.get("account_number", "").strip()
    profile.swift_code = request.POST.get("swift_code", "").strip()
    profile.payment_note = request.POST.get("payment_note", "").strip()
    profile.save(update_fields=["bank_name", "account_title", "account_number", "swift_code", "payment_note"])
    log_action(request.user, "billing.organization_billing_update", profile)
    return redirect("billing:organization_billing")


class DepartmentBillingProfileView(AdminRequiredMixin, RequireFeatureMixin, TemplateView):
    """A department's own billing/tax profile - reachable by that
    department's scoped Admin, or by a SuperAdmin for any department (same
    scoping as governance's SystemPromptView, which this mirrors)."""

    feature_key = "department_settings"
    template_name = "billing/department_billing_profile.html"

    def get_context_data(self, **kwargs):
        department = _get_scoped_department_or_403(self.request, kwargs["department_id"])
        profile, _created = DepartmentBillingProfile.objects.get_or_create(department=department)
        return super().get_context_data(**kwargs) | {
            "department": department,
            "profile": profile,
            "country_choices": country_choices(),
            "reminder_choices": DepartmentBillingProfile.ReminderSchedule.choices,
            "tax_rule": tax_rule_for_country(profile.country),
        }


@role_required(User.Role.ADMIN)
@require_feature("department_settings")
@require_http_methods(["POST"])
def update_department_billing_profile(request, department_id):
    department = _get_scoped_department_or_403(request, department_id)
    profile, _created = DepartmentBillingProfile.objects.get_or_create(department=department)

    profile.company_name = request.POST.get("company_name", "").strip()
    profile.country = request.POST.get("country", "").strip()
    profile.billing_address = request.POST.get("billing_address", "").strip()
    profile.tax_id = request.POST.get("tax_id", "").strip()
    profile.is_tax_exempt = request.POST.get("is_tax_exempt") == "on"
    profile.custom_tax_rate = _decimal_or_none(request.POST.get("custom_tax_rate"))
    profile.auto_generate_invoices = request.POST.get("auto_generate_invoices") == "on"
    profile.reminder_days_after_due = _int_or_none(request.POST.get("reminder_days_after_due")) or 0
    profile.save(
        update_fields=[
            "company_name",
            "country",
            "billing_address",
            "tax_id",
            "is_tax_exempt",
            "custom_tax_rate",
            "auto_generate_invoices",
            "reminder_days_after_due",
        ]
    )
    log_action(request.user, "billing.department_billing_profile_update", profile)
    return redirect("billing:department_billing_profile", department_id=department.id)


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def update_plan_regional_pricing(request, plan_id):
    plan = get_object_or_404(Plan, id=plan_id)
    plan.seats_included = _int_or_none(request.POST.get("seats_included"))
    plan.save(update_fields=["seats_included"])

    updated = {"seats_included": plan.seats_included}
    for code in _active_region_codes():
        rp, _created = RegionalPrice.objects.get_or_create(plan=plan, region_code=code)
        rp.price = _decimal_or_none(request.POST.get(f"price_{code}"))
        rp.extra_seat_price = _decimal_or_none(request.POST.get(f"extra_seat_price_{code}"))
        rp.save(update_fields=["price", "extra_seat_price"])
        updated[code] = {"price": str(rp.price), "extra_seat_price": str(rp.extra_seat_price)}

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


def _is_scoped_admin(user):
    """Same rule as governance/views.py's own _is_scoped_admin - a plain
    Admin only ever sees/manages their own department's invoices;
    SuperAdmin is unscoped."""
    return user.role == User.Role.ADMIN


def _eligible_recipients(request):
    """Every user who could be billed - any user with a department (an
    invoice always belongs to a department), regardless of whether that
    department already has a plan assigned: the plan is chosen per-invoice
    at generation time (see generate_invoice below), not required
    up front. Scoped the same way as every other per-department admin
    action - a plain Admin only sees their own department's users."""
    qs = User.objects.filter(department__isnull=False).select_related("department", "department__plan")
    if _is_scoped_admin(request.user):
        qs = qs.filter(department_id=request.user.department_id)
    return qs.order_by("department__name", "email")


def _invoices_context(request):
    """Shared by InvoiceListView and the htmx re-render after a toggle/
    verify/reject, same reasoning as governance's _models_table_context:
    every action respects whatever department filter was already showing."""
    qs = Invoice.objects.select_related("department", "plan", "recipient_user").order_by("-issue_date", "-id")
    departments = None
    selected_department = ""
    if _is_scoped_admin(request.user):
        qs = qs.filter(department_id=request.user.department_id)
    else:
        departments = Department.objects.order_by("name")
        selected_department = request.GET.get("department", "").strip()
        if selected_department.isdigit():
            qs = qs.filter(department_id=int(selected_department))
    return {
        "invoices": qs,
        "departments": departments,
        "selected_department": selected_department,
        "eligible_recipients": _eligible_recipients(request),
        "billable_plans": Plan.objects.filter(is_active=True).order_by("-is_default", "name"),
    }


class InvoiceListView(AdminRequiredMixin, TemplateView):
    """Admin manages only their own department's invoices (generate, mark
    paid, verify/reject a submission); SuperAdmin manages every
    department's, filterable by ?department=<id>."""

    template_name = "billing/invoices.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | _invoices_context(self.request)


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def generate_invoice(request):
    recipient = get_object_or_404(User, id=request.POST.get("recipient_user_id"))
    if _is_scoped_admin(request.user) and recipient.department_id != request.user.department_id:
        raise PermissionDenied("That user is outside your department.")
    if recipient.department_id is None:
        django_messages.error(request, "This user has no department, so they can't be billed.")
        return redirect("billing:invoices")

    plan_id = request.POST.get("plan_id", "").strip()
    plan = get_object_or_404(Plan, id=plan_id) if plan_id else None
    seat_count = _int_or_none(request.POST.get("seat_count"))

    try:
        invoice = generate_invoice_for_department(
            recipient.department, recipient_user=recipient, plan=plan, seat_count=seat_count
        )
    except InvoiceGenerationError as exc:
        django_messages.error(request, str(exc))
    else:
        log_action(request.user, "billing.invoice_generate", invoice, new_value=invoice.invoice_number)
    return redirect("billing:invoices")


def _get_scoped_invoice_or_403(request, invoice_id):
    invoice = get_object_or_404(Invoice, id=invoice_id)
    if _is_scoped_admin(request.user) and invoice.department_id != request.user.department_id:
        raise PermissionDenied("That invoice is outside your scope.")
    return invoice


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def toggle_invoice_status(request, invoice_id):
    """A plain manual override (e.g. payment confirmed some other way) -
    separate from verify_invoice_payment/reject_invoice_payment below,
    which specifically resolve a user's submitted proof and record who
    reviewed it."""
    invoice = _get_scoped_invoice_or_403(request, invoice_id)
    old_status = invoice.status
    invoice.status = Invoice.Status.UNPAID if invoice.status == Invoice.Status.PAID else Invoice.Status.PAID
    invoice.save(update_fields=["status"])
    log_action(request.user, "billing.invoice_status_toggle", invoice, old_value=old_status, new_value=invoice.status)
    if request.headers.get("HX-Request"):
        return render(request, "billing/_invoices_table.html", _invoices_context(request))
    return redirect("billing:invoices")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def verify_invoice_payment(request, invoice_id):
    invoice = _get_scoped_invoice_or_403(request, invoice_id)
    invoice.verify_payment(request.user)
    log_action(request.user, "billing.invoice_payment_verified", invoice, new_value=invoice.status)
    if request.headers.get("HX-Request"):
        return render(request, "billing/_invoices_table.html", _invoices_context(request))
    return redirect("billing:invoices")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def reject_invoice_payment(request, invoice_id):
    invoice = _get_scoped_invoice_or_403(request, invoice_id)
    invoice.reject_payment(request.user)
    log_action(request.user, "billing.invoice_payment_rejected", invoice, new_value=invoice.status)
    if request.headers.get("HX-Request"):
        return render(request, "billing/_invoices_table.html", _invoices_context(request))
    return redirect("billing:invoices")


class MyInvoicesView(LoginRequiredMixin, TemplateView):
    """Any authenticated user's own invoices - visible regardless of role,
    since who gets billed (recipient_user) is chosen independently of
    Admin/SuperAdmin/Manager/User (see generate_invoice above)."""

    template_name = "billing/my_invoices.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {
            "invoices": Invoice.objects.filter(recipient_user=self.request.user)
            .select_related("plan", "department")
            .order_by("-issue_date", "-id"),
        }


@require_http_methods(["POST"])
def submit_payment_proof(request, invoice_id):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    invoice = get_object_or_404(Invoice, id=invoice_id, recipient_user=request.user)
    if invoice.status != Invoice.Status.UNPAID:
        return redirect("billing:my_invoices")

    transaction_id = request.POST.get("transaction_id", "").strip()
    proof_image = request.FILES.get("proof_image")
    if not transaction_id and not proof_image:
        django_messages.error(request, "Provide a transaction ID or a payment screenshot.")
        return redirect("billing:my_invoices")

    invoice.submit_payment_proof(transaction_id=transaction_id, proof_image=proof_image)
    log_action(request.user, "billing.invoice_payment_submitted", invoice, new_value=invoice.status)
    return redirect("billing:my_invoices")
