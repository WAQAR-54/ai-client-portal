from django.urls import path

from playground import views

app_name = "playground"

urlpatterns = [
    path("", views.PlaygroundView.as_view(), name="home"),
    path("log-run/", views.log_run, name="log_run"),
]
