from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from accounts.geo import country_code_for_ip
from accounts.models import Department, User
from accounts.permissions import AdminRequiredMixin, SuperAdminRequiredMixin, role_required
from billing.invoicing import InvoiceGenerationError, generate_invoice_for_user
from billing.models import (
    DepartmentBillingProfile,
    Invoice,
    OrganizationBillingProfile,
    RegionalPrice,
    UserBillingProfile,
    billing_profile_for_invoice,
)
from billing.pdf import render_invoice_pdf
from billing.regions import EXTRA_REGIONS, REGION_BY_CODE, REGIONS, region_for_country
from billing.tax_rules import country_choices, tax_rule_for_country
from governance.audit import log_action
from governance.features import RequireFeatureMixin, require_feature
from governance.models import Plan, SiteBranding
from notifications.emailing import send_tracked_email


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

        added_codes = {code for code, *_ in EXTRA_REGIONS} & set(active_codes)
        region_labels = []
        for code in active_codes:
            _code, label, currency, flag = REGION_BY_CODE[code]
            region_labels.append(
                {"code": code, "label": label, "currency": currency, "flag": flag, "removable": code in added_codes}
            )
        rows = []
        for plan in plans:
            cells = [existing[(plan.id, code)] for code in active_codes]
            rows.append({"plan": plan, "cells": cells, "missing": any(c.price is None for c in cells)})

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


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def remove_region(request):
    # Mirrors add_region: only ever an EXTRA_REGIONS code (never one of the
    # four always-on REGIONS), no log_action for the same reason add_region
    # has none - deleting every RegionalPrice row for this code is exactly
    # what makes it vanish from _active_region_codes() (that function
    # detects "active" purely by row existence), so it reappears in
    # "+ Add region" with no separate removed-flag to maintain.
    region_code = request.POST.get("region_code", "").strip()
    valid_codes = {code for code, *_ in EXTRA_REGIONS}
    if region_code in valid_codes:
        RegionalPrice.objects.filter(region_code=region_code).delete()
    return redirect("billing:regional_pricing")


def _is_scoped_admin(user):
    """Same rule as governance/views.py's own _is_scoped_admin - a plain
    Admin only ever sees/manages their own department's invoices;
    SuperAdmin is unscoped."""
    return user.role == User.Role.ADMIN


def _eligible_recipients(request):
    """Every user who could be billed - department-optional (Milestone 6:
    billing.invoicing.generate_invoice_for_user bills a department-less
    user directly), so this is no longer filtered to only users who
    happen to have one. Scoped the same way as every other per-department
    admin action: a plain Admin only sees their own department's users -
    a department-less user isn't within any Admin's authority, only
    SuperAdmin's, same reasoning as _get_scoped_invoice_or_403's explicit
    None-never-matches guard."""
    qs = User.objects.select_related("department", "department__plan")
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
        "billable_regions": [REGION_BY_CODE[code] for code in _active_region_codes()],
        # Deleting an invoice is SuperAdmin-only (see delete_invoice) -
        # passed down so the table's Delete button only renders for
        # someone who can actually use it.
        "can_delete_invoices": request.user.role == User.Role.SUPERADMIN,
    }


def _automation_context(request, invoices_context):
    """The "Automated Invoicing" card only makes sense pinned to one
    department (auto_generate_invoices/reminder_days_after_due are
    per-department fields) - a scoped Admin's own department, or whatever
    department a SuperAdmin has filtered the list to. Kept separate from
    _invoices_context (rather than folded into it) so the htmx re-render
    after a toggle/verify/reject doesn't pay for this extra lookup on
    every click - only the full page load needs it."""
    if _is_scoped_admin(request.user):
        automation_department = Department.objects.filter(id=request.user.department_id).first()
    else:
        selected_department = invoices_context["selected_department"]
        automation_department = (
            Department.objects.filter(id=int(selected_department)).first() if selected_department.isdigit() else None
        )
    automation_profile = None
    if automation_department is not None:
        automation_profile, _created = DepartmentBillingProfile.objects.get_or_create(department=automation_department)
    return {
        "automation_department": automation_department,
        "automation_profile": automation_profile,
        "reminder_choices": DepartmentBillingProfile.ReminderSchedule.choices,
    }


class InvoiceListView(AdminRequiredMixin, TemplateView):
    """Admin manages only their own department's invoices (generate, mark
    paid, verify/reject a submission); SuperAdmin manages every
    department's, filterable by ?department=<id>."""

    template_name = "billing/invoices.html"

    def get_context_data(self, **kwargs):
        invoices_context = _invoices_context(self.request)
        return (
            super().get_context_data(**kwargs) | invoices_context | _automation_context(self.request, invoices_context)
        )


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def generate_invoice(request):
    recipient = get_object_or_404(User, id=request.POST.get("recipient_user_id"))
    # A department-less recipient isn't within any scoped Admin's
    # authority (only SuperAdmin's) - explicit `is None` guard so this
    # never lets a department-less Admin match a department-less
    # recipient via `None == None`, same pattern as
    # _get_scoped_invoice_or_403.
    if _is_scoped_admin(request.user) and (
        recipient.department_id is None or recipient.department_id != request.user.department_id
    ):
        raise PermissionDenied("That user is outside your department.")

    plan_id = request.POST.get("plan_id", "").strip()
    plan = get_object_or_404(Plan, id=plan_id) if plan_id else None
    seat_count = _int_or_none(request.POST.get("seat_count"))
    region_code = request.POST.get("region_code", "").strip() or None

    try:
        # generate_invoice_for_user bills the department's subscription
        # when the recipient has one with a plan assigned, and the
        # recipient directly otherwise - the exact same choice the
        # automatic welcome-invoice signal and recurring sweep already
        # make, so a manually-generated invoice is never a special case.
        invoice = generate_invoice_for_user(recipient, plan=plan, seat_count=seat_count, region_code=region_code)
    except InvoiceGenerationError as exc:
        django_messages.error(request, str(exc))
    else:
        log_action(request.user, "billing.invoice_generate", invoice, new_value=invoice.invoice_number)
    return redirect("billing:invoices")


@role_required(User.Role.SUPERADMIN, exact=True)
@require_http_methods(["POST"])
def delete_invoice(request, invoice_id):
    invoice = get_object_or_404(Invoice, id=invoice_id)
    log_action(request.user, "billing.invoice_delete", invoice, old_value=invoice.invoice_number)
    invoice.delete()
    if request.headers.get("HX-Request"):
        return render(request, "billing/_invoices_table.html", _invoices_context(request))
    return redirect("billing:invoices")


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def update_invoice_automation_settings(request, department_id):
    """Only touches auto_generate_invoices/reminder_days_after_due - unlike
    update_department_billing_profile (the full billing-profile form),
    which would blank out company_name/country/etc. if posted from here
    with just these two fields."""
    department = _get_scoped_department_or_403(request, department_id)
    profile, _created = DepartmentBillingProfile.objects.get_or_create(department=department)
    profile.auto_generate_invoices = request.POST.get("auto_generate_invoices") == "on"
    profile.reminder_days_after_due = _int_or_none(request.POST.get("reminder_days_after_due")) or 0
    profile.save(update_fields=["auto_generate_invoices", "reminder_days_after_due"])
    log_action(request.user, "billing.invoice_automation_update", profile)
    return redirect("billing:invoices")


def _get_scoped_invoice_or_403(request, invoice_id):
    invoice = get_object_or_404(Invoice, id=invoice_id)
    # invoice.department_id can be None (department-less recipient) - a
    # scoped Admin never manages a department-less invoice regardless of
    # their own department_id, so None is treated as "never matches" on
    # both sides rather than letting two Nones compare equal.
    if _is_scoped_admin(request.user) and (
        invoice.department_id is None or invoice.department_id != request.user.department_id
    ):
        raise PermissionDenied("That invoice is outside your scope.")
    return invoice


def _share_url(invoice):
    """Absolute URL for the no-login public invoice view - built from
    settings.SITE_URL (same convention as notifications/emailing.py's own
    tracking-pixel URL) rather than request.build_absolute_uri(), which
    depends on the request's Host header passing ALLOWED_HOSTS validation.
    SITE_URL is one fixed, explicitly-configured value, so this can never
    fail from a request-side quirk (a proxy, a bare IP, a missing header)."""
    return settings.SITE_URL.rstrip("/") + reverse("billing:public_invoice", kwargs={"token": invoice.share_token})


def _safe_next_url(request, default):
    """`next` is only ever one of this app's own invoice URLs (the detail
    page posting back to itself) - never taken as an open redirect target,
    hence the reverse() re-derivation rather than trusting the raw POST
    value directly."""
    invoice_id = request.POST.get("next_invoice_id", "").strip()
    if invoice_id.isdigit() and Invoice.objects.filter(id=invoice_id).exists():
        return reverse("billing:invoice_detail", kwargs={"invoice_id": int(invoice_id)})
    return default


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
    return redirect(_safe_next_url(request, reverse("billing:invoices")))


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def verify_invoice_payment(request, invoice_id):
    invoice = _get_scoped_invoice_or_403(request, invoice_id)
    invoice.verify_payment(request.user)
    log_action(request.user, "billing.invoice_payment_verified", invoice, new_value=invoice.status)
    if request.headers.get("HX-Request"):
        return render(request, "billing/_invoices_table.html", _invoices_context(request))
    return redirect(_safe_next_url(request, reverse("billing:invoices")))


@role_required(User.Role.ADMIN)
@require_http_methods(["POST"])
def reject_invoice_payment(request, invoice_id):
    invoice = _get_scoped_invoice_or_403(request, invoice_id)
    invoice.reject_payment(request.user)
    log_action(request.user, "billing.invoice_payment_rejected", invoice, new_value=invoice.status)
    if request.headers.get("HX-Request"):
        return render(request, "billing/_invoices_table.html", _invoices_context(request))
    return redirect(_safe_next_url(request, reverse("billing:invoices")))


class MyInvoicesView(LoginRequiredMixin, TemplateView):
    """Any authenticated user's own invoices - visible regardless of role,
    since who gets billed (recipient_user) is chosen independently of
    Admin/SuperAdmin/Manager/User (see generate_invoice above)."""

    template_name = "billing/my_invoices.html"

    def get_context_data(self, **kwargs):
        profile, _created = UserBillingProfile.objects.get_or_create(user=self.request.user)
        return super().get_context_data(**kwargs) | {
            "invoices": Invoice.objects.filter(recipient_user=self.request.user)
            .select_related("plan", "department")
            .order_by("-issue_date", "-id"),
            "my_billing_profile": profile,
        }


@require_http_methods(["POST"])
def submit_payment_proof(request, invoice_id):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    invoice = get_object_or_404(Invoice, id=invoice_id, recipient_user=request.user)
    default_redirect = _safe_next_url(request, reverse("billing:my_invoices"))
    if invoice.status != Invoice.Status.UNPAID:
        return redirect(default_redirect)

    transaction_id = request.POST.get("transaction_id", "").strip()
    proof_image = request.FILES.get("proof_image")
    if not transaction_id and not proof_image:
        django_messages.error(request, "Provide a transaction ID or a payment screenshot.")
        return redirect(default_redirect)

    invoice.submit_payment_proof(transaction_id=transaction_id, proof_image=proof_image)
    log_action(request.user, "billing.invoice_payment_submitted", invoice, new_value=invoice.status)
    return redirect(default_redirect)


def _can_view_invoice(user, invoice):
    """Who's allowed to open one invoice's detail page: the person it's
    billed to, always; otherwise the same Admin(-own-department)/
    SuperAdmin(-any) scoping as every management action above."""
    if invoice.recipient_user_id == user.id:
        return True
    if user.role == User.Role.SUPERADMIN:
        return True
    # invoice.department_id can be None (a department-less recipient - see
    # generate_invoice_for_user) - explicit `is not None` guard so a
    # department-less Admin (an edge case, but a real one) can never match
    # a department-less invoice that isn't theirs via `None == None`.
    return (
        user.role == User.Role.ADMIN
        and invoice.department_id is not None
        and invoice.department_id == user.department_id
    )


class InvoiceDetailView(LoginRequiredMixin, TemplateView):
    """One invoice, fully expanded - amounts, the recipient's submitted
    payment proof (if any), and the payment/verification actions relevant
    to whoever's looking (submit-proof for the recipient, approve/reject/
    toggle for whoever manages this department's billing). Reachable from
    both the admin Invoices list and a recipient's own My Invoices page,
    which is why access is checked here rather than via a role mixin."""

    template_name = "billing/invoice_detail.html"

    def get_context_data(self, **kwargs):
        invoice = get_object_or_404(
            Invoice.objects.select_related("department", "plan", "recipient_user"), id=kwargs["invoice_id"]
        )
        if not _can_view_invoice(self.request.user, invoice):
            raise PermissionDenied("You don't have access to this invoice.")
        return super().get_context_data(**kwargs) | {
            "invoice": invoice,
            "billing_profile": billing_profile_for_invoice(invoice),
            "is_recipient": invoice.recipient_user_id == self.request.user.id,
            "can_manage": invoice.recipient_user_id != self.request.user.id
            and self.request.user.role in (User.Role.ADMIN, User.Role.SUPERADMIN),
            "organization_profile": OrganizationBillingProfile.load(),
            "share_url": _share_url(invoice),
        }


@require_http_methods(["GET"])
def download_invoice_pdf(request, invoice_id):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    invoice = get_object_or_404(Invoice.objects.select_related("department", "plan", "recipient_user"), id=invoice_id)
    if not _can_view_invoice(request.user, invoice):
        raise PermissionDenied("You don't have access to this invoice.")
    pdf_bytes = render_invoice_pdf(invoice)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{invoice.invoice_number}.pdf"'
    return response


@require_http_methods(["GET"])
def public_invoice_view(request, token):
    """No-login invoice view for a client who received a share link/email -
    looked up by the unguessable share_token rather than the sequential id,
    same reasoning as everything else here: billing amounts/bank details
    shouldn't be reachable by anyone who can merely guess a small integer.
    Deliberately read-only (no Manage/submit-payment actions) - an
    anonymous viewer isn't tied to any account, so this only ever renders
    the document itself, same as the PDF."""
    invoice = get_object_or_404(
        Invoice.objects.select_related("department", "plan", "recipient_user"), share_token=token
    )
    return render(
        request,
        "billing/invoice_public.html",
        {
            "invoice": invoice,
            "billing_profile": billing_profile_for_invoice(invoice),
            "organization_profile": OrganizationBillingProfile.load(),
            "site_branding": SiteBranding.load(),
        },
    )


@require_http_methods(["POST"])
def email_invoice_to_client(request, invoice_id):
    """Emails the recipient the same no-login share link a SuperAdmin/Admin
    can also copy manually from the invoice detail page - reuses the
    established send_tracked_email path (notifications/emailing.py) so
    this shows up in the admin Email Logs page like every other email the
    app sends."""
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    invoice = get_object_or_404(Invoice.objects.select_related("recipient_user"), id=invoice_id)
    if not _can_view_invoice(request.user, invoice) or request.user.role not in (
        User.Role.ADMIN,
        User.Role.SUPERADMIN,
    ):
        raise PermissionDenied("You don't have access to this invoice.")
    # Same convention as toggle/verify/reject: stays on the invoices LIST
    # by default (the new per-row "Email" button there never passes
    # next_invoice_id), but goes back to the detail page when it does
    # (the detail page's own "Email to client" button always passes it).
    default_redirect = _safe_next_url(request, reverse("billing:invoices"))
    if invoice.recipient_user_id is None or not invoice.recipient_user.email:
        django_messages.error(request, "This invoice has no recipient email to send to.")
        return redirect(default_redirect)

    share_url = _share_url(invoice)
    site_name = SiteBranding.load().site_name
    subject = f"{site_name}: Invoice {invoice.invoice_number}"
    text_body = (
        f"Your invoice {invoice.invoice_number} ({invoice.currency} {invoice.total}) is ready.\n\n"
        f"View it here: {share_url}"
    )
    success, error = send_tracked_email(invoice.recipient_user.email, subject, text_body)
    if success:
        django_messages.success(request, f"Invoice emailed to {invoice.recipient_user.email}.")
        log_action(request.user, "billing.invoice_emailed", invoice, new_value=invoice.recipient_user.email)
    else:
        django_messages.error(request, f"Couldn't send that email: {error}")
    return redirect(default_redirect)


@require_http_methods(["POST"])
def update_my_billing_profile(request):
    """A user's own Bill To details for a department-less invoice (name/
    email already come from the User record - see _billing_profile_for_
    invoice) - edited from My Invoices, mirroring how a department's
    billing profile is edited from the department's own settings."""
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    profile, _created = UserBillingProfile.objects.get_or_create(user=request.user)
    profile.company_name = request.POST.get("company_name", "").strip()
    profile.phone_number = request.POST.get("phone_number", "").strip()
    profile.billing_address = request.POST.get("billing_address", "").strip()
    profile.save(update_fields=["company_name", "phone_number", "billing_address"])
    django_messages.success(request, "Billing details updated.")
    return redirect("billing:my_invoices")
