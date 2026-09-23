from django.urls import path

from notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.notification_list, name="list"),
    path("bell/", views.bell_dropdown, name="bell_dropdown"),
    path("<int:notification_id>/read/", views.mark_read, name="mark_read"),
    path("<int:notification_id>/unread/", views.mark_unread, name="mark_unread"),
    path("mark-all-read/", views.mark_all_read, name="mark_all_read"),
    path("delete/", views.delete_notifications, name="delete_notifications"),
    path("preferences/", views.update_preferences, name="update_preferences"),
    path("track/<uuid:token>.gif", views.track_email_open, name="track_email_open"),
]
