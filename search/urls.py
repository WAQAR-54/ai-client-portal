from django.urls import path

from search import views

app_name = "search"

urlpatterns = [
    path("global/", views.global_search, name="global"),
]
