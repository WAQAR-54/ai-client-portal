from django.urls import path

from domaingen import views

app_name = "domaingen"

urlpatterns = [
    path("", views.DomainGeneratorView.as_view(), name="home"),
    path("generate/", views.generate_domains, name="generate"),
]
