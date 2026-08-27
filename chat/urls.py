from django.urls import path

from chat import views

app_name = "chat"

urlpatterns = [
    path("", views.chat_home, name="chat_home"),
    path("conversations/new/", views.create_conversation, name="create_conversation"),
    path("conversations/<int:conversation_id>/", views.chat_home, name="chat_conversation"),
    path("conversations/<int:conversation_id>/messages/", views.post_message, name="post_message"),
    path(
        "conversations/<int:conversation_id>/messages/<int:message_id>/stream/",
        views.stream_message,
        name="stream_message",
    ),
]
