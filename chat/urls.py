from django.urls import path

from chat import views

app_name = "chat"

urlpatterns = [
    path("", views.chat_home, name="chat_home"),
    path("conversations/new/", views.create_conversation, name="create_conversation"),
    path("conversations/<int:conversation_id>/", views.chat_home, name="chat_conversation"),
    path("conversations/<int:conversation_id>/pin/", views.toggle_pin, name="toggle_pin"),
    path("conversations/<int:conversation_id>/delete/", views.delete_conversation, name="delete_conversation"),
    path("conversations/<int:conversation_id>/messages/", views.post_message, name="post_message"),
    path(
        "conversations/<int:conversation_id>/messages/<int:message_id>/stream/",
        views.stream_message,
        name="stream_message",
    ),
    path(
        "conversations/<int:conversation_id>/messages/<int:message_id>/attachment/",
        views.download_attachment,
        name="download_attachment",
    ),
    path(
        "conversations/<int:conversation_id>/messages/<int:message_id>/render/",
        views.render_message,
        name="render_message",
    ),
    path("usage-widget/", views.usage_widget, name="usage_widget"),
    path("request-upgrade/", views.request_upgrade, name="request_upgrade"),
]
