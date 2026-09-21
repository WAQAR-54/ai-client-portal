from django.urls import path

from legal import views

app_name = "legal"

urlpatterns = [
    path("privacy/", views.PolicyView.as_view(key="privacy"), name="privacy"),
    path("terms/", views.PolicyView.as_view(key="terms"), name="terms"),
    path("refunds/", views.PolicyView.as_view(key="refund"), name="refund"),
    path("ai-usage/", views.PolicyView.as_view(key="ai_usage"), name="ai_usage"),
]
