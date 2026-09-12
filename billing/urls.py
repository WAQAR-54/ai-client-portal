from django.urls import path

from billing import views

app_name = "billing"

urlpatterns = [
    path("pricing/", views.PublicPricingView.as_view(), name="public_pricing"),
    path("regional-pricing/", views.RegionalPricingView.as_view(), name="regional_pricing"),
    path(
        "regional-pricing/<int:plan_id>/update/",
        views.update_plan_regional_pricing,
        name="update_plan_regional_pricing",
    ),
    path("regional-pricing/add-region/", views.add_region, name="add_region"),
    path("regional-pricing/remove-region/", views.remove_region, name="remove_region"),
    path("organization-billing/", views.OrganizationBillingSettingsView.as_view(), name="organization_billing"),
    path(
        "organization-billing/update/",
        views.update_organization_billing_profile,
        name="update_organization_billing",
    ),
    path(
        "departments/<int:department_id>/billing-profile/",
        views.DepartmentBillingProfileView.as_view(),
        name="department_billing_profile",
    ),
    path(
        "departments/<int:department_id>/billing-profile/update/",
        views.update_department_billing_profile,
        name="update_department_billing_profile",
    ),
    path("invoices/", views.InvoiceListView.as_view(), name="invoices"),
    path("invoices/generate/", views.generate_invoice, name="generate_invoice"),
    path(
        "departments/<int:department_id>/invoice-automation/",
        views.update_invoice_automation_settings,
        name="update_invoice_automation",
    ),
    path("invoices/<int:invoice_id>/", views.InvoiceDetailView.as_view(), name="invoice_detail"),
    path("invoices/<int:invoice_id>/pdf/", views.download_invoice_pdf, name="download_invoice_pdf"),
    path("invoices/<int:invoice_id>/toggle-status/", views.toggle_invoice_status, name="toggle_invoice_status"),
    path("invoices/<int:invoice_id>/verify/", views.verify_invoice_payment, name="verify_invoice_payment"),
    path("invoices/<int:invoice_id>/reject/", views.reject_invoice_payment, name="reject_invoice_payment"),
    path("invoices/<int:invoice_id>/email/", views.email_invoice_to_client, name="email_invoice"),
    path("invoices/<int:invoice_id>/delete/", views.delete_invoice, name="delete_invoice"),
    path("share/<str:token>/", views.public_invoice_view, name="public_invoice"),
    path("my-invoices/", views.MyInvoicesView.as_view(), name="my_invoices"),
    path("my-invoices/<int:invoice_id>/submit-proof/", views.submit_payment_proof, name="submit_payment_proof"),
    path("my-invoices/billing-profile/", views.update_my_billing_profile, name="update_my_billing_profile"),
]
