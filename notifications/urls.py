from django.urls import path

from notifications import views

app_name = "notifications"

urlpatterns = [
    path("bell/", views.bell_dropdown, name="bell_dropdown"),
    path("<int:notification_id>/read/", views.mark_read, name="mark_read"),
    path("mark-all-read/", views.mark_all_read, name="mark_all_read"),
    path("preferences/", views.update_preferences, name="update_preferences"),
]
