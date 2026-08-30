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
    path(
        "conversations/<int:conversation_id>/messages/<int:message_id>/edit/",
        views.edit_message,
        name="edit_message",
    ),
    path(
        "conversations/<int:conversation_id>/messages/<int:message_id>/regenerate/",
        views.regenerate_message,
        name="regenerate_message",
    ),
    path(
        "conversations/<int:conversation_id>/messages/<int:message_id>/feedback/",
        views.submit_feedback,
        name="submit_feedback",
    ),
    path(
        "conversations/<int:conversation_id>/export/markdown/",
        views.export_conversation_markdown,
        name="export_conversation_markdown",
    ),
    path(
        "conversations/<int:conversation_id>/export/text/",
        views.export_conversation_text,
        name="export_conversation_text",
    ),
    path(
        "conversations/<int:conversation_id>/export/pdf/",
        views.export_conversation_pdf,
        name="export_conversation_pdf",
    ),
    path("templates/", views.prompt_template_list, name="prompt_template_list"),
    path("templates/save/", views.save_prompt_template, name="save_prompt_template"),
    path("templates/<int:template_id>/delete/", views.delete_prompt_template, name="delete_prompt_template"),
    path("request-upgrade/", views.request_upgrade, name="request_upgrade"),
]
